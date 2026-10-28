# -*- coding: utf-8 -*-
"""Offline tests for the overlay recorder's game-area health sampling (T8, revised plan §5.2).

No game / client / encoder is touched: luma statistics, black-interval aggregation and the
health.jsonl / health_summary.json contract are tested as pure functions on synthetic PIL images
and synthetic sample lists, and HealthRecorder is exercised inside a tempfile output directory.
Importing tools/vcmi_overlay_recorder.py must NOT start argparse/main (it is guarded by
``if __name__ == '__main__'``); ``--help`` is additionally checked through a subprocess.

Run: python -X utf8 tools/test_recording_health.py
Exit code 0 only when every check passes (also run twice to confirm determinism).
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
from PIL import Image  # noqa: E402
import vcmi_overlay_recorder as R  # noqa: E402

REQUIRED_SUMMARY = {
    'ok', 'samples', 'game_area_rect', 'first_frame_black', 'last_frame_black',
    'black_ratio', 'longest_black_seconds', 'black_intervals', 'panel_active_during_black',
    'window_lost_samples', 'note'}
REQUIRED_SAMPLE = {'t', 'game_luma', 'panel_luma', 'hwnd', 'game_area'}

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, condition, detail: str = '') -> None:
    RESULTS.append((name, bool(condition), detail))
    print(('PASS ' if condition else 'FAIL ') + name + (f'  ({detail})' if detail else ''))


def solid(w: int, h: int, value: int) -> Image.Image:
    return Image.new('RGB', (w, h), (value, value, value))


def sample(t: float, game, panel=40.0, area=(0, 0, 1024, 768), hwnd=1234) -> dict:
    return {'t': round(float(t), 1), 'game_luma': game, 'panel_luma': panel,
            'hwnd': hwnd, 'game_area': list(area)}


def lost(t: float, reason: str = 'window_missing') -> dict:
    return {'t': round(float(t), 1), 'game_luma': None, 'panel_luma': None, 'hwnd': None,
            'game_area': None, 'error': reason}


def test_luma() -> None:
    check('luma_black_is_zero', R.region_luma(solid(1024, 768, 0)) == 0.0,
          str(R.region_luma(solid(1024, 768, 0))))
    check('luma_white_is_255', R.region_luma(solid(1024, 768, 255)) == 255.0)
    mid = R.region_luma(solid(1024, 768, 128))
    check('luma_mid_close_to_128', mid is not None and abs(mid - 128.0) <= 1.0, str(mid))
    half = Image.new('RGB', (100, 100), (255, 255, 255))
    half.paste((0, 0, 0), (50, 0, 100, 100))
    check('luma_box_left_white', R.region_luma(half, box=(0, 0, 50, 100)) == 255.0)
    check('luma_box_right_black', R.region_luma(half, box=(50, 0, 100, 100)) == 0.0)
    check('luma_zero_width_box_none', R.region_luma(half, box=(60, 10, 60, 90)) is None)


def test_continuous_black() -> None:
    samples = [sample(i * 0.5, 0.0) for i in range(11)]          # 0.0 .. 5.0 s, all black
    summary = R.summarize_health(samples)
    check('black5s_ratio_one', summary['black_ratio'] == 1.0, str(summary['black_ratio']))
    check('black5s_longest_ge_4_5', summary['longest_black_seconds'] >= 4.5,
          str(summary['longest_black_seconds']))
    intervals = summary['black_intervals']
    check('black5s_interval_present',
          bool(intervals) and intervals[0][1] - intervals[0][0] >= 4.5, str(intervals))
    check('black5s_first_and_last_black',
          summary['first_frame_black'] is True and summary['last_frame_black'] is True)
    check('black5s_ok_no_window_loss', summary['ok'] is True)
    check('black5s_sample_count', summary['samples'] == 11, str(summary['samples']))
    check('black5s_game_area_rect', summary['game_area_rect'] == [0, 0, 1024, 768],
          str(summary['game_area_rect']))
    check('black5s_panel_active_true', summary['panel_active_during_black'] is True)
    check('black5s_window_lost_zero', summary['window_lost_samples'] == 0)


def test_short_black_excluded() -> None:
    samples = [sample(i * 0.5, 100.0) for i in range(21)]        # 10 s normal
    samples[6] = sample(3.0, 0.0)                                 # 1 s of black (2 samples)
    samples[7] = sample(3.5, 0.0)
    summary = R.summarize_health(samples)
    check('short_black_no_intervals', summary['black_intervals'] == [],
          str(summary['black_intervals']))
    check('short_black_longest_lt_2', summary['longest_black_seconds'] < 2.0,
          str(summary['longest_black_seconds']))
    check('short_black_ratio_2_of_21',
          abs(summary['black_ratio'] - round(2 / 21, 4)) < 1e-9, str(summary['black_ratio']))
    check('short_black_first_not_black', summary['first_frame_black'] is False)


def test_dark_scene_and_threshold() -> None:
    dark = [sample(i * 0.5, 20.0 + (i % 5) * 5) for i in range(20)]   # luma 20..40 = normal dusk
    summary = R.summarize_health(dark)
    check('dark_scene_not_black',
          summary['black_intervals'] == [] and summary['black_ratio'] == 0.0
          and summary['first_frame_black'] is False)
    at_threshold = R.summarize_health([sample(0.0, 12.0)])
    check('threshold_equal_is_not_black', at_threshold['first_frame_black'] is False,
          str(at_threshold['first_frame_black']))
    below = R.summarize_health([sample(0.0, 11.9)])
    check('threshold_below_is_black', below['first_frame_black'] is True)
    custom = R.summarize_health([sample(0.0, 25.0)], black_threshold=30.0)
    check('custom_threshold_marks_black', custom['black_ratio'] == 1.0)


def test_window_lost() -> None:
    samples = [sample(0.0, 100.0), lost(0.5), sample(1.0, 100.0)]
    summary = R.summarize_health(samples)
    check('window_lost_count', summary['window_lost_samples'] == 1,
          str(summary['window_lost_samples']))
    check('window_lost_after_first_is_not_ok', summary['ok'] is False)
    check('window_lost_ratio_uses_valid_denominator', summary['black_ratio'] == 0.0,
          str(summary['black_ratio']))
    only_lost = R.summarize_health([lost(0.0, 'grab_error'), lost(0.5)])
    check('all_lost_no_crash',
          only_lost['samples'] == 2 and only_lost['ok'] is False
          and only_lost['first_frame_black'] is None and only_lost['black_ratio'] is None,
          str(only_lost['samples']))
    before_first = R.summarize_health([lost(0.0, 'window_missing'), sample(0.5, 100.0)])
    check('lost_before_first_frame_still_ok', before_first['ok'] is True,
          f"lost={before_first['window_lost_samples']}")


def test_panel_active() -> None:
    active = R.summarize_health([sample(i * 0.5, 0.0, panel=40.0) for i in range(6)])
    check('panel_active_true_when_panel_visible', active['panel_active_during_black'] is True)
    dark_panel = R.summarize_health([sample(i * 0.5, 0.0, panel=0.0) for i in range(6)])
    check('panel_active_false_when_panel_black', dark_panel['panel_active_during_black'] is False)
    no_panel = R.summarize_health([sample(0.0, 0.0, panel=None)])
    check('panel_active_null_without_samples', no_panel['panel_active_during_black'] is None)
    check('panel_visible_plus_game_black_both_expressed',
          active['black_ratio'] == 1.0 and active['panel_active_during_black'] is True)


def test_empty_summary() -> None:
    summary = R.summarize_health([])
    check('empty_summary_contract', REQUIRED_SUMMARY <= set(summary), str(sorted(summary)))
    check('empty_summary_values',
          summary['ok'] is False and summary['samples'] == 0 and summary['black_ratio'] is None
          and summary['first_frame_black'] is None and summary['last_frame_black'] is None
          and summary['game_area_rect'] is None and summary['black_intervals'] == []
          and summary['longest_black_seconds'] == 0.0 and isinstance(summary['note'], str))
    check('summary_contract_matches_recorder_constant',
          set(REQUIRED_SUMMARY) == set(R.HEALTH_REQUIRED_SUMMARY_FIELDS))


def test_health_recorder_roundtrip() -> None:
    tmp = Path(tempfile.mkdtemp(prefix='rec_health_'))
    try:
        recorder = R.HealthRecorder(tmp, interval=0.0)
        check('recorder_due_initial', recorder.due(0.0) is True)
        recorder.sample_frame(0.0, solid(1024, 768, 0), solid(342, 768, 40), 1234,
                              (0, 0, 1024, 768))
        recorder.sample_frame(0.5, solid(1024, 768, 0), solid(342, 768, 40), 1234,
                              (0, 0, 1024, 768))
        recorder.sample_lost(0.8, 'grab_error')
        recorder.sample_frame(1.0, solid(1024, 768, 200), solid(342, 768, 40), 1234,
                              (0, 0, 1024, 768))
        summary = recorder.close()
        lines = [json.loads(line) for line in
                 (tmp / 'health.jsonl').read_text(encoding='utf-8').splitlines() if line.strip()]
        check('jsonl_line_count', len(lines) == 4, str(len(lines)))
        check('jsonl_every_line_valid_json', all(isinstance(item, dict) for item in lines))
        check('jsonl_required_keys', all(REQUIRED_SAMPLE <= set(item) for item in lines))
        check('jsonl_first_sample_game_area',
              lines[0]['game_area'] == [0, 0, 1024, 768] and lines[0]['hwnd'] == 1234
              and lines[0]['game_luma'] == 0.0)
        check('jsonl_lost_sample_nulls_and_error',
              lines[2]['game_luma'] is None and lines[2]['game_area'] is None
              and lines[2]['error'] == 'grab_error')
        check('summary_file_written', (tmp / 'health_summary.json').exists())
        written = json.loads((tmp / 'health_summary.json').read_text(encoding='utf-8'))
        check('written_summary_matches_return', written == summary)
        check('written_summary_contract', REQUIRED_SUMMARY <= set(written))
        check('written_summary_counts',
              written['samples'] == 4 and written['window_lost_samples'] == 1,
              str(written['samples']))
        check('written_summary_black_edges',
              written['first_frame_black'] is True and written['last_frame_black'] is False,
              f"{written['first_frame_black']}/{written['last_frame_black']}")
        check('written_summary_ratio', written['black_ratio'] == round(2 / 3, 4),
              str(written['black_ratio']))
        check('written_summary_note_string', isinstance(written['note'], str)
              and len(written['note']) > 20)

        throttled = R.HealthRecorder(tmp / 'throttle', interval=0.5)
        check('throttle_first_sample',
              throttled.sample_frame(0.0, solid(64, 64, 0), solid(64, 64, 40), 1,
                                     (0, 0, 64, 64)) is True)
        check('throttle_skips_fast_sample',
              throttled.sample_frame(0.2, solid(64, 64, 0), solid(64, 64, 40), 1,
                                     (0, 0, 64, 64)) is False)
        check('throttle_next_after_interval',
              throttled.sample_frame(0.6, solid(64, 64, 0), solid(64, 64, 40), 1,
                                     (0, 0, 64, 64)) is True)
        throttled.close()

        noted = R.HealthRecorder(tmp / 'noted')
        noted.add_note('first frame 1024x768 matches canvas game area')
        noted.sample_frame(0.0, solid(64, 64, 10), solid(64, 64, 40), 1, (0, 0, 64, 64))
        noted.close()
        noted_summary = json.loads((tmp / 'noted' / 'health_summary.json')
                                   .read_text(encoding='utf-8'))
        check('extra_note_reaches_summary',
              'first frame 1024x768' in noted_summary['note'],
              noted_summary['note'][:60])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_luma_performance() -> None:
    game = solid(1024, 768, 37)
    panel = solid(342, 768, 40)
    R.region_luma(game)  # warm-up
    count = 40
    started = time.perf_counter()
    for _ in range(count):
        R.region_luma(game)
        R.region_luma(panel)
    per_sample_ms = (time.perf_counter() - started) / count * 1000.0
    amortised_ms = per_sample_ms / (R.HEALTH_INTERVAL * 60.0)   # one sample every 0.5 s at 60 fps
    check('luma_sample_under_2ms', per_sample_ms < 2.0, f'{per_sample_ms:.3f} ms/sample')
    check('luma_amortised_under_0_1ms', amortised_ms < 0.1, f'{amortised_ms:.4f} ms/frame')


def test_main_loop_integration() -> None:
    """Exercise main()'s capture loop with a fake window/grab/encoder (no game, no ffmpeg).

    Patches only module-level collaborators, drives ~1.5 s of synthetic 128x96 black frames and
    asserts the loop still emits health.jsonl + health_summary.json and keeps the old meta fields.
    """
    tmp = Path(tempfile.mkdtemp(prefix='rec_main_'))
    out = tmp / 'out'
    state = tmp / 'state'
    state.mkdir(parents=True)
    grabs = {'n': 0}

    class FakeStdin:
        def __init__(self) -> None:
            self.bytes = 0

        def write(self, data) -> None:
            self.bytes += len(data)

        def close(self) -> None:
            pass

    class FakeEncoder:
        def __init__(self) -> None:
            self.stdin = FakeStdin()

        def wait(self, timeout=None) -> int:
            return 0

    def fake_find_window(*args, **kwargs):
        return [(0x1234, 'VCMI fake')]

    def fake_grab(hwnd):
        grabs['n'] += 1
        if grabs['n'] >= 45:
            (out / 'STOP').write_text('stop', encoding='utf-8')
        return Image.new('RGB', (128, 96), (0, 0, 0))

    originals = (R.vcmi_shot.find_window, R.vcmi_shot.grab, R.subprocess.Popen,
                 R.shutil.which, sys.argv)
    rc = None
    try:
        R.vcmi_shot.find_window = fake_find_window
        R.vcmi_shot.grab = fake_grab
        R.subprocess.Popen = lambda *args, **kwargs: FakeEncoder()
        R.shutil.which = lambda name: sys.executable          # passes the ffmpeg existence check
        sys.argv = ['vcmi_overlay_recorder.py', '--dir', str(state), '--out', str(out),
                    '--fps', '30', '--height', '96', '--health-interval', '0.5']
        rc = R.main()
    finally:
        (R.vcmi_shot.find_window, R.vcmi_shot.grab, R.subprocess.Popen,
         R.shutil.which, sys.argv) = originals
    try:
        check('main_loop_exit_zero', rc == 0, f'rc={rc}')
        check('main_loop_frames_written', grabs['n'] >= 45, str(grabs['n']))
        health_path = out / 'health.jsonl'
        check('main_loop_health_jsonl_written', health_path.exists())
        lines = [json.loads(line) for line in
                 health_path.read_text(encoding='utf-8').splitlines() if line.strip()]
        check('main_loop_health_samples_ge_2', len(lines) >= 2, str(len(lines)))
        check('main_loop_health_lines_contract', all(REQUIRED_SAMPLE <= set(item)
                                                     for item in lines))
        summary_path = out / 'health_summary.json'
        check('main_loop_summary_written', summary_path.exists())
        summary = json.loads(summary_path.read_text(encoding='utf-8'))
        check('main_loop_summary_contract', REQUIRED_SUMMARY <= set(summary))
        check('main_loop_summary_black_frames',
              summary['black_ratio'] == 1.0 and summary['first_frame_black'] is True,
              str(summary['black_ratio']))
        check('main_loop_summary_game_area', summary['game_area_rect'] == [0, 0, 128, 96],
              str(summary['game_area_rect']))
        check('main_loop_summary_no_window_loss', summary['window_lost_samples'] == 0)
        check('main_loop_summary_ok', summary['ok'] is True)
        meta = json.loads((out / 'meta.json').read_text(encoding='utf-8'))
        check('main_loop_meta_health_file',
              str(meta.get('health_file', '')).endswith('health.jsonl'))
        check('main_loop_meta_keeps_existing_fields',
              {'ok', 'frames', 'fps_target', 'fps_actual', 'height', 'canvas', 'crf',
               'missed_slots', 'duplicated_frames', 'video', 'bytes', 'elapsed',
               'cache_removed'} <= set(meta))
        check('main_loop_meta_frames', meta.get('frames') == 45, str(meta.get('frames')))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_cli_help() -> None:
    proc = subprocess.run(
        [sys.executable, '-X', 'utf8', str(ROOT / 'tools' / 'vcmi_overlay_recorder.py'), '--help'],
        capture_output=True, text=True, timeout=120)
    check('cli_help_exit_zero', proc.returncode == 0, f'rc={proc.returncode}')
    check('cli_help_lists_out', '--out' in proc.stdout and '--dir' in proc.stdout)
    check('cli_help_lists_health_args',
          '--health-interval' in proc.stdout and '--health-black-threshold' in proc.stdout)
    check('import_did_not_create_artifacts',
          not (ROOT / 'health.jsonl').exists() and not (ROOT / 'health_summary.json').exists())


def main() -> int:
    test_luma()
    test_continuous_black()
    test_short_black_excluded()
    test_dark_scene_and_threshold()
    test_window_lost()
    test_panel_active()
    test_empty_summary()
    test_health_recorder_roundtrip()
    test_main_loop_integration()
    test_luma_performance()
    test_cli_help()
    failed = [name for name, ok, _ in RESULTS if not ok]
    print(f'\n{len(RESULTS) - len(failed)}/{len(RESULTS)} checks passed')
    if failed:
        print('FAILED: ' + ', '.join(failed))
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
