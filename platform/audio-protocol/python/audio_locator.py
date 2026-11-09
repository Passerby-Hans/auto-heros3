"""Deterministic non-periodic fixture and lossy-PCM position matching.

The reference is the original framed PCM, not an assumed AAC delay. Matching
uses normalized local correlation; low energy, low score or a non-unique peak
is explicitly uncertain rather than fabricated timing.
"""
from __future__ import annotations

import hashlib
import struct
import zlib
from pathlib import Path

import numpy as np

RATE = 44100


def make_pn(frames: int, amplitude: float = 0.6, seed: int = 670887) -> np.ndarray:
    if frames <= 0 or not 0 < amplitude <= 0.95:
        raise ValueError("invalid_pn_frames_or_amplitude")
    # 129-tap Hamming bandpass, 700..5000 Hz. PCG64 is selected explicitly.
    # Generate a continuous random sequence for the entire fixture; no block,
    # second, segment or AAC-frame repeats. Both channels carry the same code.
    taps = 129
    n = np.arange(taps, dtype=np.float64) - (taps - 1) / 2
    lowpass = lambda hz: 2 * hz / RATE * np.sinc(2 * hz / RATE * n)
    kernel = (lowpass(5000) - lowpass(700)) * np.hamming(taps)
    rng = np.random.Generator(np.random.PCG64(seed))
    white = rng.uniform(-1.0, 1.0, frames + taps - 1)
    filtered = np.convolve(white, kernel, mode="valid")
    filtered *= amplitude / np.max(np.abs(filtered))
    mono = np.rint(filtered * 32767).astype("<i2")
    return np.column_stack((mono, mono))


def load_reference(path: Path) -> tuple[np.ndarray, dict]:
    chunks = []
    expected_sample = 0
    expected_sequence = 0
    capture = None
    saw_last = False
    digest = hashlib.sha256()
    framed_digest = hashlib.sha256()
    with path.open("rb") as stream:
        while header := stream.read(128):
            if len(header) != 128 or header[:4] != b"A3AP":
                raise ValueError("bad_reference_header")
            version, header_len, flags = struct.unpack_from("<HHI", header, 4)
            capture_id, generation, sequence, rate, channels, sample_format, size = struct.unpack_from(
                "<QIIIHHI", header, 12)
            start, frames = struct.unpack_from("<QQ", header, 40)
            if (version != 1 or header_len != 128 or rate != RATE or channels != 2 or
                    sample_format != 1 or size != frames * 4 or size > 4 * 1024 * 1024):
                raise ValueError("unsupported_reference_format")
            crc_header = bytearray(header)
            crc_header[112:116] = b"\0" * 4
            if zlib.crc32(crc_header) & 0xFFFFFFFF != struct.unpack_from("<I", header, 112)[0]:
                raise ValueError("reference_header_crc")
            payload = stream.read(size)
            if len(payload) != size or zlib.crc32(payload) & 0xFFFFFFFF != struct.unpack_from("<I", header, 116)[0]:
                raise ValueError("reference_payload_crc")
            framed_digest.update(header)
            framed_digest.update(payload)
            if start != expected_sample or sequence != expected_sequence or saw_last:
                raise ValueError("reference_sample_or_sequence_gap")
            if capture is None:
                capture = (capture_id, generation)
                if not flags & 1:
                    raise ValueError("reference_missing_first")
            if capture != (capture_id, generation) or flags & ~3:
                raise ValueError("reference_discontinuity")
            if frames:
                digest.update(payload)
                chunks.append(np.frombuffer(payload, dtype="<i2").reshape(-1, 2))
            expected_sample += frames
            expected_sequence += 1
            saw_last = bool(flags & 2)
    if not chunks or not saw_last:
        raise ValueError("reference_incomplete")
    stereo = np.concatenate(chunks)
    return stereo.mean(axis=1, dtype=np.float64) / 32768.0, {
        "frames": expected_sample, "sample_rate": RATE, "pcm_sha256": digest.hexdigest(),
        "framed_sha256": framed_digest.hexdigest(), "packets": expected_sequence, "saw_last": saw_last,
    }


def locate(reference: np.ndarray, window: np.ndarray, lo: int, hi: int, *,
           minimum_score: float = 0.90, minimum_margin: float = 0.10,
           exclusion_samples: int = 16) -> dict:
    """Find a window start in [lo, hi], inclusive, using normalized FFT NCC."""
    window = np.asarray(window, dtype=np.float64)
    size = len(window)
    lo, hi = max(0, int(lo)), min(int(hi), len(reference) - size)
    if size < 128 or hi < lo:
        return {"status": "uncertain", "reason": "insufficient_reference_or_window"}
    needle = window - np.mean(window)
    energy = float(np.dot(needle, needle))
    if energy < size * 1e-8:
        return {"status": "uncertain", "reason": "low_energy"}
    segment = np.asarray(reference[lo:hi + size], dtype=np.float64)
    fft_size = 1 << (len(segment) + size - 2).bit_length()
    cross = np.fft.irfft(np.fft.rfft(segment, fft_size) *
                         np.fft.rfft(needle[::-1], fft_size), fft_size)
    cross = cross[size - 1:size - 1 + hi - lo + 1]
    sums = np.concatenate(([0.0], np.cumsum(segment)))
    squares = np.concatenate(([0.0], np.cumsum(segment * segment)))
    means = sums[size:] - sums[:-size]
    energies = squares[size:] - squares[:-size] - means * means / size
    denom = np.sqrt(np.maximum(energies, 1e-30) * energy)
    scores = cross / denom
    best = int(np.argmax(scores))
    score = float(scores[best])
    competitors = scores.copy()
    competitors[max(0, best - exclusion_samples):best + exclusion_samples + 1] = -np.inf
    second = float(np.max(competitors)) if np.any(np.isfinite(competitors)) else -1.0
    margin = score - second
    trusted = score >= minimum_score and margin >= minimum_margin
    return {"status": "matched" if trusted else "uncertain",
            "reason": None if trusted else "low_score_or_ambiguous_peak",
            "source_sample": lo + best, "correlation": score,
            "second_peak": second, "margin": margin,
            "search_min": lo, "search_max": hi, "window_samples": size}
