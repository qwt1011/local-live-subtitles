"""Diagnostic: is the 13-19 s per-chunk blowup caused by Whisper temperature fallback?

Runs the same 3 s chunks twice: once with the default temperature ladder
([0.0, 0.2, ... 1.0], which enables retry-on-failure) and once with temperature=0.0
(no retry). Also reports how often the retry ladder would have been entered.

Run:
    C:\\text\\.venv\\Scripts\\python.exe diag_fallback.py sample_0230_0300.wav --model base
"""

import argparse
import time
from pathlib import Path

from faster_whisper import WhisperModel
from faster_whisper.audio import decode_audio


def run(model, samples, chunk, language, temperature, vad):
    step = int(chunk * 16000)
    rows = []
    total = 0.0
    for index, start in enumerate(range(0, len(samples) - step + 1, step)):
        clip = samples[start : start + step]
        began = time.perf_counter()
        segments, info = model.transcribe(
            clip,
            language=language,
            beam_size=1,
            vad_filter=vad,
            condition_on_previous_text=False,
            temperature=temperature,
        )
        text = " ".join(s.text.strip() for s in segments).strip()
        elapsed = time.perf_counter() - began
        total += elapsed
        rows.append((index, elapsed, text, info.duration_after_vad))
    return rows, total


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("audio", type=Path)
    parser.add_argument("--model", default="base", choices=("tiny", "base", "small"))
    parser.add_argument("--language", default="ja")
    parser.add_argument("--chunk", type=float, default=3.0)
    args = parser.parse_args()

    samples = decode_audio(str(args.audio), sampling_rate=16000)
    audio_seconds = len(samples) / 16000
    print(f"duration={audio_seconds:.2f}s chunk={args.chunk}s")

    model = WhisperModel(args.model, device="cpu", compute_type="int8")

    cases = [
        ("ladder + vad", [0.0, 0.2, 0.4, 0.6, 0.8, 1.0], True),
        ("t=0    + vad", [0.0], True),
        ("ladder no vad", [0.0, 0.2, 0.4, 0.6, 0.8, 1.0], False),
    ]

    results = {}
    for name, temperature, vad in cases:
        rows, total = run(model, samples, args.chunk, args.language, temperature, vad)
        results[name] = (rows, total)
        slowest = max(rows, key=lambda r: r[1])
        print(
            f"{name:14s} total={total:6.2f}s ratio={total / audio_seconds:5.3f} "
            f"slowest_chunk={slowest[0]:02d} {slowest[1]:5.2f}s"
        )
        for index, elapsed, text, after_vad in rows:
            print(f"    chunk={index:02d} {elapsed:5.2f}s audio_after_vad={after_vad:4.2f}s text={text[:70]}")
        print(flush=True)

    base_rows = results["ladder + vad"][0]
    fixed_rows = results["t=0    + vad"][0]
    saved = results["ladder + vad"][1] - results["t=0    + vad"][1]
    print(f"temperature=0.0 saves {saved:.2f}s over {audio_seconds:.1f}s of audio "
          f"({saved / audio_seconds:.2f}x realtime)")
    for (index, slow, text, _), (_, fast, fast_text, _) in zip(base_rows, fixed_rows):
        if slow > fast * 2 and slow > 1.5:
            print(f"  chunk {index:02d}: {slow:.2f}s -> {fast:.2f}s   "
                  f"ladder='{text[:40]}' t0='{fast_text[:40]}'")


if __name__ == "__main__":
    main()
