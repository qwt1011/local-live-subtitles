import argparse
import time
from pathlib import Path

from faster_whisper import WhisperModel


def main() -> None:
    parser = argparse.ArgumentParser(description="Benchmark local Whisper transcription")
    parser.add_argument("audio", type=Path)
    parser.add_argument("--model", default="base", choices=("tiny", "base", "small"))
    parser.add_argument("--language", choices=("ja", "en"), help="Force language detection")
    parser.add_argument("--beam-size", type=int, default=1)
    args = parser.parse_args()

    if not args.audio.exists():
        raise SystemExit(f"Audio file not found: {args.audio}")

    started = time.perf_counter()
    model = WhisperModel(args.model, device="cpu", compute_type="int8")
    load_seconds = time.perf_counter() - started

    started = time.perf_counter()
    segments, info = model.transcribe(
        str(args.audio),
        language=args.language,
        beam_size=args.beam_size,
        vad_filter=True,
        condition_on_previous_text=True,
    )
    rows = list(segments)
    transcribe_seconds = time.perf_counter() - started

    print(f"model={args.model}")
    print(f"detected_language={info.language} probability={info.language_probability:.3f}")
    print(f"model_load_seconds={load_seconds:.2f}")
    print(f"transcribe_seconds={transcribe_seconds:.2f}")
    print("--- transcript ---")
    for row in rows:
        print(f"[{row.start:7.2f} -> {row.end:7.2f}] {row.text.strip()}")


if __name__ == "__main__":
    main()
