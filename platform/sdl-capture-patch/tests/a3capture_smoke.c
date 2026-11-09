/*
 * A2 load/format smoke.  Loads the private SDL2.dll explicitly (not the
 * system/Conan DLL), selects the a3capture driver, opens 44.1k s16 stereo,
 * runs one second of a callback and checks that the backend produced PCM.
 */
#include <windows.h>
#include <stdio.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>

typedef uint8_t Uint8;
typedef uint16_t Uint16;
typedef uint32_t Uint32;
typedef uint32_t SDL_AudioDeviceID;

typedef void (*SDL_AudioCallback)(void *, Uint8 *, int);
typedef struct {
    int freq;
    Uint16 format;
    Uint8 channels;
    Uint8 silence;
    Uint16 samples;
    Uint16 padding;
    Uint32 size;
    SDL_AudioCallback callback;
    void *userdata;
} SDL_AudioSpec;
typedef struct { Uint8 major, minor, patch; } SDL_version;

typedef int (*PFN_SDL_Init)(Uint32);
typedef void (*PFN_SDL_Quit)(void);
typedef const char *(*PFN_SDL_GetCurrentAudioDriver)(void);
typedef const char *(*PFN_SDL_GetError)(void);
typedef void (*PFN_SDL_GetVersion)(SDL_version *);
typedef SDL_AudioDeviceID (*PFN_SDL_OpenAudioDevice)(
    const char *, int, const SDL_AudioSpec *, SDL_AudioSpec *, int);
typedef void (*PFN_SDL_PauseAudioDevice)(SDL_AudioDeviceID, int);
typedef void (*PFN_SDL_CloseAudioDevice)(SDL_AudioDeviceID);

#define SDL_INIT_AUDIO 0x00000010u
#define AUDIO_S16LSB 0x8010u
#define SDL_AUDIO_ALLOW_FREQUENCY_CHANGE 0x00000001
#define SDL_AUDIO_ALLOW_CHANNELS_CHANGE 0x00000002

static int g_failed = 0;
static void check(const char *name, int ok, const char *detail)
{
    printf("%s %s%s%s\n", ok ? "PASS" : "FAIL", name,
           detail && *detail ? "  " : "", detail ? detail : "");
    if (!ok) ++g_failed;
}

static void audio_cb(void *userdata, Uint8 *stream, int len)
{
    (void)userdata;
    int16_t *p = (int16_t *)stream;
    int frames = len / 4;
    static double phase = 0.0;
    for (int i = 0; i < frames; ++i) {
        short v = (short)(3000.0 * (phase < 3.141592653589793 ? 1.0 : -1.0));
        p[i * 2] = v;
        p[i * 2 + 1] = v;
        phase += 2.0 * 3.141592653589793 * 440.0 / 44100.0;
        if (phase > 6.283185307179586) phase -= 6.283185307179586;
    }
}

