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
#include "a3_audio_protocol_c.h"

/* Use Windows kernel threads and SDL_RWops.  The private SDL2 DLL is linked
 * with /NODEFAULTLIB; pulling stdio/_beginthreadex into the backend would
 * create CRT imports that SDL's own build does not resolve. */
#include <windows.h>

/* SDL_sysaudio.h undefines _THIS at its end; re-establish the backend macro. */
#define _THIS SDL_AudioDevice *_this

#define A3CAPTURE_QUEUE_SLOTS 256
#define A3CAPTURE_DEFAULT_FILE "a3capture.raw"

typedef struct A3Packet
{
    A3AudioPacketHeader header;
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
    char *stop_path;
    A3Packet packets[A3CAPTURE_QUEUE_SLOTS];
    A3Packet *free_slots[A3CAPTURE_QUEUE_SLOTS];
    A3Packet *queue[A3CAPTURE_QUEUE_SLOTS];
    int free_count;
    int queue_head;
    int queue_count;
    Uint32 payload_bytes;
    Uint64 capture_id;
    Uint32 generation;
    Uint64 capture_epoch_qpc;
    SDL_bool sent_any;
    Uint64 pending_discontinuity_start;
    Uint64 pending_discontinuity_count;
    A3AudioPacketHeader final_header;
    SDL_bool final_requested;
    SDL_bool final_written;
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
    Uint32 test_close_delay_ms;
    SDL_bool test_short_write;
    SDL_bool test_no_thread;
    Uint8 *mixbuf;
};

static int A3Deadline(const struct SDL_PrivateAudioData *h, Uint64 frame, Uint64 *out)
{
    const a3_qpc_clock clock = {h->epoch_qpc, h->qpc_frequency, h->sample_rate};
    return a3_qpc_deadline(&clock, frame, out);
}

/* Called with h->lock held.  The final header is written by the worker after
 * it has drained all previously queued data packets. */
