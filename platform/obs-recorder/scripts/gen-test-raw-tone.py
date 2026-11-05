#!/usr/bin/env python3
"""Generate a raw s16le interleaved stereo PCM test file for S2a.

Two modes:
  * file mode (default): write a complete static file quickly, e.g. 17 s of a
    440 Hz tone at 44.1 kHz / 2 channels (VCMI game-audio.raw format:
    s16le interleaved, 44100 Hz, stereo);
  * --stream mode: create/truncate the file and then append 100 ms chunks in
    approximately real time for --seconds, simulating VCMI growing
    game-audio.raw while the recorder is running (used to test the default
    "start at EOF, read only newly appended data" behaviour).

No playback, no audio device, no ffplay.
"""
import argparse
import math
import os
import sys
import time


def make_chunk(freq, amp, rate, frames, start_phase):
    import array

    data = array.array("h")
    phase = start_phase
    step = 2.0 * math.pi * freq / rate
    scale = int(round(amp * 32767.0))
    for _ in range(frames):
        s = int(round(math.sin(phase) * scale))
        data.append(s)
        data.append(s)
        phase += step
        if phase > 2.0 * math.pi:
            phase -= 2.0 * math.pi
    return data.tobytes(), phase


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", required=True)
    ap.add_argument("--seconds", type=float, default=17.0)
    ap.add_argument("--freq", type=float, default=440.0)
    ap.add_argument("--amplitude", type=float, default=0.25)
    ap.add_argument("--rate", type=int, default=44100)
    ap.add_argument("--channels", type=int, default=2)
    ap.add_argument("--stream", action="store_true",
                    help="create/truncate the file, then append 100 ms chunks in real time")
    args = ap.parse_args()
    if args.channels != 2:
        raise SystemExit("this helper only writes 2-channel interleaved PCM")

    frames = int(round(args.seconds * args.rate))
    phase = 0.0
    tmp = args.out + ".tmp"

    if not args.stream:
        with open(tmp, "wb") as f:
            remaining = frames
            while remaining > 0:
                n = min(4410, remaining)
                blob, phase = make_chunk(args.freq, args.amplitude, args.rate, n, phase)
                f.write(blob)
                remaining -= n
        os.replace(tmp, args.out)
        print("WROTE %s frames=%d bytes=%d" % (args.out, frames, os.path.getsize(args.out)),
              flush=True)
        return 0

    # streaming mode: empty file first, then append in (approximately) real time
    with open(args.out, "wb") as f:
        f.flush()
        os.fsync(f.fileno())
    print("STREAM_READY %s" % args.out, flush=True)
    deadline = time.time() + args.seconds
    with open(args.out, "ab") as f:
        next_time = time.time()
        while time.time() < deadline:
            blob, phase = make_chunk(args.freq, args.amplitude, args.rate, 4410, phase)
            f.write(blob)
            f.flush()
            next_time += 0.1
            delay = next_time - time.time()
            if delay > 0:
                time.sleep(delay)
            else:
                # do not accumulate unbounded lag if the writer falls behind
                next_time = time.time()
    print("STREAM_DONE %s bytes=%d" % (args.out, os.path.getsize(args.out)), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
