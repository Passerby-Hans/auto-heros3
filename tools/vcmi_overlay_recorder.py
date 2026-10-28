# -*- coding: utf-8 -*-
"""Standalone overlay recorder: game frame + LLM reasoning/action panel on one 16:9 canvas.

Design notes
------------
* Runs as its own process (spawned by the MCP server) so reloading the MCP server never
  interrupts a recording.
* Frames are streamed as raw RGB straight into a single ffmpeg/x264 encoder.  There are no
  intermediate JPEG/PNG files: no double compression (quality), no multi-GB frame cache, and
  nothing to clean up after the encode.  Legacy frame dirs from older runs are removed too.
* Canvas geometry is configurable: --height 768 -> 1366x768 (game 1024x768 + 342px panel),
  --height 1080 -> 1920x1080 (game 1440x1080 + 480px panel).  1080p upscales the 4:3 game
  frame with BICUBIC (Lanczos measured 14.7 ms vs 10.7 ms per 1024x768->1440x1080 frame and is
  visually equivalent at 1.4x); the panel is rendered at native resolution.
* Target frame rate defaults to 60.  If capture/compose occasionally misses a slot, the same
  frame is duplicated to keep wall-clock duration exact (and meta.json reports the shortfall).

Panel content comes from <state-dir>/overlay.jsonl, one JSON object per line:
    {"ts": "23:41:02", "kind": "thought"|"action"|"result"|"event", "text": "..."}
The MCP server appends action results automatically; the model adds thoughts with game_note.
A structured optional line {"kind": "goal"|"title", "text": "..."} sets the header
"当前目标" band (the newest such line wins).  Feeds written by older MCP versions simply lack
it and the band shows a dim placeholder - the protocol stays backward compatible.

The panel is split into four fixed regions so a 1080p clip reads well: header (title + current
goal + 局面 line), 思考 (wrapped thoughts, newest kept), 行动 (actions/results), 事件 (events).
Fonts/margins/line heights scale with the canvas height; every entry is CJK-wrapped and the
oldest lines are trimmed first when a section overflows, so the newest reasoning is on camera.
"""
from __future__ import annotations

import argparse
import datetime
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))

import vcmi_shot  # noqa: E402
from PIL import Image, ImageDraw, ImageFont, ImageStat  # noqa: E402

BG = (13, 17, 24)
FG = (226, 232, 240)
FG_DIM = (148, 163, 184)
ACCENT = (96, 165, 250)
OK = (74, 222, 128)
ERR = (248, 113, 113)
WARN = (251, 191, 36)
THOUGHT = (196, 181, 253)

# --- recording health sampling (revised execution plan 2025-10-25 §5.2) ----------------------
# The 2026-10-25 WIN clip kept a live overlay panel while the game area went black for minutes
# after the window was minimised.  Recording therefore samples both areas and writes evidence:
#   <out>/health.jsonl         one JSON object per ~0.5 s (game_luma/panel_luma/hwnd/game_area)
#   <out>/health_summary.json  aggregated black-frame statistics at stop
# Luma is computed on a tiny thumbnail on a sampling cadence (0.5 s), not per frame on the full
# image, so the 60 fps capture loop stays cheap (measured well below 2 ms per computed sample).
HEALTH_INTERVAL = 0.5             # seconds between health samples
HEALTH_BLACK_THRESHOLD = 12.0     # game-area mean luma below this = black frame
HEALTH_MIN_BLACK_SECONDS = 2.0    # only runs at least this long count as black_intervals
HEALTH_PANEL_ACTIVE_LUMA = 12.0   # panel-area mean luma at/above this = panel has content
HEALTH_THUMB = (48, 36)           # thumbnail used for the average (sampling, not full-frame mean)
HEALTH_REQUIRED_SUMMARY_FIELDS = (
    'ok', 'samples', 'game_area_rect', 'first_frame_black', 'last_frame_black',
    'black_ratio', 'longest_black_seconds', 'black_intervals', 'panel_active_during_black',
    'window_lost_samples', 'note')

