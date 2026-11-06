#!/usr/bin/env python3
"""a3-audio/1 framed relay, parser, index and capture archive.

Modes:
  --input FILE          post-process an existing framed capture
  --pipe NAME           serve a named pipe and live-relay the framed stream

The relay never invents audio samples: it only splits validated framed packets,
writes their payload to a raw PCM file and records every packet in JSONL.
"""
from __future__ import annotations

import argparse
import json
import os
import struct
import sys
import time
import zlib
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import BinaryIO, Iterator

HEADER = 128
MAGIC = b"A3AP"
FLAG_FIRST = 1 << 0
FLAG_LAST = 1 << 1
FLAG_DISCONTINUITY = 1 << 2
FLAG_QUEUE_OVERFLOW = 1 << 3
FLAG_DEVICE_RESTART = 1 << 4
FLAG_PRODUCER_ERROR = 1 << 5


class FramedError(ValueError):
    pass


@dataclass
class Packet:
    offset: int
    sequence: int
    capture_id: int
    generation: int
    flags: int
    sample_start: int
    sample_count: int
    payload_bytes: int
    sample_rate: int
    channels: int
    sample_format: int
    planned_qpc: int
    actual_start_qpc: int
    actual_end_qpc: int
    qpc_frequency: int
    capture_epoch_qpc: int
    discontinuity_start: int
    discontinuity_count: int
    header_crc32: int
    payload_crc32: int
    payload: bytes

    @property
    def first(self) -> bool:
        return bool(self.flags & FLAG_FIRST)

    @property
    def last(self) -> bool:
        return bool(self.flags & FLAG_LAST)

    @property
    def discontinuity(self) -> bool:
        return bool(self.flags & FLAG_DISCONTINUITY)


def _unpack_header(h: bytes) -> tuple:
    if len(h) != HEADER:
        raise FramedError("short header")
    fields = struct.unpack_from("<4sHHIQIIIHHIQQ", h, 0)
    tail = struct.unpack_from("<QQQQQQQIIII", h, 56)
    return fields + tail


def _header_crc(h: bytes) -> int:
    return struct.unpack_from("<I", h, 112)[0]


def parse_header(h: bytes, offset: int) -> Packet:
    (magic, version, header_bytes, flags, capture_id, generation, sequence,
     rate, channels, sample_format, payload_bytes, sample_start,
     sample_count) = struct.unpack_from("<4sHHIQIIIHHIQQ", h, 0)
    (planned, actual_start, actual_end, qpc_frequency, capture_epoch,
     disc_start, disc_count, header_crc, payload_crc, reserved0,
     reserved1) = struct.unpack_from("<QQQQQQQIIII", h, 56)
    if magic != MAGIC:
        raise FramedError(f"bad magic at {offset}: {magic!r}")
    if version != 1 or header_bytes != HEADER:
        raise FramedError(f"bad version/header at {offset}")
    if reserved0 or reserved1:
        raise FramedError(f"nonzero reserved at {offset}")
    if rate != 44100 or channels != 2 or sample_format != 1:
        raise FramedError(f"unsupported format at {offset}")
    if payload_bytes != sample_count * 4:
        raise FramedError(
            f"payload/sample mismatch at {offset}: {payload_bytes}/{sample_count}")
    zeroed = h[:112] + b"\x00\x00\x00\x00" + h[116:]
    if zlib.crc32(zeroed) & 0xFFFFFFFF != header_crc:
        raise FramedError(f"header CRC mismatch at {offset}")
    return Packet(
        offset=offset, sequence=sequence, capture_id=capture_id,
        generation=generation, flags=flags, sample_start=sample_start,
        sample_count=sample_count, payload_bytes=payload_bytes,
        sample_rate=rate, channels=channels, sample_format=sample_format,
        planned_qpc=planned, actual_start_qpc=actual_start,
        actual_end_qpc=actual_end, qpc_frequency=qpc_frequency,
        capture_epoch_qpc=capture_epoch,
        discontinuity_start=disc_start, discontinuity_count=disc_count,
        header_crc32=header_crc, payload_crc32=payload_crc, payload=b"",
    )


