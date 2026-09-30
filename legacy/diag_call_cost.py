"""Diagnostic: how much does one model.transcribe() call cost as a function of audio length?

The point is to separate the FIXED per-call cost (feature extraction + encoder over the
padded 30 s window + decoder prompt setup) from the marginal cost of extra audio.

Run:
    C:\\text\\.venv\\Scripts\\python.exe diag_call_cost.py sample_0230_0300.wav --model base
"""

import argparse
import statistics
import tempfile
import time
import wave
from pathlib import Path

import numpy as np

from faster_whisper import WhisperModel
from faster_whisper.audio import decode_audio


def write_wav(path: Path, samples: np.ndarray, rate: int = 16000) -> None:
    pcm = np.clip(samples, -1.0, 1.0)
    pcm = (pcm * 32767.0).astype("<i2")
    with wave.open(str(path), "wb") as target:
        target.setnchannels(1)
        target.setsampwidth(2)
        target.setframerate(rate)
        target.writeframes(pcm.tobytes())


def timed_call(model, source, language, vad, repeats):
    """Return (best_seconds, text) for the fastest of `repeats` calls."""
    best = None
    text = ""
    for _ in range(repeats):
        started = time.perf_counter()
        segments, _info = model.transcribe(
            source,
            language=language,
            beam_size=1,
            vad_filter=vad,
            condition_on_previous_text=False,
        )
        text = " ".join(s.text.strip() for s in segments).strip()
        elapsed = time.perf_counter() - started
        best = elapsed if best is None else min(best, elapsed)
    return best, text


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("audio", type=Path)
    parser.add_argument("--model", default="base", choices=("tiny", "base", "small"))
    parser.add_argument("--language", default="ja")
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--durations", default="1,2,3,5,10,20,30")
    args = parser.parse_args()

    samples = decode_audio(str(args.audio), sampling_rate=16000)
    total = len(samples) / 16000
    print(f"source={args.audio.name} duration={total:.2f}s samples={len(samples)}")

    started = time.perf_counter()
    model = WhisperModel(args.model, device="cpu", compute_type="int8")
    print(f"model_load_seconds={time.perf_counter() - started:.2f}")

    durations = [float(d) for d in args.durations.split(",")]

    # Warm up once so the first call is not paying lazy-initialisation costs.
    timed_call(model, samples[:16000], args.language, False, 1)

    rows = []
    for duration in durations:
        if duration > total:
            continue
        clip = samples[: int(duration * 16000)]
        for vad in (False, True):
            best, text = timed_call(model, clip, args.language, vad, args.repeats)
            rows.append((duration, vad, best, text))
            print(
                f"duration={duration:5.1f}s vad={str(vad):5s} "
                f"call={best:6.3f}s per_audio_sec={best / duration:7.3f} "
                f"text={text[:60]}",
                flush=True,
            )

    with tempfile.TemporaryDirectory() as temp_dir:
        path = Path(temp_dir) / "clip.wav"
        write_wav(path, samples[: int(3 * 16000)])
        path_best, _ = timed_call(model, str(path), args.language, True, args.repeats)
        array_best, _ = timed_call(model, samples[: int(3 * 16000)], args.language, True, args.repeats)
        print(f"transport_3s_vad path={path_best:.3f}s array={array_best:.3f}s delta={path_best - array_best:.3f}s")

    print("--- fixed-cost estimate (linear fit over the two largest non-VAD points) ---")
    points = [(d, t) for d, vad, t, _ in rows if not vad]
    if len(points) >= 2:
        (d1, t1), (d2, t2) = points[0], points[-1]
        slope = (t2 - t1) / (d2 - d1)
        intercept = t1 - slope * d1
        print(f"marginal_seconds_per_audio_second={slope:.4f}  fixed_seconds_per_call={intercept:.3f}")
        for duration, vad, t, _ in rows:
            print(f"  measured {duration:5.1f}s vad={str(vad):5s} {t:6.3f}s  model={intercept + slope * duration:6.3f}s")
    print(f"median_call_seconds={statistics.median([t for _, _, t, _ in rows]):.3f}")


if __name__ == "__main__":
    main()
