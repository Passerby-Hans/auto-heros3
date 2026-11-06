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
    A3Packet packets[A3CAPTURE_QUEUE_SLOTS];
    A3Packet *free_slots[A3CAPTURE_QUEUE_SLOTS];
    A3Packet *queue[A3CAPTURE_QUEUE_SLOTS];
    int free_count;
    int queue_head;
    int queue_count;
    Uint32 payload_bytes;
    Uint64 next_sequence;
    Uint64 frames_written;
    Uint64 qpc_frequency;
    Uint64 epoch_qpc;
    Uint64 next_deadline_qpc;
    Uint64 planned_qpc;
    Uint64 actual_start_qpc;
    Uint32 sample_rate;
    Uint32 samples_per_callback;
    Uint64 late_events;
    Uint64 overflow_events;
    Uint64 written_packets;
    Uint64 written_bytes;
    Uint8 *mixbuf;
};

static Uint64 A3Deadline(const struct SDL_PrivateAudioData *h, Uint64 frame)
{
    const Uint64 q = frame / h->sample_rate;
    const Uint64 r = frame % h->sample_rate;
    return h->epoch_qpc + q * h->qpc_frequency +
           (r * h->qpc_frequency) / h->sample_rate;
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
            const size_t expected = (size_t)packet->frames * 2u * 2u;
            if (h->out && expected > 0) {
                const size_t wrote = SDL_RWwrite(h->out, packet->data, 1, expected);
                if (wrote == expected) {
                    ++h->written_packets;
                    h->written_bytes += expected;
                }
            }
            SDL_LockMutex(h->lock);
            h->free_slots[h->free_count++] = packet;
            SDL_UnlockMutex(h->lock);
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
    h->planned_qpc = h->next_deadline_qpc;
    const Uint64 now = SDL_GetPerformanceCounter();
    if (now < h->next_deadline_qpc) {
        const Uint64 remain = h->next_deadline_qpc - now;
        Uint64 ms = remain * 1000ULL / h->qpc_frequency;
        if (ms > 0) {
            SDL_Delay((Uint32)(ms > 50 ? 50 : ms));
        }
        while (SDL_GetPerformanceCounter() < h->next_deadline_qpc) {
            SDL_Delay(0);
        }
    } else if (now > h->next_deadline_qpc) {
        const Uint64 late = now - h->next_deadline_qpc;
        if (late > h->qpc_frequency / 1000ULL) {
            ++h->late_events;
        }
    }
    h->actual_start_qpc = SDL_GetPerformanceCounter();
}

static void A3CAPTURE_PlayDevice(_THIS)
{
    struct SDL_PrivateAudioData *h = _this->hidden;
    A3Packet *packet = NULL;

    SDL_LockMutex(h->lock);
    if (h->free_count > 0) {
        packet = h->free_slots[--h->free_count];
    } else {
        ++h->overflow_events;
    }
    if (packet) {
        packet->sequence = h->next_sequence++;
        packet->frame_start = h->frames_written;
        packet->frames = h->samples_per_callback;
        packet->planned_qpc = h->planned_qpc;
        packet->actual_start_qpc = h->actual_start_qpc;
        packet->actual_end_qpc = SDL_GetPerformanceCounter();
        memcpy(packet->data, h->mixbuf, h->payload_bytes);
        h->queue[(h->queue_head + h->queue_count) % A3CAPTURE_QUEUE_SLOTS] = packet;
        ++h->queue_count;
        SDL_CondSignal(h->cond);
        h->frames_written += h->samples_per_callback;
        h->next_deadline_qpc = A3Deadline(h, h->frames_written);
    }
    SDL_UnlockMutex(h->lock);
}

static Uint8 *A3CAPTURE_GetDeviceBuf(_THIS)
{
    return _this->hidden->mixbuf;
}

static void A3CAPTURE_CloseDevice(_THIS)
{
    struct SDL_PrivateAudioData *h = _this->hidden;
    if (!h) {
        return;
    }
    SDL_LockMutex(h->lock);
    h->stopping = SDL_TRUE;
    SDL_CondSignal(h->cond);
    SDL_UnlockMutex(h->lock);
    if (h->worker) {
        WaitForSingleObject(h->worker, INFINITE);
        CloseHandle(h->worker);
    }
    if (h->out) {
        SDL_RWclose(h->out);
    }
    SDL_Log("a3capture closed: packets=%llu bytes=%llu late=%llu overflow=%llu",
            (unsigned long long)h->written_packets,
            (unsigned long long)h->written_bytes,
            (unsigned long long)h->late_events,
            (unsigned long long)h->overflow_events);
    SDL_DestroyCond(h->cond);
    SDL_DestroyMutex(h->lock);
    SDL_free(h->mixbuf);
    for (int i = 0; i < A3CAPTURE_QUEUE_SLOTS; ++i) {
        SDL_free(h->packets[i].data);
    }
    SDL_free(h->out_path);
    SDL_free(h);
}

static int A3CAPTURE_OpenDevice(_THIS, const char *devname)
{
    struct SDL_PrivateAudioData *h;
    const char *path = SDL_getenv("A3CAPTURE_FILE");
    if (!path || !*path) {
        path = A3CAPTURE_DEFAULT_FILE;
    }
    if (_this->iscapture) {
        return SDL_SetError("a3capture has no capture devices");
    }
    SDL_Log("a3capture requested format=0x%x freq=%d channels=%u samples=%u size=%u",
            (unsigned)_this->spec.format, _this->spec.freq, (unsigned)_this->spec.channels,
            (unsigned)_this->spec.samples, (unsigned)_this->spec.size);
    if (_this->spec.format != AUDIO_S16LSB || _this->spec.freq != 44100 ||
        _this->spec.channels != 2 || _this->spec.samples == 0) {
        return SDL_SetError("a3capture requires s16le/44100/stereo");
    }

    h = (struct SDL_PrivateAudioData *)SDL_calloc(1, sizeof(*h));
    if (!h) {
        return SDL_OutOfMemory();
    }
    h->sample_rate = _this->spec.freq;
    h->samples_per_callback = _this->spec.samples;
    h->payload_bytes = _this->spec.size;
    h->mixbuf = (Uint8 *)SDL_malloc(h->payload_bytes);
    if (!h->mixbuf) {
        SDL_free(h);
        return SDL_OutOfMemory();
    }
    SDL_memset(h->mixbuf, _this->spec.silence, h->payload_bytes);
    h->qpc_frequency = SDL_GetPerformanceFrequency();
    h->epoch_qpc = SDL_GetPerformanceCounter();
    h->next_deadline_qpc = h->epoch_qpc;
    h->out_path = SDL_strdup(path);
    h->lock = SDL_CreateMutex();
    h->cond = SDL_CreateCond();
    if (!h->out_path || !h->lock || !h->cond) {
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
            SDL_free(h->out_path);
            SDL_free(h->mixbuf);
            SDL_free(h);
            return SDL_OutOfMemory();
        }
        h->free_slots[i] = &h->packets[i];
    }
    h->free_count = A3CAPTURE_QUEUE_SLOTS;
    _this->hidden = h;

    h->worker = CreateThread(NULL, 0, A3Worker, _this, 0, NULL);
    if (!h->worker) {
        A3CAPTURE_CloseDevice(_this);
        return -1;
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
