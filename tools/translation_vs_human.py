"""翻译质量对人工译文：把一次评测回放的定稿逐句翻译，和人工中文字幕（内嵌字幕 OCR）比较。

    python tools/translation_vs_human.py runs/eval/en_sv runs/eval/en_pk_v2 --zh eval/zh_refs/en_asmr_01.json

和 tools/asr_ceiling.py 的区别：那边比的是"同一个翻译模型翻 ASR 原文 vs 翻参考原文"，只看识别拖累了多少；
这里比的是"整条链路的译文 vs 人工译文"，识别和翻译的误差都算在内，是用户真正看到的东西。

对齐按时间：每句定稿取 [audio_start, audio_end]，把时间上重叠的人工字幕拼起来当参考。
人工字幕和我们的断句不一致（一条人工字幕可能跨两句定稿），所以除了逐句 chrF，
还给出每段整体拼接后的 chrF——后者不受断句影响，更适合比较两个配置。

注意：人工译文是意译、有个人风格，chrF 绝对值不高是正常的（同一句话两个人翻也很难超过 0.5），
只看两个配置之间的相对差异。
"""

import argparse
import json
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools.asr_ceiling import chrf  # noqa: E402


def finals(path):
    """每句定稿（取最高 revision 的原文），带音频时间（片段内相对时间）。"""
    latest = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        row = json.loads(line)
        if row.get("type") == "header" or not row.get("is_final") or not row.get("text"):
            continue
        key = row["segment_id"]
        if key not in latest or row["revision"] >= latest[key]["revision"]:
            latest[key] = row
    return [latest[k] for k in sorted(latest)]


def human_between(cues, start, end, pad=0.3):
    """时间上与 [start, end] 重叠的人工字幕，按出现顺序拼接（同一条只取一次）。"""
    picked = [c["text"] for c in cues if c["end"] > start - pad and c["start"] < end + pad]
    return "".join(picked)


def main():
    parser = argparse.ArgumentParser(description="链路译文 vs 人工译文")
    parser.add_argument("eval_dirs", nargs="+", type=Path)
    parser.add_argument("--zh", type=Path, required=True, help="tools/ocr_subtitles.py 的输出")
    parser.add_argument("--engine", default="hymt")
    parser.add_argument("--out", type=Path, default=ROOT / "runs" / "translation_vs_human")
    args = parser.parse_args()

    manifest = json.loads((ROOT / "eval" / "manifest.json").read_text(encoding="utf-8"))
    clips = {c["id"]: c for c in manifest["clips"]}
    cues = json.loads(args.zh.read_text(encoding="utf-8"))["cues"]

    from app.translate.factory import create_translator
    translator = create_translator(args.engine)

    summary, details = {}, {}
    for eval_dir in args.eval_dirs:
        tag = eval_dir.name
        rows, per_clip = [], []
        for path in sorted(eval_dir.glob("*.jsonl")):
            clip = clips.get(path.stem)
            if clip is None or clip["language"] != "en":
                continue
            offset = clip["start"]
            ours, theirs = [], []
            for final in finals(path):
                start, end = offset + final["audio_start"], offset + final["audio_end"]
                human = human_between(cues, start, end)
                zh = translator.translate(final["text"], source="en", target="zh")
                ours.append(zh)
                if human:
                    theirs.append(human)
                    rows.append({"clip": path.stem, "start": round(start, 1), "asr": final["text"],
                                 "zh": zh, "human": human, "chrf": round(chrf(zh, human), 3)})
            # 整段：我们全部译文 vs 片段时间范围内全部人工字幕
            clip_human = human_between(cues, offset, offset + clip["duration"], pad=0)
            per_clip.append(round(chrf("".join(ours), clip_human), 3))
            print(f"{tag} {path.stem}: 整段 chrF {per_clip[-1]:.3f}（{len(ours)} 句）", flush=True)
        summary[tag] = {"clips": per_clip, "clip_mean": round(statistics.fmean(per_clip), 3),
                        "sentence_median": round(statistics.median(r["chrf"] for r in rows), 3),
                        "sentences": len(rows)}
        details[tag] = rows

    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "summary.json").write_text(json.dumps({"summary": summary, "details": details},
                                                      ensure_ascii=False, indent=1), encoding="utf-8")
    lines = ["# 链路译文 vs 人工译文", "", f"人工译文：`{args.zh.as_posix()}`（内嵌字幕 OCR）", ""]
    for tag, rows in details.items():
        lines += [f"## {tag}", ""]
        for r in sorted(rows, key=lambda r: r["chrf"])[:15]:
            lines += [f"- [{r['clip']} {r['start']}s] chrF {r['chrf']}", f"  - 原文：{r['asr']}",
                      f"  - 我们：{r['zh']}", f"  - 人工：{r['human']}"]
        lines.append("")
    (args.out / "review.md").write_text("\n".join(lines), encoding="utf-8")

    print()
    for tag, s in summary.items():
        print(f"{tag:20} 整段 chrF 均值 {s['clip_mean']:.3f}  逐句中位 {s['sentence_median']:.3f}  "
              f"（{s['sentences']} 句）  逐段 {s['clips']}")
    print(f"逐句对照（最差 15 句）：{args.out / 'review.md'}")


if __name__ == "__main__":
    main()