class FramedParser:
    """Incremental parser; feed() accepts arbitrary chunk boundaries."""

    def __init__(self, strict: bool = True) -> None:
        self.buf = bytearray()
        self.base = 0
        self.strict = strict
        self.capture_id: int | None = None
        self.generation: int | None = None
        self.expected_sequence: int | None = None
        self.total_frames = 0
        self.packet_count = 0
        self._saw_first = False
        self._saw_last = False
        self.partial = False

    def feed(self, data: bytes) -> list[Packet]:
        self.buf.extend(data)
        out: list[Packet] = []
        while len(self.buf) >= HEADER:
            h = bytes(self.buf[:HEADER])
            pkt = parse_header(h, self.base)
            if len(self.buf) < HEADER + pkt.payload_bytes:
                self.partial = True
                break
            payload = bytes(self.buf[HEADER:HEADER + pkt.payload_bytes])
            if zlib.crc32(payload) & 0xFFFFFFFF != pkt.payload_crc32:
                raise FramedError(f"payload CRC mismatch at {pkt.offset}")
            pkt.payload = payload
            del self.buf[:HEADER + pkt.payload_bytes]
            self.base += HEADER + pkt.payload_bytes
            self.partial = False
            self._accept(pkt)
            out.append(pkt)
        return out

    def _accept(self, pkt: Packet) -> None:
        if self._saw_last:
            raise FramedError("packet after LAST")
        if not self._saw_first:
            if not pkt.first:
                raise FramedError("first packet lacks FIRST")
            self._saw_first = True
        if self.capture_id is None:
            self.capture_id = pkt.capture_id
            self.generation = pkt.generation
        elif (pkt.capture_id, pkt.generation) != (self.capture_id, self.generation):
            raise FramedError("capture/generation changed mid-stream")
        if self.expected_sequence is not None:
            expected = self.expected_sequence
            if pkt.sequence != expected:
                if not (pkt.discontinuity or pkt.flags & FLAG_QUEUE_OVERFLOW):
                    raise FramedError(
                        f"sequence gap {expected}->{pkt.sequence} without flag")
        self.expected_sequence = pkt.sequence + 1
        if pkt.discontinuity:
            if pkt.discontinuity_count == 0:
                raise FramedError("discontinuity flag with zero count")
        elif pkt.sample_start != self.total_frames:
            raise FramedError(
                f"sample gap {self.total_frames}->{pkt.sample_start} without flag")
        self.total_frames = max(self.total_frames, pkt.sample_start + pkt.sample_count)
        self.packet_count += 1
        if pkt.last:
            if pkt.sample_count != 0:
                raise FramedError("LAST packet has samples")
            self._saw_last = True

    def finish(self, require_last: bool) -> dict:
        if self.buf:
            self.partial = True
        if require_last and not self._saw_last:
            raise FramedError("stream ended without LAST")
        return {
            "packets": self.packet_count,
            "frames": self.total_frames,
            "seconds": self.total_frames / 44100.0,
            "capture_id": self.capture_id,
            "generation": self.generation,
            "saw_first": self._saw_first,
            "saw_last": self._saw_last,
            "partial_tail": bool(self.buf),
            "strict": self.strict,
        }

def _atomic_json(path: Path, value: object) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)


def run_file(input_path: Path, raw_path: Path, index_path: Path,
             manifest_path: Path, require_last: bool) -> dict:
    parser = FramedParser()
    index_path.parent.mkdir(parents=True, exist_ok=True)
    raw_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    with open(input_path, "rb") as src, open(raw_path, "wb") as raw, \
            open(index_path, "w", encoding="utf-8") as index:
        while True:
            chunk = src.read(1 << 20)
            if not chunk:
                break
            for pkt in parser.feed(chunk):
                raw.write(pkt.payload)
                rec = asdict(pkt)
                rec.pop("payload", None)
                index.write(json.dumps(rec, sort_keys=True) + "\n")
    summary = parser.finish(require_last=require_last)
    summary["source"] = str(input_path)
    summary["raw"] = str(raw_path)
    summary["index"] = str(index_path)
    summary["raw_bytes"] = parser.total_frames * 4
    _atomic_json(manifest_path, summary)
    return summary


def run_pipe(pipe_name: str, ready: Path, framed_path: Path, raw_path: Path,
             index_path: Path, manifest_path: Path, require_last: bool) -> dict:
    import pywintypes
    import win32file
    import win32pipe

    name = "\\\\.\\pipe\\" + pipe_name
    ready.parent.mkdir(parents=True, exist_ok=True)
    try:
        ready.unlink()
    except OSError:
        pass
    server = win32pipe.CreateNamedPipe(
        name, win32pipe.PIPE_ACCESS_INBOUND,
        win32pipe.PIPE_TYPE_BYTE | win32pipe.PIPE_READMODE_BYTE | win32pipe.PIPE_WAIT,
        1, 1 << 20, 1 << 20, 0, None)
    parser = FramedParser()
    framed_path.parent.mkdir(parents=True, exist_ok=True)
    raw_path.parent.mkdir(parents=True, exist_ok=True)
    index_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    ready.write_text("ready", encoding="utf-8")
    try:
        win32pipe.ConnectNamedPipe(server, None)
        with open(framed_path, "wb") as framed, open(raw_path, "wb") as raw, \
                open(index_path, "w", encoding="utf-8") as index:
            while True:
                try:
                    _hr, data = win32file.ReadFile(server, 65536)
                except pywintypes.error:
                    break
                if not data:
                    break
                framed.write(data)
                for pkt in parser.feed(data):
                    raw.write(pkt.payload)
                    rec = asdict(pkt)
                    rec.pop("payload", None)
                    index.write(json.dumps(rec, sort_keys=True) + "\n")
    finally:
        try:
            win32file.CloseHandle(server)
        except Exception:
            pass
        try:
            ready.unlink()
        except OSError:
            pass
    summary = parser.finish(require_last=require_last)
    summary.update({"source": f"pipe:{pipe_name}", "framed": str(framed_path),
                    "raw": str(raw_path), "index": str(index_path),
                    "raw_bytes": parser.total_frames * 4})
    _atomic_json(manifest_path, summary)
    return summary


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--input", type=Path)
    src.add_argument("--pipe")
    ap.add_argument("--ready", type=Path, default=Path("a3relay.ready"))
    ap.add_argument("--framed-out", type=Path, default=Path("capture.a3a"))
    ap.add_argument("--raw-out", type=Path, required=True)
    ap.add_argument("--index-out", type=Path, required=True)
    ap.add_argument("--manifest-out", type=Path, required=True)
    ap.add_argument("--require-last", action="store_true")
    a = ap.parse_args(argv)
    try:
        if a.input:
            summary = run_file(a.input, a.raw_out, a.index_out,
                               a.manifest_out, a.require_last)
        else:
            summary = run_pipe(a.pipe, a.ready, a.framed_out, a.raw_out,
                               a.index_out, a.manifest_out, a.require_last)
    except Exception as exc:
        print(f"FAIL: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
