"""用多个独立来源的整段转写生成"共识参考"的审校稿。

    python tools/consensus_ref.py --clip ja_asmr_0230
    python tools/consensus_ref.py              # 全部片段

来源：
  - whisper large-v3（整段上下文 + beam 5，离线最强，作为参考主干）
  - whisper small   （整段上下文，独立的第二意见）
  - SenseVoice 2024 （仅做对照展示）
被评测的是 SenseVoice，所以它**不参与**决定参考文本，否则就是被测系统给自己出题。

不按 VAD 句子逐句比：词级时间戳会漂移，把词分到相邻句里，造成大量假分歧。
改为整段字符级对齐，只把 large 与 small 真正不一致的片段列出来。

输出 eval/review/<clip>.md：
  - large-v3 的逐句全文（带时间）
  - 分歧表：位置、large、small、上下文
审校后写入 eval/refs/<clip>.txt。
"""

import argparse
import difflib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from faster_whisper.audio import decode_audio  # noqa: E402

from app.audio.vad import speech_regions  # noqa: E402
from tools.metrics import normalize  # noqa: E402

SAMPLE_RATE = 16000
EVAL_DIR = ROOT / "eval"


def transcribe(engine, pcm, language):
    segments, _ = engine.model.transcribe(
        pcm, language=language, beam_size=5, condition_on_previous_text=True, vad_filter=True,
    )
    return [(round(s.start, 1), s.text.strip()) for s in segments if s.text.strip()]


def stamp(seconds):
    return f"[{int(seconds // 60):02d}:{seconds % 60:04.1f}]"


def disagreements(a, b, context=6):
    rows = []
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if tag == "equal":
            continue
        rows.append((a[i1:i2] or "∅", b[j1:j2] or "∅",
                     a[max(0, i1 - context):i1] + "【" + a[i1:i2] + "】" + a[i2:i2 + context]))
    return rows


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--clip", nargs="*", default=None)
    args = parser.parse_args()

    from app.asr.factory import create_engine
    from app.asr.faster_whisper_engine import WhisperEngine

    manifest = json.loads((EVAL_DIR / "manifest.json").read_text(encoding="utf-8"))
    clips = [c for c in manifest["clips"] if not args.clip or c["id"] in args.clip]

    print("加载 large-v3 / small / sensevoice ...", flush=True)
    large = WhisperEngine("large-v3")
    small = WhisperEngine("small")
    sense = create_engine("sensevoice", "sensevoice-2024", language="ja")

    out_dir = EVAL_DIR / "review"
    out_dir.mkdir(parents=True, exist_ok=True)
    for clip in clips:
        pcm = decode_audio(str(EVAL_DIR / "audio" / f"{clip['id']}.wav"), sampling_rate=SAMPLE_RATE)
        lang = clip["language"]
        large_rows = transcribe(large, pcm, lang)
        small_rows = transcribe(small, pcm, lang)
        sense_rows = []
        for start, end in speech_regions(pcm):
            text = sense.transcribe(pcm[int(start * SAMPLE_RATE): int(end * SAMPLE_RATE)], language=lang).text
            if text:
                sense_rows.append((round(start, 1), text))

        a = normalize("".join(t for _, t in large_rows))
        b = normalize("".join(t for _, t in small_rows))
        ratio = difflib.SequenceMatcher(None, a, b, autojunk=False).ratio()
        diffs = disagreements(a, b)

        lines = [f"# {clip['id']}  large/small 一致度 {ratio:.3f}，分歧 {len(diffs)} 处", "",
                 "## large-v3", *[f"{stamp(s)} {t}" for s, t in large_rows], "",
                 "## small", *[f"{stamp(s)} {t}" for s, t in small_rows], "",
                 "## SenseVoice 2024（被测对象，仅对照）", *[f"{stamp(s)} {t}" for s, t in sense_rows], "",
                 "## 分歧（large | small | large 上下文）"]
        lines += [f"- {x} | {y} | {c}" for x, y, c in diffs]
        path = out_dir / f"{clip['id']}.md"
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        print(f"{clip['id']}: 一致度 {ratio:.3f}，分歧 {len(diffs)} 处 → {path.relative_to(ROOT)}", flush=True)


if __name__ == "__main__":
    main()