FONT_CANDIDATES = (
    r'C:\Windows\Fonts\msyh.ttc',
    r'C:\Windows\Fonts\msyhbd.ttc',
    r'C:\Windows\Fonts\simhei.ttf',
    r'C:\Windows\Fonts\deng.ttf',
)
# Bold face first for titles/section labels; falls back to the regular faces above.
FONT_BOLD_CANDIDATES = (
    r'C:\Windows\Fonts\msyhbd.ttc',
    r'C:\Windows\Fonts\msyh.ttc',
    r'C:\Windows\Fonts\simhei.ttf',
    r'C:\Windows\Fonts\deng.ttf',
)


def load_font(size: int, bold: bool = False):
    for path in (FONT_BOLD_CANDIDATES if bold else FONT_CANDIDATES):
        if Path(path).exists():
            try:
                return ImageFont.truetype(path, size)
            except OSError:
                continue
    return ImageFont.load_default()


def wrap(text: str, font, width: int, draw: ImageDraw.ImageDraw) -> list[str]:
    """CJK-friendly greedy wrap (per character; ASCII words stay together)."""
    lines: list[str] = []
    current = ''
    tokens: list[str] = []
    word = ''
    for ch in text:
        if ch.isascii() and not ch.isspace():
            word += ch
            continue
        if word:
            tokens.append(word)
            word = ''
        tokens.append(ch)
    if word:
        tokens.append(word)
    for token in tokens:
        candidate = current + token
        if draw.textlength(candidate, font=font) <= width or not current:
            current = candidate
            continue
        lines.append(current.rstrip())
        current = '' if token == '\n' else token
        if token == '\n':
            continue
    if current.strip():
        lines.append(current.rstrip())
    return lines


class Feed:
    """overlay.jsonl tailer with 0.25 s mtime-based refresh."""

    def __init__(self, path: Path):
        self.path = path
        self.mtime = -1.0
        self.entries: list[dict] = []
        # Thoughts are tracked separately from the rolling action buffer: a long run writes
        # hundreds of action lines, and pruning to the last N entries used to drop every thought
        # written early on - the panel then showed an empty "思考" area in the late game.
        self.thoughts: list[dict] = []
        self.actions: list[dict] = []
        self.events: list[dict] = []
        self.goal = ''

    def refresh(self) -> None:
        try:
            mtime = self.path.stat().st_mtime
        except OSError:
            return
        if mtime == self.mtime:
            return
        self.mtime = mtime
        entries = []
        try:
            with self.path.open('r', encoding='utf-8') as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        item = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(item, dict) and item.get('text'):
                        entries.append(item)
        except OSError:
            return
        self.thoughts = [e for e in entries if e.get('kind') == 'thought'][-8:]
        # Optional structured goal/title line -> header band; newest one wins.
        goals = [e for e in entries if e.get('kind') in ('goal', 'title')]
        self.goal = str(goals[-1].get('text', '')).strip()[:220] if goals else ''
        self.actions = [e for e in entries if e.get('kind') in ('action', 'result')][-40:]
        known = ('thought', 'goal', 'title', 'action', 'result')
        self.events = [e for e in entries
                       if e.get('kind') == 'event' or e.get('kind') not in known][-16:]
        self.entries = entries[-400:]


def ts_age_seconds(ts: str):
    """Seconds since an 'HH:MM:SS' overlay timestamp (None when it cannot be parsed)."""
    try:
        hh, mm, ss = (int(part) for part in str(ts).split(':'))
    except (TypeError, ValueError):
        return None
    now = datetime.datetime.now()
    then = now.replace(hour=hh, minute=mm, second=ss, microsecond=0)
    delta = (now - then).total_seconds()
    if delta < -60:      # timestamp written just before midnight
        delta += 86400.0
    return max(0.0, delta)


