"""全上下文离线转录，用于生成回放台架的**伪参考文本**。

真实评测需要人工校对过的转写。当前没有，所以退而求其次：
用更大模型 + 整段上下文（30 秒一次性喂入）的输出作为伪参考，
它至少比"3 秒独立块"的输出可靠，可以用来观察流式内核有没有把质量做坏。

注意：这不是 ground truth，只能做**相对比较**（同一份参考下比较不同流水线），
不能当作绝对准确率。等有人工校对文本后，用同样的接口替换即可。

用法：
    python tools/offline_reference.py --wav sample_0230_0300.wav --model small --out reference_small.txt
"""

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from faster_whisper.audio import decode_audio  # noqa: E402

from app.asr.faster_whisper_engine import WhisperEngine  # noqa: E402

SAMPLE_RATE = 16000


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--wav", type=Path, required=True)
    parser.add_argument("--model", default="small", choices=("tiny", "base", "small"))
    parser.add_argument("--language", default="ja")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--condition-on-previous", action="store_true",
                        help="整段转录时允许使用上文（默认开启整段上下文，仅此处为 True）")
    args = parser.parse_args()

    pcm = decode_audio(str(args.wav), sampling_rate=SAMPLE_RATE)
    print(f"duration={len(pcm) / SAMPLE_RATE:.2f}s model={args.model}")

    engine = WhisperEngine(args.model)
    began = time.perf_counter()
    result = engine.transcribe(
        pcm,
        language=args.language,
        condition_on_previous_text=True,   # 整段离线转录，上下文是免费的
    )
    elapsed = time.perf_counter() - began

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(result.text + "\n", encoding="utf-8")

    print(f"offline_transcribe_seconds={elapsed:.2f} ratio={elapsed / (len(pcm) / SAMPLE_RATE):.3f}")
    print("--- segments ---")
    for row in result.segments:
        print(f"[{row['start']:7.2f} -> {row['end']:7.2f}] {row['text']}")
    print(f"wrote {args.out}")


if __name__ == "__main__":
    main()
