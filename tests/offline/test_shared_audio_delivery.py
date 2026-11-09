"""Shared transport acceptance cannot mistake fallback/late/partial archive for PASS."""
from __future__ import annotations
import copy
import importlib.util
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("shared_delivery_harness", ROOT / "tests/live/test_native_audio_delivery.py")
delivery = importlib.util.module_from_spec(spec)
spec.loader.exec_module(delivery)


def failure(code: str) -> dict:
    return {"exit_code": 6, "status": "fail", "steps": [
        {"name": name, "ok": False, "detail": f"error_code='{code}'"}
        for name in ("framed_audio_after_stop", "framed_audio_consumed")]}


class SharedAudioDeliveryTests(unittest.TestCase):
    def test_archive_one_second_must_not_be_accepted_as_expected_failure(self):
        self.assertFalse(delivery.expected_stall_failure(failure("late_budget_exceeded"), 6, 1000, "shared_memory"))

    def test_archive_capacity_failure_is_specific(self):
        report = failure("live_producer_fatal")
        report["media"] = {"tail": {"source_after_drain": {"reader_diagnostics": {"producer_fatal_code": 2}}}}
        self.assertTrue(delivery.expected_stall_failure(report, 6, 6000, "shared_memory"))
        for code in (0, 3, 4, 7, None):
            report["media"]["tail"]["source_after_drain"]["reader_diagnostics"]["producer_fatal_code"] = code
            self.assertFalse(delivery.expected_stall_failure(report, 6, 6000, "shared_memory"))
        self.assertFalse(delivery.expected_stall_failure(failure("late_budget_exceeded"), 6, 6000, "shared_memory"))

    def test_reader_late_is_specific(self):
        self.assertTrue(delivery.expected_stall_failure(failure("late_budget_exceeded"), 6, 0, "shared_memory", 750))
        self.assertFalse(delivery.expected_stall_failure(failure("live_producer_fatal"), 6, 0, "shared_memory", 750))

    def test_missing_map_not_any_failed_start(self):
        report = {"status": "fail", "exit_code": 9, "steps": [
            {"name": "framed_audio_opened", "ok": False, "detail": "error_code='live_map_missing'"}]}
        self.assertTrue(delivery.expected_missing_map(report, 9))
        self.assertFalse(delivery.expected_missing_map(report, 6))
        report["steps"][0]["detail"] = "error_code='live_owner_mismatch'"
        self.assertFalse(delivery.expected_missing_map(report, 9))

    def test_actual_shared_transport_and_archive_fence(self):
        source = {"submitted_end_sample": 44000, "submitted_gaps": 0, "reader_diagnostics": {
            "transport": "shared_memory", "read_offset": 128, "eof_reopens": 0,
            "mmcss_registered": True, "first_submitted_age_ns": 69000000,
            "archive_commit_ok": True, "archive_committed_end_sample": 44000,
            "producer_fatal_code": 0, "fatal": {"latched": False}}}
        report = {"media": {"tail": {"source_after_drain": source}}}
        self.assertTrue(all(delivery.shared_delivery_checks(report).values()))
        for field, value in (("transport", "file"), ("read_offset", 8192), ("eof_reopens", 1),
                             ("first_submitted_age_ns", 70000001), ("mmcss_registered", False),
                             ("archive_commit_ok", False), ("archive_committed_end_sample", 43999),
                             ("producer_fatal_code", 2), ("fatal", {"latched": True})):
            changed = copy.deepcopy(report)
            changed["media"]["tail"]["source_after_drain"]["reader_diagnostics"][field] = value
            with self.subTest(field=field):
                self.assertFalse(all(delivery.shared_delivery_checks(changed).values()))
        self.assertFalse(all(delivery.shared_delivery_checks({}).values()))


if __name__ == "__main__":
    result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(SharedAudioDeliveryTests))
    print(f"{result.testsRun - len(result.failures) - len(result.errors)}/{result.testsRun} checks passed")
    raise SystemExit(0 if result.wasSuccessful() else 1)
