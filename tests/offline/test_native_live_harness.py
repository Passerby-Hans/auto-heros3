"""Pure harness/control/source-identity contracts; never starts native media or a game."""
from __future__ import annotations

import asyncio
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "platform/audio-protocol/python"))
from make_test_framed import make_packet

spec = importlib.util.spec_from_file_location("native_live_harness", ROOT / "tests/live/test_native_recording_e2e.py")
h = importlib.util.module_from_spec(spec)
spec.loader.exec_module(h)


class HarnessTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.out = Path(self.tmp.name)

    def args(self, **overrides):
        return SimpleNamespace(**({"default_path": True, "rollover_seconds": None,
                                  "max_segment_gb": None} | overrides))

    def box(self):
        return h.ControlMailbox(self.out)

    def request(self, rid="one", tool="game_act", **arguments):
        return {"id": rid, "tool": tool, "arguments": arguments or
                {"action": "end_turn", "request_id": "action-one"}}

    def test_default_omits_backend_rollover(self):
        self.assertEqual(h.recording_arguments(self.args()), {})

    def test_size_cap_preserves_default_routing(self):
        self.assertEqual(h.recording_arguments(self.args(max_segment_gb=.02)), {"max_segment_gb": .02})

    def test_explicit_mode_compatibility(self):
        self.assertEqual(h.recording_arguments(self.args(default_path=False)),
                         {"backend": "libobs", "rollover_seconds": 10})

    def test_explicit_time_threshold(self):
        self.assertEqual(h.recording_arguments(self.args(rollover_seconds=900)), {"rollover_seconds": 900})

    def test_default_strips_all_audio_overrides(self):
        with patch.dict(os.environ, {"AUTO_HEROS3_AUDIO_DRIVER": "disk", "A3_BAD": "1",
                                    "A3CAPTURE_FILE": "bad", "SDL_AUDIODRIVER": "wasapi",
                                    "SDL_DISKAUDIOFILE": "stale.raw"}):
            env = h.isolated_env(self.out, True)
        self.assertNotIn("AUTO_HEROS3_AUDIO_DRIVER", env)
        self.assertFalse(any(k.startswith(("A3_", "A3CAPTURE_", "SDL_AUDIO", "SDL_DISKAUDIO")) for k in env))
        self.assertEqual(env["AUTO_HEROS3_STATE_DIR"], str(self.out / "state"))

    def test_explicit_env(self):
        self.assertEqual(h.isolated_env(self.out)["AUTO_HEROS3_AUDIO_DRIVER"], "a3capture")

    def test_default_session_omits_display(self):
        self.assertEqual(h.session_arguments("map", "default"), {"map_name": "map"})

    def test_existing_display_argument(self):
        self.assertEqual(h.session_arguments("map", "2"), {"map_name": "map", "display_index": 2})

    def test_action_requires_request_id(self):
        with self.assertRaises(ValueError):
            self.box().validate(self.request(action="end_turn"))

    def test_no_cheats_or_session_control(self):
        box = self.box()
        for i, request in enumerate((self.request(tool="session_stop"), self.request(action="debug_cheat", request_id="x"))):
            request["id"] = str(i)
            with self.assertRaises(ValueError):
                box.validate(request)

    def test_duplicate_control_id_is_not_dispatched(self):
        box = self.box()
        box.validate(self.request())
        with self.assertRaises(ValueError):
            box.validate(self.request())

    def test_duplicate_action_request_id_is_not_dispatched(self):
        box = self.box()
        box.validate(self.request())
        with self.assertRaises(ValueError):
            box.validate(self.request("different"))

    def test_partial_line_waits_without_consuming(self):
        box = self.box()
        line = json.dumps(self.request()).encode()
        box.path.write_bytes(line)
        self.assertIsNone(box.next_request())
        self.assertEqual(box.offset, 0)
        with box.path.open("ab") as f:
            f.write(b"\n")
        self.assertEqual(box.next_request(), self.request())

    def test_unknown_fence_allows_reads(self):
        box = self.box()
        box.apply_result("game_act", {"request_id": "a"}, {"status": "unknown", "action_id": "ledger-a"})
        self.assertEqual(box.validate(self.request(tool="game_status", unused=True))[1], "game_status")
        with self.assertRaises(ValueError):
            box.validate(self.request("two"))

    def test_reconcile_must_match_and_prove_completion(self):
        box = self.box()
        box.apply_result("game_act", {"request_id": "a"}, {"status": "submitted"})
        for rid, status in (("wrong", "completed"), ("a", "submitted")):
            box.apply_result("game_reconcile", {"request_id": rid},
                             {"resolved": True, "result": {"status": status}})
            self.assertIsNotNone(box.fence)
        box.apply_result("game_reconcile", {"request_id": "a"},
                         {"resolved": True, "result": {"status": "completed"}})
        self.assertIsNone(box.fence)

    def test_action_status_must_match_ledger(self):
        box = self.box()
        box.apply_result("game_act", {"request_id": "a"}, {"status": "unknown", "action_id": "ledger-a"})
        box.apply_result("game_action_status", {"action_id": "ledger-a"},
                         {"ok": True, "action": {"status": "rejected"}})
        self.assertIsNone(box.fence)

    def test_screenshot_cannot_escape_run(self):
        with self.assertRaises(ValueError):
            self.box().validate(self.request(tool="game_screenshot", path=str(self.out.parent / "escape.png")))

    def test_call_timeout_fences_without_retry(self):
        box = self.box()
        box.path.write_text(json.dumps(self.request()) + "\n", encoding="utf-8")
        calls = []
        async def call(*a, **kw):
            calls.append(a)
            raise TimeoutError("lost response")
        asyncio.run(box.dispatch_one(call))
        asyncio.run(box.dispatch_one(call))
        self.assertEqual(len(calls), 1)
        self.assertEqual(box.fence["status"], "unknown")
        rows = [json.loads(line) for line in box.results.read_text(encoding="utf-8").splitlines()]
        self.assertEqual([row["state"] for row in rows], ["dispatching", "unknown"])

    def test_size_reason_not_inferred_from_clip_count(self):
        with self.assertRaises(RuntimeError):
            h.rollover_events({"clips": [{}, {}]}, "SIZE")

    def test_explicit_rollover_event(self):
        report = {"output_events": [{"name": "output_rollover", "t_ns": 123,
            "detail": "reason=SIZE; start_pts_usec=0; boundary_pts_usec=10000000; closed_bytes=20000000"}]}
        self.assertEqual(h.rollover_events(report, "SIZE")[0]["closed_bytes"], 20000000)
        with self.assertRaises(RuntimeError):
            h.rollover_events(report, "TIME")

    def source_fixture(self):
        state = self.out / "state"
        state.mkdir()
        source = state / "a3capture.raw"
        source.write_bytes(make_packet(0, 0, 2, bytes(8), 1000000000, 1, 1234))
        Path(str(source) + ".ready").write_text("ready", encoding="utf-8")
        h.write_json(state / "session.json", {"notes": ["audio_sink=a3capture raw=test"]})
        h.write_json(state / "recording.json", {"config": {"audio_framed": str(source), "audio_framed_live": True}})
        return source

    def test_actual_framed_identity(self):
        self.source_fixture()
        self.assertEqual(h.framed_identity(self.out)["first_header"]["capture_id"], 1234)

    def test_corrupt_payload_is_not_accepted(self):
        path = self.source_fixture()
        path.write_bytes(path.read_bytes()[:-1] + b"x")
        with self.assertRaises(RuntimeError):
            h.framed_identity(self.out)

    def test_missing_ready_is_not_raw_fallback(self):
        path = self.source_fixture()
        Path(str(path) + ".ready").unlink()
        with self.assertRaises(RuntimeError):
            h.framed_identity(self.out)


if __name__ == "__main__":
    result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(HarnessTests))
    failures = len(result.failures) + len(result.errors)
    print(f"{result.testsRun - failures}/{result.testsRun} checks passed")
    raise SystemExit(0 if result.wasSuccessful() else 1)