def read_state_line(state_dir: Path) -> str:
    try:
        st = json.loads((state_dir / 'state.json').read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return ''
    date = st.get('date') or {}
    res = st.get('resources') or {}
    parts = []
    if date:
        parts.append(f"第 {date.get('week', '?')} 周 第 {date.get('day', '?')} 天")
    if res:
        parts.append(f"金 {res.get('gold', '?')}")
        parts.append(f"木/矿 {res.get('wood', '?')}/{res.get('ore', '?')}")
    if st.get('making_turn'):
        parts.append('我方回合')
    battle = st.get('battle') or {}
    if battle.get('active'):
        parts.append('⚔ 战斗中')
    return '   '.join(parts)


class Panel:
    """Renders the right-hand information column at native canvas resolution.

    Fixed regions: header (title, current-goal band, 局面 line), 思考, 行动, 事件, footer.
    Geometry scales with the canvas height so 768p and 1080p share one design; long entries are
    CJK-wrapped and the oldest lines are trimmed first on overflow, so the newest reasoning is
    always on camera.
    """

    def __init__(self, width: int, height: int, state_dir: Path):
        scale = height / 768.0
        self.scale = scale
        self.width = width
        self.height = height
        for name, size in (('font_title', 23), ('font_section', 15), ('font_body', 16),
                           ('font_small', 12)):
            setattr(self, name, load_font(max(9, int(size * scale + 0.5)),
                                          bold=name in ('font_title', 'font_section')))
        self.font_sub = self.font_section          # backward-compatible alias
        self.pad = max(10, int(18 * scale + 0.5))
        self.gap = max(2, int(5 * scale + 0.5))
        self.line_body = max(14, int(22 * scale + 0.5))
        self.line_action = max(13, int(20 * scale + 0.5))
        self.line_event = max(12, int(18 * scale + 0.5))
        self.line_small = max(11, int(17 * scale + 0.5))
        self.line_section = max(15, int(22 * scale + 0.5))
        self.title_h = max(18, int(29 * scale + 0.5))
        self.footer_h = max(16, int(28 * scale + 0.5))
        self.feed = Feed(state_dir / 'overlay.jsonl')
        self.state_dir = state_dir
        self.state_line = ''
        self.last_state_read = 0.0
        self.image = Image.new('RGB', (width, height), BG)

    @staticmethod
    def _result_color(text: str):
        if any(key in text for key in ('ERR', 'REJECTED', 'rejected', '失败')):
            return ERR
        if any(key in text for key in ('OK', 'COMPLETED', 'completed', 'submitted', 'SUBMITTED')):
            return OK
        return FG

    @staticmethod
    def _action_color(text: str):
        """Action lines written by the MCP carry the outcome inline: '<name> <args> -> status'."""
        if '-> rejected' in text or 'REJECTED' in text or 'ERR' in text:
            return ERR
        if '-> unknown' in text:
            return WARN
        if '-> completed' in text or 'COMPLETED' in text:
            return OK
        if '-> submitted' in text or 'submitted' in text:
            return FG
        return ACCENT

    def render(self, elapsed: float, frames: int) -> Image.Image:
        now = time.time()
        if now - self.last_state_read > 1.0:
            self.state_line = read_state_line(self.state_dir)
            self.last_state_read = now
        self.feed.refresh()
        img = Image.new('RGB', (self.width, self.height), BG)
        draw = ImageDraw.Draw(img)
        s = self.scale
        x0 = self.pad
        width = self.width - 2 * self.pad
        y = self.pad

        # ---- header: title, current-goal band, 局面 line --------------------------
        draw.text((x0, y), 'auto-heros3', font=self.font_title, fill=ACCENT)
        y += self.title_h
        draw.text((x0, y), 'LLM 实况：思考与行动', font=self.font_small, fill=FG_DIM)
        y += self.line_small + self.gap

        band_pad = max(4, int(8 * s + 0.5))
        goal = self.feed.goal
        goal_text = goal or '（未设置；game_note(kind="goal") 可设定演示目标）'
        goal_lines = wrap(goal_text, self.font_body, width - 2 * band_pad, draw)[:2]
        band_h = band_pad * 2 + self.line_small + len(goal_lines) * self.line_body
        draw.rounded_rectangle((x0, y, x0 + width - 1, y + band_h - 1),
                               radius=max(3, int(6 * s + 0.5)), fill=(20, 27, 40),
                               outline=(45, 55, 72), width=1)
        draw.text((x0 + band_pad, y + band_pad), '当前目标', font=self.font_small, fill=ACCENT)
        gy = y + band_pad + self.line_small
        for line in goal_lines:
            draw.text((x0 + band_pad, gy), line, font=self.font_body,
                      fill=FG if goal else FG_DIM)
            gy += self.line_body
        y += band_h + self.gap
        draw.text((x0, y), self.state_line or '读取局面…', font=self.font_small, fill=FG_DIM)
        y += self.line_small + self.gap
        draw.line((x0, y, x0 + width, y), fill=(45, 55, 72))
        y += self.gap

        footer_y = self.height - self.footer_h
        avail = max(self.line_body * 3, footer_y - y - self.gap * 2)
        stamp_w = round(draw.textlength('00:00:00', font=self.font_small)) + max(3, int(5 * s))
        prefix_w = max(8, int(12 * s + 0.5))
        text_w = width - stamp_w - prefix_w

        def collect(entries, font, prefix_for, color_for):
            """One group of rendered rows per entry (keeps entries atomic when trimming)."""
            groups = []
            for entry in entries:
                text = str(entry.get('text', '') or '')
                wrapped = wrap(text, font, text_w, draw) or ['']
                stamp = str(entry.get('ts', ''))
                prefix = prefix_for(entry)
                groups.append([{'stamp': stamp if index == 0 else '',
                                'prefix': prefix if index == 0 else '',
                                'entry_ts': stamp, 'entry_prefix': prefix,
                                'text': item, 'color': color_for(entry)}
                               for index, item in enumerate(wrapped)])
            return groups

        def draw_section(label, color, region_h, groups, font, line_h, empty_hint, last):
            nonlocal y
            draw.text((x0, y), label, font=self.font_section, fill=color)
            content_y = y + self.line_section
            content_h = region_h - self.line_section
            keep = max(1, content_h // line_h)
            flat = []
            if groups:
                # Drop the oldest *whole* entries until the newest ones fit, so a visible
                # section normally starts at a timestamp instead of mid-sentence.  Only when the
                # newest single entry alone is taller than the region is it trimmed from its top.
                flat = [row for group in groups for row in group]
                start = 0
                while start < len(groups) - 1 and len(flat) > keep:
                    start += 1
                    flat = [row for group in groups[start:] for row in group]
                if len(flat) > keep:
                    flat = flat[-keep:]
                if flat and not flat[0]['stamp']:
                    flat[0]['stamp'] = flat[0]['entry_ts']
                if flat and not flat[0]['prefix']:
                    flat[0]['prefix'] = flat[0]['entry_prefix']
            if not flat:
                if empty_hint:
                    draw.text((x0, content_y), empty_hint, font=self.font_small, fill=FG_DIM)
            else:
                for row in flat:
                    if row['stamp']:
                        draw.text((x0, content_y), row['stamp'], font=self.font_small,
                                  fill=FG_DIM)
                    if row['prefix']:
                        draw.text((x0 + stamp_w, content_y), row['prefix'], font=font,
                                  fill=row['color'])
                    draw.text((x0 + stamp_w + prefix_w, content_y), row['text'], font=font,
                              fill=row['color'])
                    content_y += line_h
            y += region_h
            if not last:
                draw.line((x0, y - self.gap, x0 + width, y - self.gap), fill=(38, 46, 60))

        thought_groups = collect(self.feed.thoughts, self.font_body,
                                 lambda entry: '', lambda entry: THOUGHT)
        if self.feed.thoughts:
            # make staleness visible instead of silently keeping an old thought on screen
            age = ts_age_seconds(self.feed.thoughts[-1].get('ts', ''))
            if age is not None and age > 120:
                thought_groups.append([{'stamp': '', 'prefix': '', 'entry_ts': '',
                                        'entry_prefix': '',
                                        'text': f'（思考 {int(age)} 秒未更新）',
                                        'color': WARN}])
        action_groups = collect(
            self.feed.actions, self.font_body,
            lambda entry: '> ' if entry.get('kind') == 'action' else '· ',
            lambda entry: (self._action_color(str(entry.get('text', '')))
                           if entry.get('kind') == 'action'
                           else self._result_color(str(entry.get('text', '')))))
        event_groups = collect(self.feed.events, self.font_small,
                               lambda entry: '• ',
                               lambda entry: WARN if entry.get('kind') == 'event' else FG_DIM)

        region_thought = int(avail * 0.50)
        region_action = int(avail * 0.30)
        region_event = max(self.line_event * 2, avail - region_thought - region_action)
        draw_section('思考', THOUGHT, region_thought, thought_groups, self.font_body,
                     self.line_body, '（等待 LLM 指令…）', last=False)
        draw_section('行动', ACCENT, region_action, action_groups, self.font_body,
                     self.line_action, '（暂无动作）', last=False)
        draw_section('事件', WARN, region_event, event_groups, self.font_small,
                     self.line_event, '（暂无事件）', last=True)

        draw.line((x0, footer_y, x0 + width, footer_y), fill=(45, 55, 72))
        draw.text((x0, footer_y + max(2, int(4 * s + 0.5))),
                  f'录制 {int(elapsed // 60):02d}:{int(elapsed % 60):02d}   {frames} 帧',
                  font=self.font_small, fill=FG_DIM)
        self.image = img
        return img


def encode_done(video: Path) -> bool:
    return video.exists() and video.stat().st_size > 0


# ---------------------------------------------------------------- recording health (pure parts)
def region_luma(img: Image.Image, box: tuple[int, int, int, int] | None = None,
                thumb: tuple[int, int] = HEALTH_THUMB) -> float | None:
    """Average luma (0-255) of an image region, computed on a tiny thumbnail.

    Sampling-based by design: a 48x36 NEAREST thumbnail is enough to tell a black game area from
    a normal frame, and PIL's resize/ImageStat run in C.  Never raises: None means "cannot judge".
    """
    try:
        region = img.crop(box) if box is not None else img
        if region.width <= 0 or region.height <= 0:
            return None
        small = region.resize(thumb, Image.NEAREST).convert('L')
        return round(float(ImageStat.Stat(small).mean[0]), 1)
    except Exception:
        return None


def _black_runs(samples: list[dict], threshold: float) -> list[list[float]]:
    """Consecutive black-sample runs as [first_t, last_t, sample_count] (invalid sample breaks run)."""
    runs: list[list[float]] = []
    current: list[float] | None = None
    for sample in samples:
        t = sample.get('t')
        luma = sample.get('game_luma')
        numeric = isinstance(t, (int, float)) and isinstance(luma, (int, float))
        if numeric and luma < threshold:
            if current is None:
                current = [float(t), float(t), 1.0]
            else:
                current[1] = float(t)
                current[2] += 1.0
        elif current is not None:
            runs.append(current)
            current = None
    if current is not None:
        runs.append(current)
    return runs


def summarize_health(samples: list[dict],
                     black_threshold: float = HEALTH_BLACK_THRESHOLD,
                     min_black_seconds: float = HEALTH_MIN_BLACK_SECONDS,
                     extra_notes: list[str] | None = None) -> dict:
    """Aggregate health samples into the health_summary.json contract (pure function).

    `ok` = the sampler produced usable data and the window was not lost after the first sampled
    frame.  It does NOT mean the game area was healthy: black-screen findings are expressed by
    first/last_frame_black, black_ratio, longest_black_seconds and black_intervals.
    `black_ratio` denominator = samples with a numeric game_luma.  `black_intervals` are the
    first/last *sampled* times of a run, so the true black span can be up to one interval longer.
    """
    rows = [s for s in samples if isinstance(s, dict)]
    total = len(rows)
    valid = [s for s in rows if isinstance(s.get('game_luma'), (int, float))]
    lost = [s for s in rows if s.get('error')]
    first_valid = next((i for i, s in enumerate(rows)
                        if isinstance(s.get('game_luma'), (int, float))), None)
    lost_after_first = (sum(1 for s in rows[first_valid:] if s.get('error'))
                        if first_valid is not None else len(lost))
    black = [s for s in valid if s['game_luma'] < black_threshold]
    runs = _black_runs(rows, black_threshold)
    intervals = [[round(run[0], 1), round(run[1], 1)] for run in runs
                 if (run[1] - run[0]) >= min_black_seconds]
    longest = max((run[1] - run[0] for run in runs), default=0.0)
    panel_values = [s['panel_luma'] for s in black
                    if isinstance(s.get('panel_luma'), (int, float))]
    panel_active: bool | None = None
    if panel_values:
        panel_active = sum(1 for value in panel_values
                           if value >= HEALTH_PANEL_ACTIVE_LUMA) * 2 > len(panel_values)
    first_black: bool | None = None
    last_black: bool | None = None
    if valid:
        first_black = valid[0]['game_luma'] < black_threshold
        last_black = valid[-1]['game_luma'] < black_threshold
    game_area: list[int] | None = None
    for sample in rows:
        area = sample.get('game_area')
        if isinstance(area, (list, tuple)) and len(area) == 4:
            game_area = [int(value) for value in area]
            break
    black_ratio: float | None = None
    if valid:
        black_ratio = round(len(black) / len(valid), 4)
    note_parts = [f'黑屏判据 game_luma<{black_threshold:g}；计入 black_intervals 需连续>='
                  f'{min_black_seconds:g}s；samples={total}, valid_game={len(valid)}, '
                  f'window_lost={len(lost)}；ok 只表示采样有效且首帧后窗口未丢失，'
                  '不代表游戏区正常。']
    if extra_notes:
        note_parts = [str(item) for item in extra_notes if item] + note_parts
    if intervals:
        black_total = sum(end - start for start, end in intervals)
        note_parts.append(f'检测到游戏区持续黑区间 {len(intervals)} 段，共 {black_total:.1f}s'
                          '（区间为采样首末时间，实际可能多至多一个采样间隔）；'
                          '可能是窗口最小化/遮挡，未经独立验证，不据此判定原因。')
    if panel_active and black:
        note_parts.append('黑样本期间 panel_luma 仍有内容，overlay 面板可能在继续更新（仅亮度证据）。')
    if lost:
        note_parts.append(f'窗口丢失/采样失败 {len(lost)} 次（见 health.jsonl 的 error 字段）。')
    return {'ok': bool(first_valid is not None and lost_after_first == 0),
            'samples': total,
            'game_area_rect': game_area,
            'first_frame_black': first_black,
            'last_frame_black': last_black,
            'black_ratio': black_ratio,
            'longest_black_seconds': round(longest, 1),
            'black_intervals': intervals,
            'panel_active_during_black': panel_active,
            'window_lost_samples': len(lost),
            'note': ' '.join(note_parts)}


class HealthRecorder:
    """Best-effort luma sampler: appends health.jsonl while recording, writes the summary on close.

    All file/IO errors are swallowed: health evidence is important but must never kill a clip.
    """

    def __init__(self, out_dir: Path, interval: float = HEALTH_INTERVAL,
                 black_threshold: float = HEALTH_BLACK_THRESHOLD,
                 min_black_seconds: float = HEALTH_MIN_BLACK_SECONDS):
        self.out_dir = Path(out_dir)
        self.path = self.out_dir / 'health.jsonl'
        self.summary_path = self.out_dir / 'health_summary.json'
        self.interval = float(interval)
        self.black_threshold = float(black_threshold)
        self.min_black_seconds = float(min_black_seconds)
        self.samples: list[dict] = []
        self._notes: list[str] = []
        self._handle = None
        self._last_elapsed: float | None = None
        self._pending = 0
        try:
            self.out_dir.mkdir(parents=True, exist_ok=True)
            self._handle = self.path.open('a', encoding='utf-8')
        except OSError:
            self._handle = None

    def add_note(self, text: str) -> None:
        """Add a human-readable note (first-frame window/game-area check) to the summary."""
        if text:
            self._notes.append(str(text))

    def due(self, elapsed: float) -> bool:
        return self._last_elapsed is None or elapsed - self._last_elapsed >= self.interval

    def _record(self, sample: dict) -> None:
        self.samples.append(sample)
        self._last_elapsed = float(sample['t'])
        self._pending += 1
        if self._handle is None:
            return
        try:
            self._handle.write(json.dumps(sample, ensure_ascii=False) + '\n')
            if self._pending >= 10:
                self._handle.flush()
                self._pending = 0
        except (OSError, ValueError):
            pass

    def sample_frame(self, elapsed: float, game_img: Image.Image, panel_img: Image.Image,
                     hwnd, game_area) -> bool:
        """Sample one grab (throttled by `interval`); returns True when a sample was written."""
        if not self.due(elapsed):
            return False
        game_luma = region_luma(game_img)
        panel_luma = region_luma(panel_img)
        sample = {'t': round(float(elapsed), 1), 'game_luma': game_luma,
                  'panel_luma': panel_luma,
                  'hwnd': int(hwnd) if hwnd else None,
                  'game_area': [int(v) for v in game_area] if game_area else None}
        if game_luma is None:
            sample['error'] = 'luma_failed'
        self._record(sample)
        return True

    def sample_lost(self, elapsed: float, reason: str = 'window_lost') -> bool:
        """Record a sample for a missing window / failed grab (null luma + error reason)."""
        if not self.due(elapsed):
            return False
        self._record({'t': round(float(elapsed), 1), 'game_luma': None, 'panel_luma': None,
                      'hwnd': None, 'game_area': None, 'error': str(reason)})
        return True

    def close(self) -> dict:
        if self._handle is not None:
            try:
                self._handle.flush()
            except OSError:
                pass
            try:
                self._handle.close()
            except OSError:
                pass
            self._handle = None
        summary = summarize_health(self.samples, self.black_threshold, self.min_black_seconds,
                                   extra_notes=self._notes)
        try:
            self.summary_path.write_text(json.dumps(summary, ensure_ascii=False), encoding='utf-8')
        except OSError:
            pass
        return summary


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--dir', required=True, help='bridge state dir (state.json / overlay.jsonl)')
    ap.add_argument('--out', required=True, help='output directory for the video')
    ap.add_argument('--fps', type=int, default=60)
    ap.add_argument('--height', type=int, default=768, help='canvas height: 768 or 1080')
    ap.add_argument('--crf', type=int, default=10, help='x264 CRF; 0 = lossless')
    ap.add_argument('--preset', default='veryfast')
    ap.add_argument('--keep-frames', action='store_true',
                    help='legacy: keep JPEG frames (off by default; streaming only)')
    ap.add_argument('--stop-file', default=None)
    ap.add_argument('--health-interval', type=float, default=HEALTH_INTERVAL,
                    help='seconds between health samples (game/panel luma, see health_summary.json)')
    ap.add_argument('--health-black-threshold', type=float, default=HEALTH_BLACK_THRESHOLD,
                    help='game-area mean luma below this counts as a black frame')
    ap.add_argument('--health-min-black', type=float, default=HEALTH_MIN_BLACK_SECONDS,
                    help='minimum consecutive black seconds counted in black_intervals')
    args = ap.parse_args()

    state_dir = Path(args.dir)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    stop_file = Path(args.stop_file) if args.stop_file else out / 'STOP'
    video = out / 'video.mp4'
    height = args.height
    total_w = round(height * 16 / 9)
    if total_w % 2:              # yuv420p needs even dimensions for the raw stream
        total_w += 1
    game_w = round(height * 4 / 3)
    game_w -= game_w % 2
    panel_w = total_w - game_w

    ffmpeg = shutil.which('ffmpeg') or r'C:\Users\Hans\AppData\Local\Microsoft\WinGet\Links\ffmpeg.exe'
    if not Path(ffmpeg).exists():
        (out / 'meta.json').write_text(json.dumps({'ok': False, 'error': 'ffmpeg not found'}))
        return 2
    cmd = [ffmpeg, '-y', '-hide_banner', '-loglevel', 'error',
           '-f', 'rawvideo', '-pix_fmt', 'rgb24', '-s', f'{total_w}x{height}',
           '-r', str(args.fps), '-i', '-',
           '-c:v', 'libx264', '-preset', args.preset, '-crf', str(args.crf),
           '-pix_fmt', 'yuv420p', '-movflags', '+faststart', str(video)]
    encoder = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL)
    panel = Panel(panel_w, height, state_dir)
    canvas = Image.new('RGB', (total_w, height), BG)
    panel_image = panel.render(0.0, 0)
    canvas.paste(panel_image, (game_w, 0))

    health = HealthRecorder(out, interval=args.health_interval,
                            black_threshold=args.health_black_threshold,
                            min_black_seconds=args.health_min_black)
    started = time.time()
    frames = 0
    duplicated = 0
    hwnd = None
    panel_age = 0.0
    missed = 0
    first_frame = True
    try:
        while not stop_file.exists():
            if hwnd is None:
                wins = vcmi_shot.find_window()
                hwnd = wins[0][0] if wins else None
                if hwnd is None:
                    # health: a window was expected here; record the missing sample (throttled)
                    health.sample_lost(time.time() - started, 'window_missing')
                    time.sleep(0.2)
                    continue
            try:
                game = vcmi_shot.grab(hwnd)
            except Exception:
                hwnd = None
                health.sample_lost(time.time() - started, 'grab_error')
                time.sleep(0.2)
                continue
            now = time.time()
            if now - panel_age > 0.25:
                panel_image = panel.render(now - started, frames)
                panel_age = now
                canvas = Image.new('RGB', (total_w, height), BG)
                canvas.paste(panel_image, (game_w, 0))
            # health: first-frame window/game-area check, then game/panel luma on a fixed cadence
            if first_frame:
                if game.size != (game_w, height):
                    health.add_note(f'game frame {game.size[0]}x{game.size[1]} scaled to '
                                    f'{game_w}x{height}')
                else:
                    health.add_note(f'first frame {game.size[0]}x{game.size[1]} matches canvas '
                                    'game area')
                first_frame = False
            health.sample_frame(now - started, game, panel_image, hwnd,
                                (0, 0, game_w, height))
            game_scaled = game.resize((game_w, height), Image.BICUBIC) \
                if game.size != (game_w, height) else game
            canvas.paste(game_scaled, (0, 0))
            data = canvas.tobytes()
            try:
                encoder.stdin.write(data)
            except (BrokenPipeError, ValueError):
                break
            frames += 1
            target = started + frames / args.fps
            now = time.time()
            if now < target:
                time.sleep(target - now)
            else:
                behind = (now - target) * args.fps
                missed += 1
                for _ in range(min(4, int(behind))):
                    try:
                        encoder.stdin.write(data)
                    except (BrokenPipeError, ValueError):
                        break
                    frames += 1
                    duplicated += 1
    finally:
        # Health evidence is emitted first: it must survive even if the encoder shutdown raises.
        try:
            health.close()
        except Exception:
            pass
        try:
            encoder.stdin.close()
        except Exception:
            pass
        encoder.wait(timeout=300)
        elapsed = time.time() - started
        # The stream is the only artifact: drop legacy frame caches and the STOP marker.
        removed = 0
        if not args.keep_frames:
            for cache in (out / 'frames',):
                if cache.is_dir():
                    removed += len(list(cache.glob('*')))
                    shutil.rmtree(cache, ignore_errors=True)
        try:
            stop_file.unlink()
        except OSError:
            pass
        meta = {'ok': encode_done(video), 'frames': frames, 'fps_target': args.fps,
                'fps_actual': round(frames / elapsed, 2) if elapsed else 0,
                'height': height, 'canvas': f'{total_w}x{height}', 'crf': args.crf,
                'missed_slots': missed, 'duplicated_frames': duplicated,
                'video': str(video) if encode_done(video) else None,
                'bytes': video.stat().st_size if video.exists() else 0,
                'elapsed': round(elapsed, 1), 'cache_removed': removed,
                'health_file': str(out / 'health.jsonl')}
        (out / 'meta.json').write_text(json.dumps(meta, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