static void A3MakeFinalLocked(struct SDL_PrivateAudioData *h)
{
    if (h->final_requested || h->final_written) {
        return;
    }
    A3AudioPacketHeader fh = {0};
    const Uint64 now = SDL_GetPerformanceCounter();
    Uint64 planned = now;
    A3Deadline(h, h->generated_frames, &planned);
    memcpy(fh.magic, "A3AP", 4);
    fh.version = A3_AUDIO_WIRE_VERSION;
    fh.header_bytes = A3_AUDIO_HEADER_BYTES;
    fh.capture_id = h->capture_id;
    fh.generation = h->generation;
    fh.sequence = (Uint32)h->next_sequence++;
    fh.sample_rate = h->sample_rate;
    fh.channels = A3_AUDIO_CHANNELS_STEREO;
    fh.sample_format = A3_AUDIO_FORMAT_S16LE;
    fh.payload_bytes = 0;
    fh.sample_start = h->generated_frames;
    fh.sample_count = 0;
    fh.planned_qpc = planned;
    fh.actual_start_qpc = now;
    fh.actual_end_qpc = now;
    fh.qpc_frequency = h->qpc_frequency;
    fh.capture_epoch_qpc = h->capture_epoch_qpc;
    fh.flags |= A3_AUDIO_FLAG_LAST;
    if (!h->sent_any) {
        fh.flags |= A3_AUDIO_FLAG_FIRST;
    }
    if (h->pending_discontinuity_count > 0) {
        fh.flags |= A3_AUDIO_FLAG_DISCONTINUITY;
        fh.discontinuity_start = h->pending_discontinuity_start;
        fh.discontinuity_count = h->pending_discontinuity_count;
    }
    if (h->overflow_events > 0) {
        fh.flags |= A3_AUDIO_FLAG_QUEUE_OVERFLOW;
    }
    a3_audio_finalize_packet(&fh, NULL, 0);
    h->final_header = fh;
    h->final_requested = SDL_TRUE;
    h->stopping = SDL_TRUE;
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
            const Uint32 close_delay = h->test_close_delay_ms;
            A3AudioPacketHeader final_header = h->final_header;
            const SDL_bool final_requested = h->final_requested;
            SDL_UnlockMutex(h->lock);
            if (close_delay) {
                SDL_Delay(close_delay);
            }
            if (final_requested && !h->final_written) {
                const size_t wrote = SDL_RWwrite(h->out, &final_header, 1, sizeof(final_header));
                SDL_LockMutex(h->lock);
                if (wrote == sizeof(final_header)) {
                    h->final_written = SDL_TRUE;
                    ++h->written_packets;
                    h->written_bytes += sizeof(final_header);
                    SDL_Log("a3capture final written: generated=%llu queued=%llu "
                            "written=%llu committed=%llu dropped=%llu failed=%d",
                            (unsigned long long)h->generated_frames,
                            (unsigned long long)h->queued_frames,
                            (unsigned long long)h->written_frames,
                            (unsigned long long)h->committed_frames,
                            (unsigned long long)h->dropped_frames,
                            h->failed ? 1 : 0);
                } else {
                    h->failed = SDL_TRUE;
                    SDL_snprintf(h->write_error, sizeof(h->write_error),
                                 "short final header %llu/%llu",
                                 (unsigned long long)wrote,
                                 (unsigned long long)sizeof(final_header));
                }
                SDL_UnlockMutex(h->lock);
            }
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
            const size_t payload = (size_t)packet->header.sample_count * 2u * 2u;
            const size_t header_len = sizeof(packet->header);
            const size_t payload_write =
                (h->test_short_write && payload > 1) ? payload - 1 : payload;
            size_t header_wrote = 0;
            size_t payload_wrote = 0;
            if (h->out) {
                header_wrote = SDL_RWwrite(h->out, &packet->header, 1, header_len);
                if (header_wrote == header_len && payload_write > 0) {
                    payload_wrote = SDL_RWwrite(h->out, packet->data, 1, payload_write);
                }
            }
            SDL_LockMutex(h->lock);
            if (header_wrote == header_len && payload_wrote == payload) {
                ++h->written_packets;
                h->written_bytes += header_len + payload;
                h->written_frames += packet->header.sample_count;
                h->committed_frames = h->written_frames;
            } else {
                h->failed = SDL_TRUE;
                SDL_snprintf(h->write_error, sizeof(h->write_error),
                             "short packet header=%llu/%llu payload=%llu/%llu frame=%llu",
                             (unsigned long long)header_wrote,
                             (unsigned long long)header_len,
                             (unsigned long long)payload_wrote,
                             (unsigned long long)payload,
                             (unsigned long long)packet->header.sample_start);
            }
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
    if (h->final_requested || h->stopping) {
        /* The capture stream is closed; keep the SDL callback thread idle
         * without charging post-stop callbacks as lateness. */
        SDL_Delay(10);
        h->actual_start_qpc = SDL_GetPerformanceCounter();
        return;
    }
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
    if (h->final_requested || h->stopping) {
        SDL_UnlockMutex(h->lock);
        return;
    }
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
        if (h->pending_discontinuity_count == 0) {
            h->pending_discontinuity_start = frame_start;
            if (h->dropped_frames == 0) {
                h->first_dropped_frame = frame_start;
            }
        }
        h->pending_discontinuity_count += h->samples_per_callback;
        h->dropped_frames += h->samples_per_callback;
        h->failed = SDL_TRUE;
    }
    if (packet) {
        A3AudioPacketHeader hh = {0};
        memcpy(hh.magic, "A3AP", 4);
        hh.version = A3_AUDIO_WIRE_VERSION;
        hh.header_bytes = A3_AUDIO_HEADER_BYTES;
        hh.capture_id = h->capture_id;
        hh.generation = h->generation;
        hh.sequence = (Uint32)sequence;
        hh.sample_rate = h->sample_rate;
        hh.channels = A3_AUDIO_CHANNELS_STEREO;
        hh.sample_format = A3_AUDIO_FORMAT_S16LE;
        hh.payload_bytes = h->payload_bytes;
        hh.sample_start = frame_start;
        hh.sample_count = h->samples_per_callback;
        hh.planned_qpc = planned;
        hh.actual_start_qpc = h->callback_start_qpc ? h->callback_start_qpc : now;
        hh.actual_end_qpc = now;
        hh.qpc_frequency = h->qpc_frequency;
        hh.capture_epoch_qpc = h->capture_epoch_qpc;
        if (!h->sent_any) {
            hh.flags |= A3_AUDIO_FLAG_FIRST;
        }
        if (h->pending_discontinuity_count > 0) {
            hh.flags |= A3_AUDIO_FLAG_DISCONTINUITY;
            hh.discontinuity_start = h->pending_discontinuity_start;
            hh.discontinuity_count = h->pending_discontinuity_count;
        }
        if (h->overflow_events > 0) {
            hh.flags |= A3_AUDIO_FLAG_QUEUE_OVERFLOW;
        }
        memcpy(packet->data, h->mixbuf, h->payload_bytes);
        a3_audio_finalize_packet(&hh, packet->data, h->payload_bytes);
        packet->header = hh;
        h->queue[(h->queue_head + h->queue_count) % A3CAPTURE_QUEUE_SLOTS] = packet;
        ++h->queue_count;
        h->queued_frames += h->samples_per_callback;
        h->sent_any = SDL_TRUE;
        h->pending_discontinuity_start = 0;
        h->pending_discontinuity_count = 0;
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
    struct SDL_PrivateAudioData *h = _this->hidden;
    if (h->stop_path && GetFileAttributesA(h->stop_path) != INVALID_FILE_ATTRIBUTES) {
        SDL_LockMutex(h->lock);
        A3MakeFinalLocked(h);
        SDL_CondSignal(h->cond);
        SDL_UnlockMutex(h->lock);
    }
    h->callback_start_qpc = SDL_GetPerformanceCounter();
    return h->mixbuf;
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
    A3MakeFinalLocked(h);
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
    SDL_free(h->stop_path);
    SDL_free(h->ready_path);
    SDL_free(h->out_path);
    SDL_free(h);
}


