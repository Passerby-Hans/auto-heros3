#include "panel_source.hpp"

#include <obs.h>
#include <obs-source.h>
#include <util/platform.h>

#include <windows.h>

#include <atomic>
#include <cstdio>
#include <cstring>
#include <string>
#include <thread>
#include <vector>

namespace a3 {
namespace {

constexpr char kSourceId[] = "a3_status_panel";
constexpr int kWidth = 480;
constexpr int kHeight = 1080;

struct PanelData {
    obs_source_t* source = nullptr;
    std::string title = "AUTO-HEROS3";
    std::string session = "libobs";
    std::vector<uint8_t> bgra;
    HDC dc = nullptr;
    HBITMAP bitmap = nullptr;
    HGDIOBJ old_bitmap = nullptr;
    void* bits = nullptr;
    std::thread thread;
    std::atomic<bool> stopping{false};
    uint64_t started_ns = 0;
};

std::atomic<PanelData*> g_panel{nullptr};

void DrawPanel(PanelData* p) {
    if (!p->dc || !p->bits) return;
    uint8_t* px = static_cast<uint8_t*>(p->bits);

    // Dark charcoal base with a subtle vertical gradient and an orange edge.
    for (int y = 0; y < kHeight; ++y) {
        const int v = 22 + (y * 10) / kHeight;
        for (int x = 0; x < kWidth; ++x) {
            uint8_t* q = px + (static_cast<size_t>(y) * kWidth + x) * 4;
            q[0] = static_cast<uint8_t>(v);       // B
            q[1] = static_cast<uint8_t>(v + 2);   // G
            q[2] = static_cast<uint8_t>(v + 4);   // R
            q[3] = 255;
        }
    }
    RECT edge{0, 0, 5, kHeight};
    HBRUSH orange = CreateSolidBrush(RGB(214, 120, 42));
    FillRect(p->dc, &edge, orange);
    DeleteObject(orange);

    SetBkMode(p->dc, TRANSPARENT);
    SetTextColor(p->dc, RGB(235, 235, 235));
    HFONT h1 = CreateFontW(-34, 0, 0, 0, FW_BOLD, FALSE, FALSE, FALSE,
                           DEFAULT_CHARSET, OUT_DEFAULT_PRECIS, CLIP_DEFAULT_PRECIS,
                           CLEARTYPE_QUALITY, DEFAULT_PITCH, L"Segoe UI");
    HFONT h2 = CreateFontW(-20, 0, 0, 0, FW_NORMAL, FALSE, FALSE, FALSE,
                           DEFAULT_CHARSET, OUT_DEFAULT_PRECIS, CLIP_DEFAULT_PRECIS,
                           CLEARTYPE_QUALITY, DEFAULT_PITCH, L"Segoe UI");
    HFONT h3 = CreateFontW(-17, 0, 0, 0, FW_NORMAL, FALSE, FALSE, FALSE,
                           DEFAULT_CHARSET, OUT_DEFAULT_PRECIS, CLIP_DEFAULT_PRECIS,
                           CLEARTYPE_QUALITY, DEFAULT_PITCH, L"Consolas");
    auto text = [&](HFONT font, const std::wstring& s, int x, int y, int w, int h,
                    COLORREF color, UINT flags) {
        HGDIOBJ old = SelectObject(p->dc, font);
        SetTextColor(p->dc, color);
        RECT r{x, y, x + w, y + h};
        DrawTextW(p->dc, s.c_str(), -1, &r, flags | DT_NOPREFIX);
        SelectObject(p->dc, old);
    };

    text(h1, L"AUTO-HEROS3", 30, 54, 420, 52, RGB(255, 255, 255), DT_LEFT);
    text(h2, L"LIBOBS RECORDER", 32, 112, 420, 30, RGB(170, 180, 190), DT_LEFT);
    text(h2, L"SELF-OWNED CAPTURE ENGINE", 32, 146, 420, 30, RGB(120, 132, 144), DT_LEFT);

    RECT sep{32, 204, 448, 206};
    HBRUSH sepbrush = CreateSolidBrush(RGB(64, 70, 78));
    FillRect(p->dc, &sep, sepbrush);
    DeleteObject(sepbrush);

    const uint64_t elapsed = p->started_ns ? (os_gettime_ns() - p->started_ns) / 1000000000ULL : 0;
    wchar_t rec[64];
    std::swprintf(rec, 64, L"REC   %02llu:%02llu",
                  static_cast<unsigned long long>(elapsed / 60),
                  static_cast<unsigned long long>(elapsed % 60));
    text(h1, rec, 30, 238, 420, 52, RGB(232, 96, 72), DT_LEFT);

    text(h3, L"GAME / 4:3", 32, 352, 420, 26, RGB(140, 150, 160), DT_LEFT);
    text(h2, L"1440 x 1080", 32, 382, 420, 34, RGB(235, 235, 235), DT_LEFT);
    text(h3, L"CANVAS", 32, 444, 420, 26, RGB(140, 150, 160), DT_LEFT);
    text(h2, L"1920 x 1080 @ 60", 32, 474, 420, 34, RGB(235, 235, 235), DT_LEFT);

    text(h3, L"CAPTURE", 32, 550, 420, 26, RGB(140, 150, 160), DT_LEFT);
    text(h2, L"WGC window / SDR", 32, 580, 420, 34, RGB(235, 235, 235), DT_LEFT);

    text(h3, L"AUDIO", 32, 656, 420, 26, RGB(140, 150, 160), DT_LEFT);
    text(h2, L"game PCM / no playback", 32, 686, 420, 34, RGB(235, 235, 235), DT_LEFT);

    text(h3, L"SESSION", 32, 772, 420, 26, RGB(140, 150, 160), DT_LEFT);
    std::wstring sess(p->session.begin(), p->session.end());
    if (sess.size() > 30) sess = sess.substr(0, 30) + L"...";
    text(h3, sess, 32, 802, 420, 26, RGB(220, 220, 220), DT_LEFT);

    text(h3, L"STATUS", 32, 890, 420, 26, RGB(140, 150, 160), DT_LEFT);
    text(h2, L"RECORDING", 32, 920, 420, 34, RGB(120, 220, 150), DT_LEFT);

    text(h3, L"panel is part of the recorded frame", 30, 1018, 420, 24,
         RGB(105, 115, 125), DT_LEFT);

    DeleteObject(h1);
    DeleteObject(h2);
    DeleteObject(h3);

    // GDI text does not reliably preserve the alpha byte.  The async BGRA frame
    // is composited over the black scene background; force opaque pixels so the
    // panel text is not erased as transparent.
    for (size_t i = 3; i < static_cast<size_t>(kWidth) * kHeight * 4; i += 4)
        px[i] = 255;
}

void PanelThread(PanelData* p) {
    while (!p->stopping.load(std::memory_order_acquire)) {
        DrawPanel(p);
        obs_source_frame frame = {};
        frame.data[0] = static_cast<uint8_t*>(p->bits);
        frame.linesize[0] = kWidth * 4;
        frame.width = kWidth;
        frame.height = kHeight;
        frame.format = VIDEO_FORMAT_BGRA;
        frame.full_range = true;
        frame.timestamp = os_gettime_ns();
        if (p->source) obs_source_output_video(p->source, &frame);
        for (int i = 0; i < 20 && !p->stopping.load(std::memory_order_acquire); ++i)
            std::this_thread::sleep_for(std::chrono::milliseconds(50));
    }
}

const char* PanelName(void*) { return "A3 Status Panel (S5b)"; }

void* PanelCreate(obs_data_t* settings, obs_source_t* source) {
    auto* p = new PanelData();
    p->source = source;
    p->bgra.assign(static_cast<size_t>(kWidth) * kHeight * 4, 0);
    if (obs_data_t* s = settings) {
        const char* t = obs_data_get_string(s, "title");
        const char* sess = obs_data_get_string(s, "session");
        if (t && *t) p->title = t;
        if (sess && *sess) p->session = sess;
    }

    BITMAPINFO bmi = {};
    bmi.bmiHeader.biSize = sizeof(BITMAPINFOHEADER);
    bmi.bmiHeader.biWidth = kWidth;
    bmi.bmiHeader.biHeight = -kHeight;
    bmi.bmiHeader.biPlanes = 1;
    bmi.bmiHeader.biBitCount = 32;
    bmi.bmiHeader.biCompression = BI_RGB;
    p->dc = CreateCompatibleDC(nullptr);
    p->bitmap = CreateDIBSection(p->dc, &bmi, DIB_RGB_COLORS, &p->bits, nullptr, 0);
    if (p->bitmap) p->old_bitmap = SelectObject(p->dc, p->bitmap);
    p->started_ns = os_gettime_ns();
    p->thread = std::thread(PanelThread, p);
    g_panel.store(p, std::memory_order_release);
    return p;
}

void PanelDestroy(void* data) {
    auto* p = static_cast<PanelData*>(data);
    if (!p) return;
    p->stopping.store(true, std::memory_order_release);
    if (p->thread.joinable()) p->thread.join();
    g_panel.store(nullptr, std::memory_order_release);
    if (p->dc && p->old_bitmap) SelectObject(p->dc, p->old_bitmap);
    if (p->bitmap) DeleteObject(p->bitmap);
    if (p->dc) DeleteDC(p->dc);
    delete p;
}

const obs_source_info kInfo = [] {
    obs_source_info info = {};
    info.id = kSourceId;
    info.type = OBS_SOURCE_TYPE_INPUT;
    info.output_flags = OBS_SOURCE_ASYNC_VIDEO;
    info.get_name = PanelName;
    info.create = PanelCreate;
    info.destroy = PanelDestroy;
    return info;
}();

} // namespace

void RegisterStatusPanelSource() {
    obs_register_source_s(&kInfo, sizeof(kInfo));
}

void StopStatusPanelSourceThread() {
    PanelData* p = g_panel.load(std::memory_order_acquire);
    if (!p) return;
    p->stopping.store(true, std::memory_order_release);
    if (p->thread.joinable()) p->thread.join();
    std::printf("PANEL thread_stopped\n");
    std::fflush(stdout);
}

} // namespace a3
