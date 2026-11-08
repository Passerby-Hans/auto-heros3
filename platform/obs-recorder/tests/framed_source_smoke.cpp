#include "framed_audio_source.hpp"

#include <obs.h>
#include <util/platform.h>

#include <chrono>
#include <cstdio>
#include <thread>

int main(int argc, char** argv)
{
    if (argc < 2) {
        std::fprintf(stderr, "usage: framed-source-smoke PATH\n");
        return 2;
    }
    if (!obs_startup("en-US", nullptr, nullptr)) {
        std::fprintf(stderr, "obs_startup failed\n");
        return 2;
    }
    obs_audio_info2 ai = {};
    ai.samples_per_sec = 44100;
    ai.speakers = SPEAKERS_STEREO;
    ai.max_buffering_ms = 1000;
    ai.fixed_buffering = true;
    if (!obs_reset_audio2(&ai)) {
        std::fprintf(stderr, "obs_reset_audio2 failed\n");
        obs_shutdown();
        return 3;
    }
    a3::RegisterFramedAudioSource();
    obs_data_t* settings = obs_data_create();
    obs_data_set_string(settings, "path", argv[1]);
    obs_data_set_bool(settings, "live", false);
    obs_data_set_bool(settings, "start_at_zero", true);
    obs_data_set_int(settings, "replay_origin_ns", static_cast<long long>(os_gettime_ns()));
    obs_source_t* source = obs_source_create("a3_framed_audio", "framed-source-smoke",
                                              settings, nullptr);
    obs_data_release(settings);
    if (!source) {
        std::fprintf(stderr, "source create failed\n");
        obs_shutdown();
        return 4;
    }
    a3::FramedAudioStats st;
    for (int i = 0; i < 500; ++i) {
        st = a3::GetFramedAudioStats();
        if (st.error_code.size() || st.saw_last || st.submitted_frames > 0) break;
        std::this_thread::sleep_for(std::chrono::milliseconds(20));
    }
    // Give replay pacing up to the source duration to finish the LAST packet.
    for (int i = 0; i < 500 && !st.saw_last && st.error_code.empty(); ++i) {
        st = a3::GetFramedAudioStats();
        std::this_thread::sleep_for(std::chrono::milliseconds(20));
    }
    obs_source_release(source);
    std::printf("FRAMED_SOURCE opened=%d validation=%d packet_ok=%d stream_ok=%d "
                "validated=%llu submitted=%llu frames=%llu saw_first=%d saw_last=%d "
                "error_code='%s' error='%s'\n",
                st.opened ? 1 : 0, st.validation_performed ? 1 : 0,
                st.packet_integrity_ok ? 1 : 0, st.stream_integrity_ok ? 1 : 0,
                (unsigned long long)st.validated_packets,
                (unsigned long long)st.submitted_packets,
                (unsigned long long)st.submitted_frames,
                st.saw_first ? 1 : 0, st.saw_last ? 1 : 0,
                st.error_code.c_str(), st.error.c_str());
    const bool ok = st.opened && st.validation_performed && st.packet_integrity_ok &&
                    st.stream_integrity_ok && st.submitted_frames > 0 &&
                    st.saw_last && st.error_code.empty();
    obs_shutdown();
    return ok ? 0 : 1;
}