/* A shared-read/write Windows file RWops.  The original SDL_RWFromFile("wb")
 * opens the file through the CRT with a share mode that denies readers; the
 * native recorder must follow the growing framed file while VCMI writes it. */
typedef struct A3WinFile {
    HANDLE handle;
} A3WinFile;

static Sint64 A3WinFileSize(SDL_RWops *rw)
{
    A3WinFile *f = (A3WinFile *)rw->hidden.unknown.data1;
    LARGE_INTEGER size;
    if (!f || !GetFileSizeEx(f->handle, &size)) return -1;
    return (Sint64)size.QuadPart;
}

static Sint64 A3WinFileSeek(SDL_RWops *rw, Sint64 offset, int whence)
{
    A3WinFile *f = (A3WinFile *)rw->hidden.unknown.data1;
    LARGE_INTEGER distance, result;
    DWORD move;
    if (!f) return -1;
    if (whence == RW_SEEK_CUR) move = FILE_CURRENT;
    else if (whence == RW_SEEK_END) move = FILE_END;
    else move = FILE_BEGIN;
    distance.QuadPart = offset;
    if (!SetFilePointerEx(f->handle, distance, &result, move)) return -1;
    return (Sint64)result.QuadPart;
}

static size_t A3WinFileRead(SDL_RWops *rw, void *ptr, size_t size, size_t maxnum)
{
    A3WinFile *f = (A3WinFile *)rw->hidden.unknown.data1;
    DWORD total = (DWORD)(size * maxnum), got = 0;
    if (!f || size == 0 || maxnum == 0) return 0;
    if (!ReadFile(f->handle, ptr, total, &got, NULL)) return 0;
    return got / size;
}

