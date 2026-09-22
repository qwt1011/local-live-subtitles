import argparse
import tempfile
import time
import wave
from pathlib import Path

from faster_whisper import WhisperModel


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("audio", type=Path)
    parser.add_argument("--model", default="base", choices=("base", "small"))
    parser.add_argument("--language", default="ja", choices=("ja", "en"))
    parser.add_argument("--step", type=float, default=1.5)
    parser.add_argument("--window", type=float, default=6.0)
    args = parser.parse_args()

    with wave.open(str(args.audio), "rb") as source:
        params = source.getparams()
        raw = source.readframes(source.getnframes())
    bytes_per_second = params.framerate * params.sampwidth * params.nchannels
    duration = len(raw) / bytes_per_second

    started = time.perf_counter()
    model = WhisperModel(args.model, device="cpu", compute_type="int8")
    print(f"model_load_seconds={time.perf_counter() - started:.2f}")

    total = 0.0
    with tempfile.TemporaryDirectory() as temp_dir:
        index = 0
        position = 0.0
        while position < duration:
            begin = max(0.0, position + args.step - args.window)
            end = min(duration, position + args.step)
            start_byte = int(begin * bytes_per_second)
            end_byte = int(end * bytes_per_second)
            path = Path(temp_dir) / f"window_{index:03d}.wav"
            with wave.open(str(path), "wb") as target:
                target.setparams(params)
                target.writeframes(raw[start_byte:end_byte])
            tick = time.perf_counter()
            segments, _ = model.transcribe(
                str(path), language=args.language, beam_size=1,
                vad_filter=True, condition_on_previous_text=True,
            )
            text = " ".join(s.text.strip() for s in segments).strip()
            elapsed = time.perf_counter() - tick
            total += elapsed
            print(f"step={position + args.step:5.1f}s window={begin:5.1f}-{end:5.1f}s compute={elapsed:5.2f}s text={text}")
            position += args.step
            index += 1
    print(f"audio_seconds={duration:.2f}")
    print(f"compute_seconds={total:.2f}")
    print(f"compute_ratio={total / duration:.3f}")


if __name__ == "__main__":
    main()
