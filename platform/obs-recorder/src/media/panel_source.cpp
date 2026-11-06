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

std::vector<OverlayEntry> ReadOverlay(const std::string& path, size_t keep = 7) {
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

COLORREF KindColor(const std::string& kind) {
    if (kind == "thought") return RGB(240, 181, 99);
    if (kind == "action") return RGB(119, 216, 154);
    if (kind == "result") return RGB(135, 181, 236);
    if (kind == "event") return RGB(194, 158, 224);
    return RGB(170, 178, 190);
}

std::wstring KindLabel(const std::string& kind) {
    if (kind == "thought") return L"思考";
    if (kind == "action") return L"操作";
    if (kind == "result") return L"结果";
    if (kind == "event") return L"事件";
    return Utf8ToWide(kind);
}

void FillRoundRect(HDC dc, RECT r, int radius, COLORREF fill, COLORREF border) {
    HBRUSH brush = CreateSolidBrush(fill);
    HPEN pen = CreatePen(PS_SOLID, 1, border);
    HGDIOBJ old_brush = SelectObject(dc, brush);
    HGDIOBJ old_pen = SelectObject(dc, pen);
    RoundRect(dc, r.left, r.top, r.right, r.bottom, radius, radius);
    SelectObject(dc, old_brush);
    SelectObject(dc, old_pen);
    DeleteObject(brush);
    DeleteObject(pen);
}

void FillRectColor(HDC dc, RECT r, COLORREF color) {
    HBRUSH brush = CreateSolidBrush(color);
    FillRect(dc, &r, brush);
    DeleteObject(brush);
}

void DrawPanelLegacy(PanelData* p) {
    if (!p->dc || !p->bits) return;
    uint8_t* px = static_cast<uint8_t*>(p->bits);

    // 深色渐变底；游戏画面仍是主体，侧栏只负责解释与陪伴。
    for (int y = 0; y < kHeight; ++y) {
        const int v = 18 + (y * 14) / kHeight;
        for (int x = 0; x < kWidth; ++x) {
            uint8_t* q = px + (static_cast<size_t>(y) * kWidth + x) * 4;
            q[0] = static_cast<uint8_t>(v);
            q[1] = static_cast<uint8_t>(v + 3);
            q[2] = static_cast<uint8_t>(v + 7);
            q[3] = 255;
        }
    }

    RECT accent{0, 0, 4, kHeight};
    FillRectColor(p->dc, accent, RGB(210, 112, 44));
    RECT accent_hi{4, 0, 6, kHeight};
    FillRectColor(p->dc, accent_hi, RGB(84, 52, 34));

    SetBkMode(p->dc, TRANSPARENT);

    HFONT h_brand = CreateFontW(-31, 0, 0, 0, FW_BOLD, FALSE, FALSE, FALSE,
                                DEFAULT_CHARSET, OUT_DEFAULT_PRECIS, CLIP_DEFAULT_PRECIS,
                                CLEARTYPE_QUALITY, DEFAULT_PITCH, L"Microsoft YaHei UI");
    HFONT h_sub = CreateFontW(-15, 0, 0, 0, FW_NORMAL, FALSE, FALSE, FALSE,
                              DEFAULT_CHARSET, OUT_DEFAULT_PRECIS, CLIP_DEFAULT_PRECIS,
                              CLEARTYPE_QUALITY, DEFAULT_PITCH, L"Microsoft YaHei UI");
    HFONT h_section = CreateFontW(-20, 0, 0, 0, FW_BOLD, FALSE, FALSE, FALSE,
                                  DEFAULT_CHARSET, OUT_DEFAULT_PRECIS, CLIP_DEFAULT_PRECIS,
                                  CLEARTYPE_QUALITY, DEFAULT_PITCH, L"Microsoft YaHei UI");
    HFONT h_timer = CreateFontW(-24, 0, 0, 0, FW_BOLD, FALSE, FALSE, FALSE,
                                DEFAULT_CHARSET, OUT_DEFAULT_PRECIS, CLIP_DEFAULT_PRECIS,
                                CLEARTYPE_QUALITY, DEFAULT_PITCH, L"Consolas");
    HFONT h_label = CreateFontW(-13, 0, 0, 0, FW_NORMAL, FALSE, FALSE, FALSE,
                                DEFAULT_CHARSET, OUT_DEFAULT_PRECIS, CLIP_DEFAULT_PRECIS,
                                CLEARTYPE_QUALITY, DEFAULT_PITCH, L"Microsoft YaHei UI");
    HFONT h_value = CreateFontW(-16, 0, 0, 0, FW_NORMAL, FALSE, FALSE, FALSE,
                                DEFAULT_CHARSET, OUT_DEFAULT_PRECIS, CLIP_DEFAULT_PRECIS,
                                CLEARTYPE_QUALITY, DEFAULT_PITCH, L"Microsoft YaHei UI");
    HFONT h_body = CreateFontW(-15, 0, 0, 0, FW_NORMAL, FALSE, FALSE, FALSE,
                               DEFAULT_CHARSET, OUT_DEFAULT_PRECIS, CLIP_DEFAULT_PRECIS,
                               CLEARTYPE_QUALITY, DEFAULT_PITCH, L"Microsoft YaHei UI");

    auto text = [&](HFONT font, const std::wstring& s, int x, int y, int w, int h,
                    COLORREF color, UINT flags) {
        HGDIOBJ old = SelectObject(p->dc, font);
        SetTextColor(p->dc, color);
        RECT r{x, y, x + w, y + h};
        DrawTextW(p->dc, s.c_str(), -1, &r, flags | DT_NOPREFIX);
        SelectObject(p->dc, old);
    };

    text(h_brand, L"AUTO-HEROS3", 28, 28, 424, 46, RGB(248, 249, 251), DT_LEFT | DT_SINGLELINE);
    text(h_sub, L"智能体实况  ·  libobs 自有录制", 30, 74, 424, 24,
         RGB(154, 166, 182), DT_LEFT | DT_SINGLELINE);
    RECT top_rule{28, 108, 452, 109};
    FillRectColor(p->dc, top_rule, RGB(49, 57, 69));

    FillRoundRect(p->dc, RECT{24, 124, 456, 204}, 10, RGB(28, 34, 44), RGB(52, 61, 74));
    RECT rec_bar{24, 124, 29, 204};
    FillRectColor(p->dc, rec_bar, RGB(224, 94, 79));
    HBRUSH dot = CreateSolidBrush(RGB(239, 100, 86));
    HGDIOBJ old_dot = SelectObject(p->dc, dot);
    Ellipse(p->dc, 42, 148, 54, 160);
    SelectObject(p->dc, old_dot);
    DeleteObject(dot);
    text(h_value, L"录制中", 62, 142, 120, 25, RGB(245, 246, 248), DT_LEFT | DT_SINGLELINE);
    text(h_label, L"REC  ·  libobs / WGC", 62, 170, 180, 20,
         RGB(148, 158, 174), DT_LEFT | DT_SINGLELINE);

    const uint64_t elapsed = p->started_ns ? (os_gettime_ns() - p->started_ns) / 1000000000ULL : 0;
    wchar_t rec[64];
    std::swprintf(rec, 64, L"%02llu:%02llu",
                  static_cast<unsigned long long>(elapsed / 60),
                  static_cast<unsigned long long>(elapsed % 60));
    text(h_timer, rec, 300, 145, 136, 34, RGB(242, 126, 108), DT_RIGHT | DT_SINGLELINE);

    FillRoundRect(p->dc, RECT{24, 220, 456, 374}, 10, RGB(25, 31, 41), RGB(47, 55, 68));
    text(h_label, L"画面布局", 42, 238, 92, 20, RGB(132, 143, 160), DT_LEFT | DT_SINGLELINE);
    text(h_value, L"4:3 游戏  ·  1440 × 1080", 144, 234, 292, 24,
         RGB(224, 228, 234), DT_LEFT | DT_SINGLELINE);
    text(h_label, L"输出画布", 42, 270, 92, 20, RGB(132, 143, 160), DT_LEFT | DT_SINGLELINE);
    text(h_value, L"1920 × 1080  ·  60 fps", 144, 266, 292, 24,
         RGB(224, 228, 234), DT_LEFT | DT_SINGLELINE);
    text(h_label, L"捕获方式", 42, 302, 92, 20, RGB(132, 143, 160), DT_LEFT | DT_SINGLELINE);
    text(h_value, L"WGC · SDR  ·  PCM 音轨", 144, 298, 292, 24,
         RGB(224, 228, 234), DT_LEFT | DT_SINGLELINE);
    text(h_label, L"物理播放", 42, 334, 92, 20, RGB(132, 143, 160), DT_LEFT | DT_SINGLELINE);
    text(h_value, L"关闭（不输出到扬声器）", 144, 330, 292, 24,
         RGB(164, 174, 188), DT_LEFT | DT_SINGLELINE);

    RECT section_bar{28, 395, 33, 421};
    FillRectColor(p->dc, section_bar, RGB(224, 126, 58));
    text(h_section, L"LLM 思考与操作", 46, 390, 300, 32,
         RGB(246, 247, 249), DT_LEFT | DT_SINGLELINE);
    FillRoundRect(p->dc, RECT{368, 394, 450, 420}, 12, RGB(43, 51, 63), RGB(61, 70, 84));
    text(h_label, L"实时", 382, 397, 56, 18, RGB(165, 208, 177), DT_LEFT | DT_SINGLELINE);
    text(h_label, L"来自 MCP 的思考、动作与结果", 30, 432, 424, 20,
         RGB(132, 143, 160), DT_LEFT | DT_SINGLELINE);

    const std::vector<OverlayEntry> entries = ReadOverlay(p->overlay_path, 7);
    int y = 464;
    const int bottom = 990;
    if (entries.empty()) {
        FillRoundRect(p->dc, RECT{24, y, 456, y + 92}, 10, RGB(25, 31, 41), RGB(47, 55, 68));
        text(h_value, L"等待 LLM 思考与操作…", 42, y + 22, 392, 28,
             RGB(160, 170, 184), DT_LEFT | DT_SINGLELINE);
        text(h_label, L"游戏开始后，这里会实时滚动显示本局指令。", 42, y + 54, 392, 20,
             RGB(112, 124, 140), DT_LEFT | DT_SINGLELINE);
    } else {
        for (auto it = entries.rbegin(); it != entries.rend(); ++it) {
            const OverlayEntry& e = *it;
            std::wstring body = Utf8ToWide(e.text);
            if (body.size() > 260) body = body.substr(0, 260) + L"…";

            RECT calc{0, 0, 388, 0};
            HGDIOBJ old = SelectObject(p->dc, h_body);
            DrawTextW(p->dc, body.c_str(), -1, &calc,
                      DT_CALCRECT | DT_WORDBREAK | DT_NOPREFIX);
            SelectObject(p->dc, old);
            int body_h = static_cast<int>(calc.bottom - calc.top);
            body_h = std::max(20, std::min(128, body_h + 4));
            const int entry_h = 24 + body_h + 14;
            if (y + entry_h > bottom) break;

            RECT marker{28, y + 2, 31, y + 22};
            FillRectColor(p->dc, marker, KindColor(e.kind));
            text(h_label, KindLabel(e.kind), 40, y, 120, 20,
                 KindColor(e.kind), DT_LEFT | DT_SINGLELINE);
            if (!e.ts.empty()) {
                text(h_label, Utf8ToWide(e.ts), 320, y, 120, 20,
                     RGB(106, 118, 134), DT_RIGHT | DT_SINGLELINE);
            }
            text(h_body, body, 40, y + 24, 388, body_h, RGB(222, 227, 234),
                 DT_LEFT | DT_WORDBREAK);
            y += entry_h;
            if (y < bottom - 6) {
                RECT rule{40, y - 7, 448, y - 6};
                FillRectColor(p->dc, rule, RGB(42, 49, 60));
            }
        }
    }

    RECT bottom_rule{28, 1002, 452, 1003};
    FillRectColor(p->dc, bottom_rule, RGB(43, 51, 63));
    text(h_label, L"会话", 30, 1014, 38, 20, RGB(108, 120, 136), DT_LEFT | DT_SINGLELINE);
    std::wstring sess = Utf8ToWide(p->session);
    if (sess.size() > 42) sess = sess.substr(0, 42) + L"…";
    text(h_label, sess, 72, 1014, 376, 20, RGB(168, 177, 190), DT_LEFT | DT_SINGLELINE);
    text(h_label, L"右侧面板属于最终录制画面", 30, 1040, 424, 20,
         RGB(92, 103, 118), DT_LEFT | DT_SINGLELINE);

    DeleteObject(h_brand);
    DeleteObject(h_sub);
    DeleteObject(h_section);
    DeleteObject(h_timer);
    DeleteObject(h_label);
    DeleteObject(h_value);
    DeleteObject(h_body);

    // GDI 文字不保证 alpha 字节；强制不透明，避免面板被合成层擦成透明。
    for (size_t i = 3; i < static_cast<size_t>(kWidth) * kHeight * 4; i += 4)
        px[i] = 255;
}

void DrawPanel(PanelData* p) {
    if (!p->dc || !p->bits) return;
    uint8_t* px = static_cast<uint8_t*>(p->bits);

    for (int y = 0; y < kHeight; ++y) {
        const int v = 16 + (y * 12) / kHeight;
        for (int x = 0; x < kWidth; ++x) {
            uint8_t* q = px + (static_cast<size_t>(y) * kWidth + x) * 4;
            q[0] = static_cast<uint8_t>(v);
            q[1] = static_cast<uint8_t>(v + 3);
            q[2] = static_cast<uint8_t>(v + 8);
            q[3] = 255;
        }
    }
    RECT accent{0, 0, 4, kHeight};
    FillRectColor(p->dc, accent, RGB(208, 112, 46));
    RECT accent_hi{4, 0, 6, kHeight};
    FillRectColor(p->dc, accent_hi, RGB(82, 50, 34));

    SetBkMode(p->dc, TRANSPARENT);
    HFONT h_brand = CreateFontW(-26, 0, 0, 0, FW_BOLD, FALSE, FALSE, FALSE,
                                DEFAULT_CHARSET, OUT_DEFAULT_PRECIS, CLIP_DEFAULT_PRECIS,
                                CLEARTYPE_QUALITY, DEFAULT_PITCH, L"Microsoft YaHei UI");
    HFONT h_sub = CreateFontW(-12, 0, 0, 0, FW_NORMAL, FALSE, FALSE, FALSE,
                              DEFAULT_CHARSET, OUT_DEFAULT_PRECIS, CLIP_DEFAULT_PRECIS,
                              CLEARTYPE_QUALITY, DEFAULT_PITCH, L"Microsoft YaHei UI");
    HFONT h_section = CreateFontW(-22, 0, 0, 0, FW_BOLD, FALSE, FALSE, FALSE,
                                  DEFAULT_CHARSET, OUT_DEFAULT_PRECIS, CLIP_DEFAULT_PRECIS,
                                  CLEARTYPE_QUALITY, DEFAULT_PITCH, L"Microsoft YaHei UI");
    HFONT h_thought = CreateFontW(-20, 0, 0, 0, FW_NORMAL, FALSE, FALSE, FALSE,
                                  DEFAULT_CHARSET, OUT_DEFAULT_PRECIS, CLIP_DEFAULT_PRECIS,
                                  CLEARTYPE_QUALITY, DEFAULT_PITCH, L"Microsoft YaHei UI");
    HFONT h_action = CreateFontW(-14, 0, 0, 0, FW_NORMAL, FALSE, FALSE, FALSE,
                                 DEFAULT_CHARSET, OUT_DEFAULT_PRECIS, CLIP_DEFAULT_PRECIS,
                                 CLEARTYPE_QUALITY, DEFAULT_PITCH, L"Microsoft YaHei UI");
    HFONT h_label = CreateFontW(-11, 0, 0, 0, FW_NORMAL, FALSE, FALSE, FALSE,
                                DEFAULT_CHARSET, OUT_DEFAULT_PRECIS, CLIP_DEFAULT_PRECIS,
                                CLEARTYPE_QUALITY, DEFAULT_PITCH, L"Microsoft YaHei UI");
    HFONT h_tiny = CreateFontW(-9, 0, 0, 0, FW_NORMAL, FALSE, FALSE, FALSE,
                               DEFAULT_CHARSET, OUT_DEFAULT_PRECIS, CLIP_DEFAULT_PRECIS,
                               CLEARTYPE_QUALITY, DEFAULT_PITCH, L"Microsoft YaHei UI");
    HFONT h_timer = CreateFontW(-15, 0, 0, 0, FW_BOLD, FALSE, FALSE, FALSE,
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

    // Header: brand is deliberately small; the reasoning gets the space.
    text(h_brand, L"AUTO-HEROS3", 26, 18, 300, 36, RGB(248, 249, 251),
         DT_LEFT | DT_SINGLELINE);
    text(h_sub, L"智能体实况  ·  libobs 自有录制", 28, 54, 300, 20,
         RGB(146, 158, 176), DT_LEFT | DT_SINGLELINE);
    HBRUSH dot = CreateSolidBrush(RGB(239, 100, 86));
    HGDIOBJ old_dot = SelectObject(p->dc, dot);
    Ellipse(p->dc, 347, 30, 356, 39);
    SelectObject(p->dc, old_dot);
    DeleteObject(dot);
    text(h_label, L"录制中", 364, 25, 60, 18, RGB(226, 128, 112),
         DT_LEFT | DT_SINGLELINE);
    const uint64_t elapsed = p->started_ns ? (os_gettime_ns() - p->started_ns) / 1000000000ULL : 0;
    wchar_t rec[32];
    std::swprintf(rec, 32, L"%02llu:%02llu",
                  static_cast<unsigned long long>(elapsed / 60),
                  static_cast<unsigned long long>(elapsed % 60));
    text(h_timer, rec, 390, 24, 62, 24, RGB(235, 126, 107), DT_RIGHT | DT_SINGLELINE);

    // Collect the feed once: thoughts are shown big, actions separately.
    const std::vector<OverlayEntry> entries = ReadOverlay(p->overlay_path, 24);
    const OverlayEntry* latest_thought = nullptr;
    std::vector<OverlayEntry> ops;
    std::wstring terminal = L"终局：进行中";
    bool end_state_found = false;
    for (auto it = entries.rbegin(); it != entries.rend(); ++it) {
        if (it->kind == "thought" && !latest_thought) latest_thought = &*it;
        // Newest end/pause marker wins.  Pause is not a terminal ending: it is shown as
        // “暂停” and the user still decides whether to continue or terminate.
        if (!end_state_found &&
            (it->kind == "pause" || it->kind == "terminal" || it->kind == "game_over")) {
            terminal = Utf8ToWide(it->text);
            end_state_found = true;
        }
        if ((it->kind == "action" || it->kind == "result") && ops.size() < 5) {
            ops.push_back(*it);
        }
    }

    // Large reasoning block (this is the part the viewer should read).
    FillRoundRect(p->dc, RECT{24, 84, 456, 438}, 12, RGB(24, 30, 40), RGB(70, 63, 52));
    RECT thought_bar{28, 96, 33, 124};
    FillRectColor(p->dc, thought_bar, RGB(232, 168, 86));
    text(h_section, L"LLM 思考", 44, 94, 260, 34, RGB(247, 244, 236),
         DT_LEFT | DT_SINGLELINE);
    text(h_label, L"详细推理", 372, 102, 66, 18, RGB(170, 145, 108),
         DT_RIGHT | DT_SINGLELINE);
    text(h_tiny, L"这局为什么这样打、下一步准备做什么", 44, 126, 390, 16,
         RGB(135, 143, 156), DT_LEFT | DT_SINGLELINE);

    if (latest_thought) {
        std::wstring body = Utf8ToWide(latest_thought->text);
        if (body.size() > 700) body = body.substr(0, 700) + L"…";
        RECT calc{0, 0, 392, 0};
        HGDIOBJ old = SelectObject(p->dc, h_thought);
        DrawTextW(p->dc, body.c_str(), -1, &calc, DT_CALCRECT | DT_WORDBREAK | DT_NOPREFIX);
        SelectObject(p->dc, old);
        int h = static_cast<int>(calc.bottom - calc.top);
        h = std::max(30, std::min(276, h + 4));
        text(h_thought, body, 44, 150, 392, h, RGB(238, 240, 244),
             DT_LEFT | DT_WORDBREAK);
    } else {
        text(h_thought, L"等待下一步详细思考…", 44, 154, 392, 34,
             RGB(142, 150, 162), DT_LEFT | DT_SINGLELINE);
    }

    // Operations block: compact, but still separate from reasoning.
    FillRoundRect(p->dc, RECT{24, 452, 456, 902}, 12, RGB(22, 28, 37), RGB(52, 61, 74));
    RECT ops_bar{28, 466, 33, 490};
    FillRectColor(p->dc, ops_bar, RGB(98, 205, 137));
    text(h_section, L"LLM 操作", 44, 462, 260, 32, RGB(231, 244, 235),
         DT_LEFT | DT_SINGLELINE);
    text(h_label, L"动作 / 结果", 360, 470, 78, 18, RGB(125, 174, 143),
         DT_RIGHT | DT_SINGLELINE);
    int y = 500;
    if (ops.empty()) {
        text(h_action, L"等待操作记录…", 44, y + 4, 392, 24,
             RGB(145, 154, 168), DT_LEFT | DT_SINGLELINE);
    } else {
        for (const OverlayEntry& e : ops) {
            std::wstring body = Utf8ToWide(e.text);
            if (body.size() > 180) body = body.substr(0, 180) + L"…";
            RECT calc{0, 0, 380, 0};
            HGDIOBJ old = SelectObject(p->dc, h_action);
            DrawTextW(p->dc, body.c_str(), -1, &calc,
                      DT_CALCRECT | DT_WORDBREAK | DT_NOPREFIX);
            SelectObject(p->dc, old);
            int h = std::max(18, std::min(58, static_cast<int>(calc.bottom - calc.top) + 3));
            if (y + 22 + h > 884) break;
            text(h_label, KindLabel(e.kind), 44, y, 70, 18, KindColor(e.kind),
                 DT_LEFT | DT_SINGLELINE);
            if (!e.ts.empty()) {
                text(h_tiny, Utf8ToWide(e.ts), 362, y + 1, 64, 14,
                     RGB(102, 114, 130), DT_RIGHT | DT_SINGLELINE);
            }
            text(h_action, body, 44, y + 18, 380, h, RGB(207, 216, 227),
                 DT_LEFT | DT_WORDBREAK);
            y += 22 + h + 9;
            if (y < 892) {
                RECT rule{44, y - 5, 444, y - 4};
                FillRectColor(p->dc, rule, RGB(40, 48, 59));
            }
        }
    }

    // The former technical cards are now deliberately tiny at the bottom.
    RECT footer_rule{28, 914, 452, 915};
    FillRectColor(p->dc, footer_rule, RGB(44, 52, 64));
    COLORREF terminal_color = RGB(150, 163, 179);
    if (terminal.find(L"暂停") != std::wstring::npos) {
        terminal_color = RGB(239, 190, 104);
    } else if (terminal.find(L"可恢复") != std::wstring::npos &&
               terminal.find(L"不可恢复") == std::wstring::npos) {
        terminal_color = RGB(132, 186, 236);
    } else if (terminal.find(L"不可恢复") != std::wstring::npos ||
               terminal.find(L"投降") != std::wstring::npos ||
               terminal.find(L"失败") != std::wstring::npos ||
               terminal.find(L"LOSER") != std::wstring::npos) {
        terminal_color = RGB(232, 126, 104);
    } else if (terminal.find(L"胜利") != std::wstring::npos ||
               terminal.find(L"WINNER") != std::wstring::npos) {
        terminal_color = RGB(142, 211, 161);
    }
    if (terminal.size() > 52) terminal = terminal.substr(0, 52) + L"…";
    text(h_label, terminal, 30, 924, 424, 18, terminal_color,
         DT_LEFT | DT_SINGLELINE);
    text(h_tiny, L"4:3 游戏 1440×1080  ·  画布 1920×1080@60  ·  WGC/SDR/PCM", 30, 948, 424, 14,
         RGB(116, 127, 142), DT_LEFT | DT_SINGLELINE);
    text(h_tiny, L"录制中  ·  无物理播放  ·  无音频设备输出", 30, 966, 424, 14,
         RGB(100, 111, 126), DT_LEFT | DT_SINGLELINE);
    std::wstring sess = Utf8ToWide(p->session);
    if (sess.size() > 58) sess = sess.substr(0, 58) + L"…";
    text(h_tiny, L"会话 " + sess, 30, 986, 424, 14, RGB(92, 103, 118),
         DT_LEFT | DT_SINGLELINE);
    text(h_tiny, L"右侧面板属于最终录制画面  ·  思考与操作来自 MCP overlay.jsonl", 30, 1010, 424, 14,
         RGB(84, 94, 108), DT_LEFT | DT_SINGLELINE);

    DeleteObject(h_brand);
    DeleteObject(h_sub);
    DeleteObject(h_section);
    DeleteObject(h_thought);
    DeleteObject(h_action);
    DeleteObject(h_label);
    DeleteObject(h_tiny);
    DeleteObject(h_timer);

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

const char* PanelName(void*) { return "A3 中文状态面板 (S6)"; }

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
