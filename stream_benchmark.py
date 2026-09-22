import argparse
import tempfile
import time
import wave
from pathlib import Path

from faster_whisper import WhisperModel


def main() -> None:
    parser = argparse.ArgumentParser(description="Approximate streaming Whisper benchmark")
    parser.add_argument("audio", type=Path)
    parser.add_argument("--model", default="small", choices=("tiny", "base", "small"))
    parser.add_argument("--language", default="ja", choices=("ja", "en"))
    parser.add_argument("--chunk", type=float, default=3.0)
    args = parser.parse_args()

    with wave.open(str(args.audio), "rb") as source:
        params = source.getparams()
        frames_per_chunk = int(params.framerate * args.chunk)
        chunks = []
        while True:
            frames = source.readframes(frames_per_chunk)
            if not frames:
                break
            chunks.append(frames)

    model_started = time.perf_counter()
    model = WhisperModel(args.model, device="cpu", compute_type="int8")
    print(f"model_load_seconds={time.perf_counter() - model_started:.2f}")

    total_audio = len(chunks) * args.chunk
    total_compute = 0.0
    with tempfile.TemporaryDirectory() as temp_dir:
        for index, frames in enumerate(chunks):
            chunk_path = Path(temp_dir) / f"chunk_{index:03d}.wav"
            with wave.open(str(chunk_path), "wb") as target:
                target.setparams(params)
                target.writeframes(frames)
            started = time.perf_counter()
            segments, _ = model.transcribe(
                str(chunk_path), language=args.language, beam_size=1,
                vad_filter=True, condition_on_previous_text=False,
            )
            text = " ".join(segment.text.strip() for segment in segments).strip()
            elapsed = time.perf_counter() - started
            total_compute += elapsed
            print(f"chunk={index:02d} audio={args.chunk:.1f}s compute={elapsed:.2f}s text={text}")

    print(f"audio_seconds={total_audio:.2f}")
    print(f"compute_seconds={total_compute:.2f}")
    print(f"compute_ratio={total_compute / total_audio:.3f}")


if __name__ == "__main__":
    main()
