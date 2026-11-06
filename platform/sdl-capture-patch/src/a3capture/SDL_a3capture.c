/*
 * a3capture: a private SDL2 audio backend for the auto-heros3 recorder.
 *
 * This is an experimental A2 spike, not the final A3 wire transport.
 * - no physical endpoint is opened;
 * - the SDL audio callback only copies into a bounded preallocated ring and
 *   signals a worker (no file/pipe/log/flush I/O in the callback);
 * - WaitDevice uses an absolute QPC deadline based on cumulative sample frames;
 * - the worker writes raw s16le/stereo PCM to A3CAPTURE_FILE for load/format
 *   verification.  Framed a3-audio/1 packets are the next step.
 */
#include "../../SDL_internal.h"

#ifdef SDL_AUDIO_DRIVER_A3CAPTURE

#include "SDL.h"
#include "SDL_audio.h"
#include "SDL_mutex.h"
#include "SDL_rwops.h"
#include "SDL_timer.h"
#include "../SDL_audio_c.h"
#include "../SDL_sysaudio.h"
#include "audio_clock_c.h"

/* Use Windows kernel threads and SDL_RWops.  The private SDL2 DLL is linked
 * with /NODEFAULTLIB; pulling stdio/_beginthreadex into the backend would
 * create CRT imports that SDL's own build does not resolve. */
#include <windows.h>

/* SDL_sysaudio.h undefines _THIS at its end; re-establish the backend macro. */
#define _THIS SDL_AudioDevice *_this

#define A3CAPTURE_QUEUE_SLOTS 16
#define A3CAPTURE_DEFAULT_FILE "a3capture.raw"

typedef struct A3Packet
{
    Uint64 sequence;
    Uint64 frame_start;
    Uint32 frames;
    Uint64 planned_qpc;
    Uint64 actual_start_qpc;
    Uint64 actual_end_qpc;
    Uint8 *data;
} A3Packet;

struct SDL_PrivateAudioData
{
    SDL_mutex *lock;
    SDL_cond *cond;
    HANDLE worker;
    volatile SDL_bool stopping;
    SDL_bool worker_done;
    SDL_RWops *out;
    char *out_path;
    char *ready_path;
    A3Packet packets[A3CAPTURE_QUEUE_SLOTS];
    A3Packet *free_slots[A3CAPTURE_QUEUE_SLOTS];
    A3Packet *queue[A3CAPTURE_QUEUE_SLOTS];
    int free_count;
    int queue_head;
    int queue_count;
    Uint32 payload_bytes;
    Uint64 next_sequence;
    Uint64 generated_frames;
    Uint64 queued_frames;
    Uint64 written_frames;
    Uint64 committed_frames;
    Uint64 qpc_frequency;
    Uint64 epoch_qpc;
    Uint64 next_deadline_qpc;
    Uint64 planned_qpc;
    Uint64 actual_start_qpc;
    Uint64 callback_start_qpc;
    Uint64 first_dropped_frame;
    Uint64 dropped_frames;
    Uint32 sample_rate;
    Uint32 samples_per_callback;
    Uint64 late_events;
    Uint64 overflow_events;
    Uint64 written_packets;
    Uint64 written_bytes;
    SDL_bool failed;
    SDL_bool closed;
    char write_error[128];
    Uint32 test_worker_delay_ms;
    SDL_bool test_short_write;
    SDL_bool test_no_thread;
    Uint8 *mixbuf;
};

static int A3Deadline(const struct SDL_PrivateAudioData *h, Uint64 frame, Uint64 *out)
{
    const a3_qpc_clock clock = {h->epoch_qpc, h->qpc_frequency, h->sample_rate};
    return a3_qpc_deadline(&clock, frame, out);
}