int main(int argc, char **argv)
{
    setvbuf(stdout, NULL, _IONBF, 0);
    setvbuf(stderr, NULL, _IONBF, 0);
    if (argc < 3) {
        fprintf(stderr, "usage: a3capture_smoke <SDL2.dll> <output.raw>\n");
        return 2;
    }
    wchar_t dll_w[MAX_PATH];
    MultiByteToWideChar(CP_UTF8, 0, argv[1], -1, dll_w, MAX_PATH);
    HMODULE h = LoadLibraryW(dll_w);
    if (!h) {
        fprintf(stderr, "LoadLibrary failed: %lu\n", GetLastError());
        return 3;
    }
    PFN_SDL_Init init = (PFN_SDL_Init)GetProcAddress(h, "SDL_Init");
    PFN_SDL_Quit quit = (PFN_SDL_Quit)GetProcAddress(h, "SDL_Quit");
    PFN_SDL_GetCurrentAudioDriver current =
        (PFN_SDL_GetCurrentAudioDriver)GetProcAddress(h, "SDL_GetCurrentAudioDriver");
    PFN_SDL_GetError error = (PFN_SDL_GetError)GetProcAddress(h, "SDL_GetError");
    PFN_SDL_GetVersion getver = (PFN_SDL_GetVersion)GetProcAddress(h, "SDL_GetVersion");
    PFN_SDL_OpenAudioDevice open =
        (PFN_SDL_OpenAudioDevice)GetProcAddress(h, "SDL_OpenAudioDevice");
    PFN_SDL_PauseAudioDevice pause =
        (PFN_SDL_PauseAudioDevice)GetProcAddress(h, "SDL_PauseAudioDevice");
    PFN_SDL_CloseAudioDevice close =
        (PFN_SDL_CloseAudioDevice)GetProcAddress(h, "SDL_CloseAudioDevice");
    check("symbols", init && quit && current && open && pause && close && error && getver, "");
    if (g_failed) return 4;

    printf("ARGV2=%s ARGV3=%s\n", argv[2], argc > 3 ? argv[3] : "(none)");
    SetEnvironmentVariableA("SDL_AUDIODRIVER", "a3capture");
    SetEnvironmentVariableA("A3CAPTURE_FILE", argv[2]);
    printf("ENV_A3CAPTURE_FILE=%s\n", getenv("A3CAPTURE_FILE") ? getenv("A3CAPTURE_FILE") : "(null)");
    if (init(SDL_INIT_AUDIO) != 0) {
        printf("SDL_Init failed: %s\n", error ? error() : "?");
        return 5;
    }
    SDL_version ver = {0, 0, 0};
    getver(&ver);
    const char *driver_ptr = current();
    char driver[64] = "";
    if (driver_ptr) snprintf(driver, sizeof(driver), "%s", driver_ptr);
    check("driver_is_a3capture", driver_ptr && strcmp(driver_ptr, "a3capture") == 0,
          driver_ptr ? driver_ptr : "(null)");

    SDL_AudioSpec want = {0}, have = {0};
    want.format = AUDIO_S16LSB;
    want.freq = 44100;
    want.channels = 2;
    want.samples = 1024;
    want.callback = audio_cb;
    printf("STEP opening audio device\n");
    SDL_AudioDeviceID dev = open(NULL, 0, &want, &have, 0);
    if (!dev) {
        printf("SDL_OpenAudioDevice failed: %s\n", error ? error() : "?");
        quit();
        return 6;
    }
    printf("STEP opening done\n");
    char spec[256];
    snprintf(spec, sizeof(spec), "freq=%d channels=%u samples=%u size=%u",
             have.freq, have.channels, have.samples, have.size);
    check("format_44100_s16_stereo", have.freq == 44100 && have.channels == 2 &&
          have.format == AUDIO_S16LSB && have.samples == 1024, spec);
    int run_ms = 1200;
    if (argc > 3) {
        run_ms = atoi(argv[3]);
        if (run_ms < 200) run_ms = 200;
    }
    printf("STEP unpausing (%d ms)\n", run_ms);
    pause(dev, 0);
    Sleep((DWORD)run_ms);
    printf("STEP closing device\n");
    pause(dev, 1);
    close(dev);
    printf("STEP device closed; quitting SDL\n");
    quit();
    printf("STEP SDL quit; freeing DLL\n");
    FreeLibrary(h);
    printf("STEP DLL freed\n");

    const int pipe_mode = getenv("A3CAPTURE_TEST_PIPE") != NULL;
    FILE *f = pipe_mode ? NULL : fopen(argv[2], "rb");
    int64_t bytes = 0;
    unsigned char header[128] = {0};
    size_t header_read = 0;
    if (f) {
        _fseeki64(f, 0, SEEK_END);
        bytes = _ftelli64(f);
        _fseeki64(f, 0, SEEK_SET);
        header_read = fread(header, 1, sizeof(header), f);
        fclose(f);
    }
    char detail[160];
    const int64_t expected = (int64_t)run_ms * 44100 * 4 / 1000;
    snprintf(detail, sizeof(detail), "bytes=%lld expected~%lld", (long long)bytes, (long long)expected);
    if (header_read >= 128) {
        const unsigned char *b = header;
        check("framed_magic", b[0] == 'A' && b[1] == '3' && b[2] == 'A' && b[3] == 'P',
              "first bytes are not A3AP");
        check("framed_header_format",
              b[4] == 1 && b[5] == 0 && b[6] == 0x80 && b[7] == 0,
              "unexpected version/header size");
    }
    if (getenv("A3CAPTURE_TEST_PIPE")) {
        check("pipe_mode_skip_file_check", 1, "named-pipe relay owns the framed stream");
    } else if (getenv("A3CAPTURE_TEST_NO_PCM")) {
        check("no_pcm_expected", 1, "fault mode intentionally leaves no complete file");
    } else if (!getenv("A3CAPTURE_TEST_NO_THREAD")) {
        check("pcm_written", bytes > 0, detail);
        if (!getenv("A3CAPTURE_TEST_FAULT")) {
            check("pcm_rate_bounds",
                  bytes >= expected * 95L / 100L && bytes <= expected * 105L / 100L, detail);
        }
    } else {
        check("no_thread_close_survived", 1, "no worker by design");
    }
    printf("DLL_VERSION %u.%u.%u\n", ver.major, ver.minor, ver.patch);
    printf("DRIVER %s\n", driver ? driver : "");
    printf("AUDIO %s\n", spec);
    printf("RESULT %s\n", g_failed == 0 ? "PASS" : "FAIL");
    return g_failed == 0 ? 0 : 7;
}
