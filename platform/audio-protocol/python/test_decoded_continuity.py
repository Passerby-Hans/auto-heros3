"""Offline failure-injection tests for lossy source-location acceptance."""
import unittest

import numpy as np

from audio_locator import RATE, locate, make_pn
from verify_decoded_continuity import boundary_checks, match_clip


class DecodedContinuityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.reference = make_pn(RATE * 4, seed=721234).mean(axis=1) / 32768

    def test_lossy_gain_quantization_and_noise_still_locate(self):
        source_index = 27183
        source = self.reference[source_index:source_index + 2048]
        rng = np.random.Generator(np.random.PCG64(123))
        decoded = np.round(source * 0.73 * 2048) / 2048 + rng.normal(0, 0.0008, len(source))
        match = locate(self.reference, decoded, 0, RATE)
        self.assertEqual(match["status"], "matched")
        self.assertEqual(match["source_sample"], source_index)

    def test_periodic_signal_and_silence_are_uncertain(self):
        sine = np.sin(np.arange(RATE) * 2 * np.pi * 1000 / RATE)
        self.assertEqual(locate(sine, sine[4410:6458], 0, RATE - 2048)["status"], "uncertain")
        self.assertEqual(locate(self.reference, np.zeros(2048), 0, RATE)["reason"], "low_energy")

    def test_declared_presentation_is_continuous_but_raw_concat_repeats(self):
        offset, length, extra = 500, RATE, 272
        first, base = match_clip(self.reference, self.reference[offset:offset + length + extra],
                                 presentation_frames=length, first_pts=0, global_begin=0, base_offset=None)
        second, _ = match_clip(self.reference, self.reference[offset + length:offset + length * 2 + 480],
                               presentation_frames=length, first_pts=0, global_begin=length, base_offset=base)
        first["clip"], second["clip"] = "first", "second"
        self.assertTrue(first["ok"] and second["ok"])
        boundary = boundary_checks([first, second], 2)[0]
        self.assertEqual(boundary["presentation_source_gap_samples"], 0)
        self.assertEqual(boundary["unsafe_raw_pcm_concat_gap_samples"], -extra)
        self.assertTrue(boundary["raw_extra_matches_next_source_start"])
        self.assertEqual(first["decoded_frames_raw"], length + extra)

    def test_missing_internal_samples_fail_not_a_new_alignment(self):
        start, length, cut, gap = 1000, RATE, RATE // 2, 128
        source = self.reference[start:start + length + gap]
        decoded = np.concatenate((source[:cut], source[cut + gap:]))
        result, _ = match_clip(self.reference, decoded, presentation_frames=length,
                               first_pts=0, global_begin=0, base_offset=None)
        self.assertFalse(result["ok"])
        self.assertIn("source_position_jump", result["errors"])

    def test_repeated_internal_samples_fail(self):
        start, length, cut, repeat = 1000, RATE, RATE // 2, 128
        source = self.reference[start:start + length]
        decoded = np.concatenate((source[:cut], source[cut - repeat:]))[:length]
        result, _ = match_clip(self.reference, decoded, presentation_frames=length,
                               first_pts=0, global_begin=0, base_offset=None)
        self.assertFalse(result["ok"])
        self.assertIn("source_position_jump", result["errors"])

    def test_boundary_missing_samples_fail(self):
        first, base = match_clip(self.reference, self.reference[500:500 + RATE],
                                 presentation_frames=RATE, first_pts=0, global_begin=0, base_offset=None)
        second, _ = match_clip(self.reference, self.reference[500 + RATE + 64:500 + RATE * 2 + 64],
                               presentation_frames=RATE, first_pts=0, global_begin=RATE, base_offset=base)
        self.assertTrue(first["ok"])
        self.assertFalse(second["ok"])
        self.assertEqual(second["measured_source_offset"], base + 64)

    def test_real_positive_decode_start_is_missing_head(self):
        result, _ = match_clip(self.reference, self.reference[500:500 + RATE],
                               presentation_frames=RATE, first_pts=32, global_begin=0, base_offset=None)
        self.assertFalse(result["ok"])
        self.assertIn("presentation_not_fully_covered_or_too_short", result["errors"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