static DWORD WINAPI A3Worker(LPVOID arg)
{
    SDL_AudioDevice *device = (SDL_AudioDevice *)arg;
    struct SDL_PrivateAudioData *h = device->hidden;

    for (;;) {
        A3Packet *packet = NULL;
        SDL_LockMutex(h->lock);
        while (h->queue_count == 0 && !h->stopping) {
            SDL_CondWait(h->cond, h->lock);
        }
        if (h->queue_count == 0 && h->stopping) {
            SDL_UnlockMutex(h->lock);
            break;
        }
        packet = h->queue[h->queue_head];
        h->queue_head = (h->queue_head + 1) % A3CAPTURE_QUEUE_SLOTS;
        --h->queue_count;
        SDL_UnlockMutex(h->lock);

        if (packet) {
            if (h->test_worker_delay_ms) {
                SDL_Delay(h->test_worker_delay_ms);
            }
            const size_t expected = (size_t)packet->frames * 2u * 2u;
            if (!h->out || expected == 0) {
                SDL_LockMutex(h->lock);
                h->failed = SDL_TRUE;
                SDL_snprintf(h->write_error, sizeof(h->write_error),
                             "missing output or empty packet");
                h->free_slots[h->free_count++] = packet;
                SDL_UnlockMutex(h->lock);
            } else {
                const size_t write_len =
                    (h->test_short_write && expected > 1) ? expected - 1 : expected;
                const size_t wrote = SDL_RWwrite(h->out, packet->data, 1, write_len);
                SDL_LockMutex(h->lock);
                if (wrote == expected) {
                    ++h->written_packets;
                    h->written_bytes += expected;
                    h->written_frames += packet->frames;
                    h->committed_frames = h->written_frames;
                } else {
                    h->failed = SDL_TRUE;
                    SDL_snprintf(h->write_error, sizeof(h->write_error),
                                 "short write %llu/%llu at frame %llu",
                                 (unsigned long long)wrote,
                                 (unsigned long long)expected,
                                 (unsigned long long)packet->frame_start);
                }
                h->free_slots[h->free_count++] = packet;
                SDL_UnlockMutex(h->lock);
            }
        }
    }

    SDL_LockMutex(h->lock);
    h->worker_done = SDL_TRUE;
    SDL_UnlockMutex(h->lock);
    return 0;
}

static void A3CAPTURE_WaitDevice(_THIS)
{
    struct SDL_PrivateAudioData *h = _this->hidden;
    const Uint64 deadline = h->next_deadline_qpc;
    Uint64 now = SDL_GetPerformanceCounter();
    if (now < deadline) {
        const Uint64 remain = deadline - now;
        Uint64 ms = remain * 1000ULL / h->qpc_frequency;
        if (ms > 0) {
            SDL_Delay((Uint32)(ms > 50 ? 50 : ms));
        }
        while ((now = SDL_GetPerformanceCounter()) < deadline) {
            SDL_Delay(0);
        }
    }
    now = SDL_GetPerformanceCounter();
    // Measure lateness after the sleep as well as before it.  A late wakeup
    // must not be reported as late=0 just because the entry check was early.
    if (now > deadline + h->qpc_frequency / 1000ULL) {
        ++h->late_events;
    }
    h->actual_start_qpc = now;
}

static void A3CAPTURE_PlayDevice(_THIS)
{
    struct SDL_PrivateAudioData *h = _this->hidden;
    A3Packet *packet = NULL;
    const Uint64 now = SDL_GetPerformanceCounter();
    const Uint64 frame_start = h->generated_frames;
    Uint64 planned = 0;

    SDL_LockMutex(h->lock);
    const Uint64 sequence = h->next_sequence++;
    if (!A3Deadline(h, frame_start, &planned)) {
        h->failed = SDL_TRUE;
        SDL_snprintf(h->write_error, sizeof(h->write_error),
                     "deadline overflow at frame %llu",
                     (unsigned long long)frame_start);
        planned = now;
    }
    if (h->free_count > 0) {
        packet = h->free_slots[--h->free_count];
    } else {
        ++h->overflow_events;
        if (h->dropped_frames == 0) {
            h->first_dropped_frame = frame_start;
        }
        h->dropped_frames += h->samples_per_callback;
        h->failed = SDL_TRUE;
    }
    if (packet) {
        packet->sequence = sequence;
        packet->frame_start = frame_start;
        packet->frames = h->samples_per_callback;
        packet->planned_qpc = planned;
        packet->actual_start_qpc = h->callback_start_qpc ? h->callback_start_qpc : now;
        packet->actual_end_qpc = now;
        memcpy(packet->data, h->mixbuf, h->payload_bytes);
        h->queue[(h->queue_head + h->queue_count) % A3CAPTURE_QUEUE_SLOTS] = packet;
        ++h->queue_count;
        h->queued_frames += h->samples_per_callback;
        SDL_CondSignal(h->cond);
    }
    // The generated cursor advances even when the queue rejects a block; the
    // dropped interval is explicit and never re-labelled as a later packet.
    h->planned_qpc = planned;
    h->generated_frames += h->samples_per_callback;
    if (!A3Deadline(h, h->generated_frames, &h->next_deadline_qpc)) {
        h->failed = SDL_TRUE;
    }
    SDL_UnlockMutex(h->lock);
}

static Uint8 *A3CAPTURE_GetDeviceBuf(_THIS)
{
    _this->hidden->callback_start_qpc = SDL_GetPerformanceCounter();
    return _this->hidden->mixbuf;
}

