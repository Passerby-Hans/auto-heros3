#include "panel_source.hpp"
#include "../json_util.hpp"

#include <obs.h>
#include <obs-source.h>
#include <util/platform.h>

#include <windows.h>

#include <algorithm>
#include <atomic>
#include <cstddef>
#include <cstdio>
#include <cstring>
#include <cwctype>
#include <fstream>
#include <string>
#include <thread>
#include <vector>

namespace a3 {
namespace {

constexpr char kSourceId[] = "a3_status_panel";
constexpr int kWidth = 480;
constexpr int kHeight = 1080;

struct OverlayEntry {
    std::string kind;
    std::string ts;
    std::string text;
};

struct PanelData {
    obs_source_t* source = nullptr;
    std::string title = "AUTO-HEROS3";
    std::string session = "libobs";
    std::string overlay_path;
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

std::wstring Utf8ToWide(const std::string& s) {
    if (s.empty()) return L"";
    int n = MultiByteToWideChar(CP_UTF8, 0, s.data(), static_cast<int>(s.size()), nullptr, 0);
    if (n <= 0) return L"";
    std::wstring w(static_cast<size_t>(n), L'\0');
    MultiByteToWideChar(CP_UTF8, 0, s.data(), static_cast<int>(s.size()), &w[0], n);
    return w;
}

std::vector<OverlayEntry> ReadOverlay(const std::string& path, size_t keep = 8) {
    std::vector<OverlayEntry> entries;
    if (path.empty()) return entries;
    std::ifstream f(path, std::ios::binary);
    if (!f) return entries;
    std::string line;
    while (std::getline(f, line)) {
        if (line.empty()) continue;
        std::string kind, text, ts;
        bool found = false;
        json_extract_string(line, "kind", &kind, &found);
        json_extract_string(line, "text", &text, &found);
        json_extract_string(line, "ts", &ts, &found);
        if (text.empty()) continue;
        entries.push_back({kind, ts, text});
        if (entries.size() > 64) entries.erase(entries.begin());
    }
    if (entries.size() > keep)
        entries.erase(entries.begin(), entries.end() - static_cast<std::ptrdiff_t>(keep));
    return entries;
}

void DrawPanel(PanelData* p) {
    if (!p->dc || !p->bits) return;
    uint8_t* px = static_cast<uint8_t*>(p->bits);

    // Dark charcoal base with a subtle vertical gradient and an orange edge.
    for (int y = 0; y < kHeight; ++y) {
        const int v = 22 + (y * 10) / kHeight;
        for (int x = 0; x < kWidth; ++x) {
            uint8_t* q = px + (static_cast<size_t>(y) * kWidth + x) * 4;
            q[0] = static_cast<uint8_t>(v);
            q[1] = static_cast<uint8_t>(v + 2);
            q[2] = static_cast<uint8_t>(v + 4);
            q[3] = 255;
        }
    }
    RECT edge{0, 0, 5, kHeight};
    HBRUSH orange = CreateSolidBrush(RGB(214, 120, 42));
    FillRect(p->dc, &edge, orange);
    DeleteObject(orange);

    SetBkMode(p->dc, TRANSPARENT);
    HFONT h1 = CreateFontW(-34, 0, 0, 0, FW_BOLD, FALSE, FALSE, FALSE,
                           DEFAULT_CHARSET, OUT_DEFAULT_PRECIS, CLIP_DEFAULT_PRECIS,
                           CLEARTYPE_QUALITY, DEFAULT_PITCH, L"Segoe UI");
    HFONT h2 = CreateFontW(-19, 0, 0, 0, FW_NORMAL, FALSE, FALSE, FALSE,
                           DEFAULT_CHARSET, OUT_DEFAULT_PRECIS, CLIP_DEFAULT_PRECIS,
                           CLEARTYPE_QUALITY, DEFAULT_PITCH, L"Segoe UI");
    HFONT h3 = CreateFontW(-16, 0, 0, 0, FW_NORMAL, FALSE, FALSE, FALSE,
                           DEFAULT_CHARSET, OUT_DEFAULT_PRECIS, CLIP_DEFAULT_PRECIS,
                           CLEARTYPE_QUALITY, DEFAULT_PITCH, L"Consolas");
    HFONT h4 = CreateFontW(-15, 0, 0, 0, FW_NORMAL, FALSE, FALSE, FALSE,
                           DEFAULT_CHARSET, OUT_DEFAULT_PRECIS, CLIP_DEFAULT_PRECIS,
                           CLEARTYPE_QUALITY, DEFAULT_PITCH, L"Microsoft YaHei");
    auto text = [&](HFONT font, const std::wstring& s, int x, int y, int w, int h,
                    COLORREF color, UINT flags) {
        HGDIOBJ old = SelectObject(p->dc, font);
        SetTextColor(p->dc, color);
        RECT r{x, y, x + w, y + h};
        DrawTextW(p->dc, s.c_str(), -1, &r, flags | DT_NOPREFIX);
        SelectObject(p->dc, old);
    };

    text(h1, L"AUTO-HEROS3", 30, 42, 420, 48, RGB(255, 255, 255), DT_LEFT);
    text(h2, L"LIBOBS RECORDER  /  SELF-OWNED", 32, 96, 420, 28, RGB(160, 172, 184), DT_LEFT);

    RECT sep{32, 140, 448, 142};
    HBRUSH sepbrush = CreateSolidBrush(RGB(64, 70, 78));
    FillRect(p->dc, &sep, sepbrush);
    DeleteObject(sepbrush);

    const uint64_t elapsed = p->started_ns ? (os_gettime_ns() - p->started_ns) / 1000000000ULL : 0;
    wchar_t rec[64];
    std::swprintf(rec, 64, L"REC   %02llu:%02llu",
                  static_cast<unsigned long long>(elapsed / 60),
                  static_cast<unsigned long long>(elapsed % 60));
    text(h1, rec, 30, 164, 420, 48, RGB(232, 96, 72), DT_LEFT);

    text(h3, L"GAME 4:3  ·  1440x1080", 32, 236, 420, 24, RGB(185, 195, 205), DT_LEFT);
    text(h3, L"CANVAS 1920x1080 @60", 32, 264, 420, 24, RGB(185, 195, 205), DT_LEFT);
    text(h3, L"WGC / SDR  ·  game PCM", 32, 292, 420, 24, RGB(185, 195, 205), DT_LEFT);
    text(h3, L"no audio playback", 32, 320, 420, 24, RGB(120, 132, 144), DT_LEFT);

    RECT sep2{32, 362, 448, 364};
    HBRUSH sep2brush = CreateSolidBrush(RGB(60, 66, 74));
    FillRect(p->dc, &sep2, sep2brush);
    DeleteObject(sep2brush);

    text(h2, L"LLM  /  ACTIONS", 32, 380, 420, 30, RGB(235, 235, 235), DT_LEFT);
    text(h3, L"live from MCP overlay.jsonl", 32, 412, 420, 22, RGB(120, 132, 144), DT_LEFT);

    const std::vector<OverlayEntry> entries = ReadOverlay(p->overlay_path, 7);
    int y = 448;
    const int bottom = 1000;
    if (entries.empty()) {
        text(h4, L"(waiting for LLM thoughts/actions)", 32, y, 416, 60,
             RGB(150, 150, 150), DT_LEFT | DT_WORDBREAK);
    } else {
        for (auto it = entries.rbegin(); it != entries.rend(); ++it) {
            const OverlayEntry& e = *it;
            COLORREF label_color = RGB(180, 180, 180);
            std::wstring kind = Utf8ToWide(e.kind);
            std::transform(kind.begin(), kind.end(), kind.begin(),
                           [](wchar_t c) { return static_cast<wchar_t>(::towupper(c)); });
            if (e.kind == "thought") label_color = RGB(238, 170, 90);
            else if (e.kind == "action") label_color = RGB(120, 220, 150);
            else if (e.kind == "result") label_color = RGB(140, 180, 230);
            else if (e.kind == "event") label_color = RGB(180, 150, 210);
            std::wstring label = kind;
            if (!e.ts.empty()) label += L"  " + Utf8ToWide(e.ts);
            text(h3, label, 32, y, 416, 20, label_color, DT_LEFT);
            y += 20;
            std::wstring body = Utf8ToWide(e.text);
            RECT calc{32, y, 448, y + 200};
            HGDIOBJ old = SelectObject(p->dc, h4);
            DrawTextW(p->dc, body.c_str(), -1, &calc, DT_CALCRECT | DT_WORDBREAK | DT_NOPREFIX);
            SelectObject(p->dc, old);
            const int h = std::min(120, static_cast<int>(calc.bottom - calc.top) + 4);
            if (y + h > bottom) break;
            text(h4, body, 32, y, 416, h, RGB(235, 235, 235), DT_LEFT | DT_WORDBREAK);
            y += h + 10;
            if (y >= bottom) break;
        }
    }

    text(h3, L"SESSION", 32, 1008, 420, 20, RGB(120, 132, 144), DT_LEFT);
    std::wstring sess = Utf8ToWide(p->session);
    if (sess.size() > 34) sess = sess.substr(0, 34) + L"...";
    text(h3, sess, 32, 1028, 420, 20, RGB(190, 196, 204), DT_LEFT);
    text(h3, L"panel is part of the recorded frame", 30, 1052, 420, 20,
         RGB(100, 108, 118), DT_LEFT);

    DeleteObject(h1);
    DeleteObject(h2);
    DeleteObject(h3);
    DeleteObject(h4);

    // GDI text does not reliably preserve the alpha byte.  Force opaque pixels
    // so the composited panel is not erased as transparent over the black scene.
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
    if (settings) {
        const char* t = obs_data_get_string(settings, "title");
        const char* sess = obs_data_get_string(settings, "session");
        const char* overlay = obs_data_get_string(settings, "overlay_path");
        if (t && *t) p->title = t;
        if (sess && *sess) p->session = sess;
        if (overlay) p->overlay_path = overlay;
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
