"""用多个独立来源的整段转写生成"共识参考"的审校稿。

    python tools/consensus_ref.py --clip ja_asmr_0230
    python tools/consensus_ref.py              # 全部片段

来源：
  - whisper large-v3（整段上下文 + beam 5，离线最强，作为参考主干）
  - whisper small   （整段上下文，独立的第二意见）
  - SenseVoice 2024 （仅做对照展示）
  - YouTube 自动字幕（仅英语片段，源视频旁边有 *.vtt 时；第三意见）
被评测的是 SenseVoice，所以它**不参与**决定参考文本，否则就是被测系统给自己出题。
英语片段按**词**对齐（大小写、标点归一化后），日语按字符对齐。

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


def vtt_text(vtt_path, start, end):
    """YouTube 自动字幕（滚动式 VTT）里 [start, end) 区间的文字：每条 cue 只取新增的最后一行，去掉重复。"""
    import re
    if not vtt_path or not vtt_path.is_file():
        return ""
    words, last = [], ""
    stamp_re = re.compile(r"(\d+):(\d+):(\d+\.\d+) --> ")
    begin = None
    for line in vtt_path.read_text(encoding="utf-8").splitlines():
        match = stamp_re.match(line)
        if match:
            h, m, sec = match.groups()
            begin = int(h) * 3600 + int(m) * 60 + float(sec)
            continue
        if begin is None or not line.strip() or "<" in line or "-->" in line:
            continue
        if start <= begin < end and line.strip() != last:
            words.append(line.strip())
            last = line.strip()
    return " ".join(words)


def words_of(text):
    """英语对齐单位：小写、去标点的词。"""
    import re
    return re.findall(r"[a-z0-9']+", text.lower())


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


def word_disagreements(a, b, context=4):
    rows = []
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if tag == "equal":
            continue
        rows.append((" ".join(a[i1:i2]) or "∅", " ".join(b[j1:j2]) or "∅",
                     " ".join(a[max(0, i1 - context):i1] + ["【" + " ".join(a[i1:i2]) + "】"] + a[i2:i2 + context])))
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
    senses = {}

    out_dir = EVAL_DIR / "review"
    out_dir.mkdir(parents=True, exist_ok=True)
    for clip in clips:
        pcm = decode_audio(str(EVAL_DIR / "audio" / f"{clip['id']}.wav"), sampling_rate=SAMPLE_RATE)
        lang = clip["language"]
        if lang not in senses:
            senses[lang] = create_engine("sensevoice", "sensevoice-2024", language=lang)
        sense = senses[lang]
        large_rows = transcribe(large, pcm, lang)
        small_rows = transcribe(small, pcm, lang)
        sense_rows = []
        for start, end in speech_regions(pcm):
            text = sense.transcribe(pcm[int(start * SAMPLE_RATE): int(end * SAMPLE_RATE)], language=lang).text
            if text:
                sense_rows.append((round(start, 1), text))

        source = manifest["sources"][clip["source"]]
        vtt = next(iter(sorted((ROOT).glob(Path(source["path"]).stem.replace("[", "[[]") + "*.vtt"))), None)
        youtube = vtt_text(vtt, clip["start"], clip["start"] + clip["duration"]) if lang != "ja" else ""

        if lang == "ja":
            a = normalize("".join(t for _, t in large_rows))
            b = normalize("".join(t for _, t in small_rows))
        else:
            a = words_of(" ".join(t for _, t in large_rows))
            b = words_of(" ".join(t for _, t in small_rows))
        ratio = difflib.SequenceMatcher(None, a, b, autojunk=False).ratio()
        diffs = disagreements(a, b) if lang == "ja" else word_disagreements(a, b)
        if youtube:
            c = words_of(youtube)
            ratio_yt = difflib.SequenceMatcher(None, a, c, autojunk=False).ratio()
            diffs_yt = word_disagreements(a, c)

        lines = [f"# {clip['id']}  large/small 一致度 {ratio:.3f}，分歧 {len(diffs)} 处", "",
                 "## large-v3", *[f"{stamp(s)} {t}" for s, t in large_rows], "",
                 "## small", *[f"{stamp(s)} {t}" for s, t in small_rows], "",
                 "## SenseVoice 2024（被测对象，仅对照）", *[f"{stamp(s)} {t}" for s, t in sense_rows], "",
                 "## 分歧（large | small | large 上下文）"]
        lines += [f"- {x} | {y} | {c}" for x, y, c in diffs]
        if youtube:
            lines += ["", f"## YouTube 自动字幕（与 large-v3 一致度 {ratio_yt:.3f}，分歧 {len(diffs_yt)} 处）",
                      youtube, "", "## 分歧（large | YouTube | large 上下文）"]
            lines += [f"- {x} | {y} | {c}" for x, y, c in diffs_yt]
        path = out_dir / f"{clip['id']}.md"
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        print(f"{clip['id']}: 一致度 {ratio:.3f}，分歧 {len(diffs)} 处 → {path.relative_to(ROOT)}", flush=True)


if __name__ == "__main__":
    main()