static void A3CAPTURE_CloseDevice(_THIS)
{
    struct SDL_PrivateAudioData *h = _this->hidden;
    if (!h || h->closed) {
        _this->hidden = NULL;
        return;
    }
    h->closed = SDL_TRUE;
    // Clear the device pointer before waiting: an upper-layer double close is
    // then a harmless no-op, and an OpenDevice failure cannot free twice.
    _this->hidden = NULL;
    SDL_LockMutex(h->lock);
    h->stopping = SDL_TRUE;
    SDL_CondSignal(h->cond);
    SDL_Log("a3capture closing: generated=%llu queued=%llu written=%llu committed=%llu "
            "dropped=%llu late=%llu overflow=%llu failed=%d error='%s'",
            (unsigned long long)h->generated_frames,
            (unsigned long long)h->queued_frames,
            (unsigned long long)h->written_frames,
            (unsigned long long)h->committed_frames,
            (unsigned long long)h->dropped_frames,
            (unsigned long long)h->late_events,
            (unsigned long long)h->overflow_events,
            h->failed ? 1 : 0, h->write_error);
    SDL_UnlockMutex(h->lock);
    if (h->worker) {
        const DWORD wait = WaitForSingleObject(h->worker, 5000);
        if (wait == WAIT_TIMEOUT) {
            // Do not free memory still referenced by the worker.  Leaking this
            // spike state is safer than a use-after-free on shutdown.
            SDL_Log("a3capture close timeout; worker detached, resources retained");
            return;
        }
        CloseHandle(h->worker);
        h->worker = NULL;
    }
    if (h->out) {
        SDL_RWclose(h->out);
        h->out = NULL;
    }
    if (h->ready_path) {
        DeleteFileA(h->ready_path);
    }
    SDL_Log("a3capture closed: generated=%llu queued=%llu written=%llu committed=%llu "
            "dropped=%llu late=%llu overflow=%llu failed=%d error='%s'",
            (unsigned long long)h->generated_frames,
            (unsigned long long)h->queued_frames,
            (unsigned long long)h->written_frames,
            (unsigned long long)h->committed_frames,
            (unsigned long long)h->dropped_frames,
            (unsigned long long)h->late_events,
            (unsigned long long)h->overflow_events,
            h->failed ? 1 : 0, h->write_error);
    SDL_DestroyCond(h->cond);
    SDL_DestroyMutex(h->lock);
    SDL_free(h->mixbuf);
    for (int i = 0; i < A3CAPTURE_QUEUE_SLOTS; ++i) {
        SDL_free(h->packets[i].data);
    }
    SDL_free(h->ready_path);
    SDL_free(h->out_path);
    SDL_free(h);
}