static size_t A3WinFileWrite(SDL_RWops *rw, const void *ptr, size_t size, size_t num)
{
    A3WinFile *f = (A3WinFile *)rw->hidden.unknown.data1;
    DWORD total = (DWORD)(size * num), wrote = 0;
    if (!f || size == 0 || num == 0) return 0;
    if (!WriteFile(f->handle, ptr, total, &wrote, NULL)) return 0;
    return wrote / size;
}

static int A3WinFileClose(SDL_RWops *rw)
{
    A3WinFile *f = (A3WinFile *)rw->hidden.unknown.data1;
    if (f) {
        CloseHandle(f->handle);
        SDL_free(f);
    }
    SDL_FreeRW(rw);
    return 0;
}

static SDL_RWops *A3WinFileOpen(const char *path)
{
    A3WinFile *f;
    SDL_RWops *rw;
    HANDLE h = CreateFileA(path, GENERIC_WRITE,
                           FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
                           NULL, CREATE_ALWAYS, FILE_ATTRIBUTE_NORMAL, NULL);
    if (h == INVALID_HANDLE_VALUE) return NULL;
    f = (A3WinFile *)SDL_calloc(1, sizeof(*f));
    rw = SDL_AllocRW();
    if (!f || !rw) {
        if (f) SDL_free(f);
        if (rw) SDL_FreeRW(rw);
        CloseHandle(h);
        return NULL;
    }
    f->handle = h;
    rw->size = A3WinFileSize;
    rw->seek = A3WinFileSeek;
    rw->read = A3WinFileRead;
    rw->write = A3WinFileWrite;
    rw->close = A3WinFileClose;
    rw->type = SDL_RWOPS_UNKNOWN;
    rw->hidden.unknown.data1 = f;
    return rw;
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
    const char *close_delay = SDL_getenv("A3CAPTURE_TEST_CLOSE_DELAY_MS");
    h->test_close_delay_ms = close_delay ? (Uint32)SDL_atoi(close_delay) : 0;
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
    h->capture_epoch_qpc = h->epoch_qpc;
    h->capture_id = h->epoch_qpc ^ ((Uint64)GetCurrentProcessId() << 32) ^
                    (Uint64)(size_t)h;
    h->generation = 1;
    h->next_deadline_qpc = h->epoch_qpc;
    h->out_path = path;
    const size_t ready_len = SDL_strlen(path) + 8; /* ".ready" + NUL */
    h->ready_path = (char *)SDL_malloc(ready_len);
    if (h->ready_path) {
        SDL_snprintf(h->ready_path, ready_len, "%s.ready", path);
    }
    const char *stop_path = SDL_getenv("A3CAPTURE_STOP_FILE");
    h->stop_path = (stop_path && *stop_path) ? SDL_strdup(stop_path) : NULL;
    h->lock = SDL_CreateMutex();
    h->cond = SDL_CreateCond();
    if (!h->out_path || !h->ready_path || !h->lock || !h->cond) {
        SDL_free(h->stop_path);
        SDL_free(h->ready_path);
        SDL_free(h->out_path);
        SDL_DestroyCond(h->cond);
        SDL_DestroyMutex(h->lock);
        SDL_free(h->mixbuf);
        SDL_free(h);
        return SDL_OutOfMemory();
    }
    SDL_Log("a3capture opening file='%s'", path);
    h->out = A3WinFileOpen(path);
    if (!h->out) {
        SDL_Log("a3capture file open failed path='%s'", path);
        SDL_SetError("cannot open A3CAPTURE_FILE");
        SDL_DestroyCond(h->cond);
        SDL_DestroyMutex(h->lock);
        SDL_free(h->stop_path);
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
            SDL_free(h->stop_path);
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