static int A3CAPTURE_OpenDevice(_THIS, const char *devname)
{
    struct SDL_PrivateAudioData *h;
    const char *env_path = SDL_getenv("A3CAPTURE_FILE");
    if (!env_path || !*env_path) {
        env_path = A3CAPTURE_DEFAULT_FILE;
    }
    // SDL_getenv returns a shared static buffer.  Copy before querying any
    // other environment variable, otherwise the test-delay lookup overwrites
    // this path pointer.
    char *path = SDL_strdup(env_path);
    if (!path) {
        return SDL_OutOfMemory();
    }
    if (_this->iscapture) {
        SDL_free(path);
        return SDL_SetError("a3capture has no capture devices");
    }
    SDL_Log("a3capture requested format=0x%x freq=%d channels=%u samples=%u size=%u",
            (unsigned)_this->spec.format, _this->spec.freq, (unsigned)_this->spec.channels,
            (unsigned)_this->spec.samples, (unsigned)_this->spec.size);
    if (_this->spec.format != AUDIO_S16LSB || _this->spec.freq != 44100 ||
        _this->spec.channels != 2 || _this->spec.samples == 0) {
        SDL_free(path);
        return SDL_SetError("a3capture requires s16le/44100/stereo");
    }

    h = (struct SDL_PrivateAudioData *)SDL_calloc(1, sizeof(*h));
    if (!h) {
        SDL_free(path);
        return SDL_OutOfMemory();
    }
    const char *test_delay = SDL_getenv("A3CAPTURE_TEST_WORKER_DELAY_MS");
    h->test_worker_delay_ms = test_delay ? (Uint32)SDL_atoi(test_delay) : 0;
    h->test_short_write = SDL_getenv("A3CAPTURE_TEST_SHORT_WRITE") ? SDL_TRUE : SDL_FALSE;
    h->test_no_thread = SDL_getenv("A3CAPTURE_TEST_NO_THREAD") ? SDL_TRUE : SDL_FALSE;
    h->sample_rate = _this->spec.freq;
    h->samples_per_callback = _this->spec.samples;
    h->payload_bytes = _this->spec.size;
    h->mixbuf = (Uint8 *)SDL_malloc(h->payload_bytes);
    if (!h->mixbuf) {
        SDL_free(path);
        SDL_free(h);
        return SDL_OutOfMemory();
    }
    SDL_memset(h->mixbuf, _this->spec.silence, h->payload_bytes);
    h->qpc_frequency = SDL_GetPerformanceFrequency();
    h->epoch_qpc = SDL_GetPerformanceCounter();
    h->next_deadline_qpc = h->epoch_qpc;
    h->out_path = path;
    const size_t ready_len = SDL_strlen(path) + 8; /* ".ready" + NUL */
    h->ready_path = (char *)SDL_malloc(ready_len);
    if (h->ready_path) {
        SDL_snprintf(h->ready_path, ready_len, "%s.ready", path);
    }
    h->lock = SDL_CreateMutex();
    h->cond = SDL_CreateCond();
    if (!h->out_path || !h->ready_path || !h->lock || !h->cond) {
        SDL_free(h->ready_path);
        SDL_free(h->out_path);
        SDL_DestroyCond(h->cond);
        SDL_DestroyMutex(h->lock);
        SDL_free(h->mixbuf);
        SDL_free(h);
        return SDL_OutOfMemory();
    }
    SDL_Log("a3capture opening file='%s'", path);
    h->out = SDL_RWFromFile(path, "wb");
    if (!h->out) {
        SDL_Log("a3capture file open failed path='%s'", path);
        SDL_SetError("cannot open A3CAPTURE_FILE");
        SDL_DestroyCond(h->cond);
        SDL_DestroyMutex(h->lock);
        SDL_free(h->ready_path);
        SDL_free(h->out_path);
        SDL_free(h->mixbuf);
        SDL_free(h);
        return -1;
    }

    for (int i = 0; i < A3CAPTURE_QUEUE_SLOTS; ++i) {
        h->packets[i].data = (Uint8 *)SDL_malloc(h->payload_bytes);
        if (!h->packets[i].data) {
            for (int j = 0; j < i; ++j) {
                SDL_free(h->packets[j].data);
            }
            SDL_RWclose(h->out);
            SDL_DestroyCond(h->cond);
            SDL_DestroyMutex(h->lock);
            SDL_free(h->ready_path);
            SDL_free(h->out_path);
            SDL_free(h->mixbuf);
            SDL_free(h);
            return SDL_OutOfMemory();
        }
        h->free_slots[i] = &h->packets[i];
    }
    h->free_count = A3CAPTURE_QUEUE_SLOTS;
    _this->hidden = h;

    if (!h->test_no_thread) {
        h->worker = CreateThread(NULL, 0, A3Worker, _this, 0, NULL);
        if (!h->worker) {
            A3CAPTURE_CloseDevice(_this);
            return -1;
        }
    }
    {
        SDL_RWops *ready = SDL_RWFromFile(h->ready_path, "wb");
        if (ready) {
            SDL_RWwrite(ready, "ready", 1, 5);
            SDL_RWclose(ready);
        } else {
            SDL_Log("a3capture ready marker open failed: %s", h->ready_path);
        }
    }
    SDL_Log("a3capture opened: file='%s' format=s16le rate=%u ch=%u samples=%u qpc_freq=%llu",
            path, h->sample_rate, _this->spec.channels, h->samples_per_callback,
            (unsigned long long)h->qpc_frequency);
    /* A2 does not yet emit framed a3-audio/1 packets. */
    return 0;
}

static void A3CAPTURE_DetectDevices(void)
{
    SDL_AddAudioDevice(SDL_FALSE, "a3capture", NULL, (void *)0x1);
}

static SDL_bool A3CAPTURE_Init(SDL_AudioDriverImpl *impl)
{
    impl->DetectDevices = A3CAPTURE_DetectDevices;
    impl->OpenDevice = A3CAPTURE_OpenDevice;
    impl->WaitDevice = A3CAPTURE_WaitDevice;
    impl->PlayDevice = A3CAPTURE_PlayDevice;
    impl->GetDeviceBuf = A3CAPTURE_GetDeviceBuf;
    impl->CloseDevice = A3CAPTURE_CloseDevice;
    impl->OnlyHasDefaultOutputDevice = SDL_TRUE;
    impl->SupportsNonPow2Samples = SDL_TRUE;
    return SDL_TRUE;
}

AudioBootStrap A3CAPTURE_bootstrap = {
    "a3capture", "auto-heros3 private capture (no playback)",
    A3CAPTURE_Init, SDL_TRUE
};

#endif /* SDL_AUDIO_DRIVER_A3CAPTURE */
