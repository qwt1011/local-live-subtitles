"""离线研究"长句分段翻译"：用会话日志里的 partial 回放"说到一半"，模拟在哪里能提前切出一段先翻译。

    python tools/sim_partial_translate.py runs/partial_study/*.jsonl runs/live_hymt.jsonl

不改线上任何逻辑。对每句定稿，按到达顺序回放它的 partial：

1. **稳定前缀**：最近 --agree 次 partial 的"内容"（去掉空格和标点）的公共前缀。
   partial 的末尾会反复改（ねし→寝しても→にしても），而 SenseVoice 每次都在末尾补一个「。」，
   所以末尾和标点都不能信，只信几次都没变的内容。
2. **切分点**：取最新 partial 里的标点 / 空格，映射回内容位置；切分点后面还必须有稳定内容，
   证明它不是末尾补出来的「。」。分三档，分开统计：
   - A 句末：。？！，或 ね/よ/わ/かしら 后跟、或空格（两句被 VAD 粘在一起的情况）；
   - B 从句：けど/から/ので/て/し/が/ば/たら/ても 后跟、；
   - 其余的、不切，只计数。
3. 切出的片段用 Hy-MT2 翻译，"可用时刻" = 切分那次 partial 的到达时刻 + 实测翻译耗时
   （离线测的，不含抢占，偏乐观）。与日志里整句译文的到达时刻比，得到提前量。

输出 runs/partial_study/sim.json 和给人看的 runs/partial_study/review.md。
"""

import argparse
import json
import re
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

PUNCT = set("。、，,.？?！!…・「」『』　 ")
SENTENCE_END = set("。？?！!")
FINAL_PARTICLES = ("かしら", "ね", "よ", "わ", "ぞ", "ぜ")
CLAUSE_ENDINGS = ("けど", "けれど", "から", "ので", "ても", "でも", "たら", "て", "で", "し", "が", "ば")
NEGATION = set("不没别非未无")


def arrival(event):
    return event.get("recv_wall", event.get("finish_wall"))


def content(text):
    return "".join(ch for ch in text if ch not in PUNCT)


def boundaries(raw):
    """最新 partial 里的候选切分点：[(内容位置, 档位)]，内容位置 = 切分点前面的内容字数。"""
    found = []
    body = ""
    tail = raw.rstrip(" 　")
    for i, ch in enumerate(raw):
        if ch not in PUNCT:
            body += ch
            continue
        if not body or (found and found[-1][0] == len(body)):
            continue        # 连续标点只算一次
        if i >= len(tail) - 1:
            # 末尾的标点：SenseVoice 每次都会补「。」，只有句末助词/问句才算，另归一档（A2）
            if ch in SENTENCE_END and (body.endswith(FINAL_PARTICLES) or ch in "？?"):
                found.append((len(body), "A2"))
            break
        if ch in SENTENCE_END:
            tier = "A"
        elif body.endswith(FINAL_PARTICLES) and ch in "、，, 　":
            tier = "A"
        elif ch in "、，," and body.endswith(CLAUSE_ENDINGS):
            tier = "B"
        elif ch in "、，,":
            tier = "other"
        else:
            continue        # 普通空格：SenseVoice 常在词间乱插空格，不代表边界
        found.append((len(body), tier))
    return found


def raw_span(raw, start, end):
    """按内容位置 [start, end) 从原始文本里取出片段，保留中间的标点。"""
    out, idx = "", 0
    for ch in raw:
        if ch not in PUNCT:
            if start <= idx < end:
                out += ch
            idx += 1
        elif start < idx <= end:
            out += ch
    # SenseVoice 在词间乱插的空格对日语没有意义，去掉再送去翻译
    return out.replace(" ", "").replace("　", "").strip("、，,")


def simulate(events, agree, min_chars, tiers):
    events = sorted((e for e in events if "segment_id" in e), key=arrival)
    by_seg = {}
    for e in events:
        by_seg.setdefault(e["segment_id"], []).append(e)

    sentences = []
    for seg, evs in sorted(by_seg.items()):
        final = next((e for e in evs if e.get("is_final") and e.get("text") and not e.get("translation")), None)
        if final is None:
            continue
        full_tr = next((e for e in evs if e.get("translation")), None)
        partials = [e for e in evs if not e.get("is_final") and e.get("text") and not e.get("translation")]
        history, cuts, committed = [], [], 0
        for p in partials:
            history.append(content(p["text"]))
            if len(history) < agree:
                continue
            recent = history[-agree:]
            stable = len(recent[0])
            for other in recent[1:]:
                stable = min(stable, len(other))
                stable = next((i for i in range(stable) if recent[0][i] != other[i]), stable)
            for pos, tier in boundaries(p["text"]):
                # 句中切分点后面至少还要有 2 个稳定字，证明它不是末尾补出来的标点；
                # 末尾切分点（A2）要求到它为止的内容在最近 agree 次 partial 里都没变。
                need = stable if tier == "A2" else stable - 2
                if tier == "A2" and len(history[-1]) != pos:
                    continue
                if tier[0] in tiers and committed + min_chars <= pos <= need:
                    cuts.append({"at": arrival(p), "audio": p["audio_end"], "tier": tier,
                                 "start": committed, "end": pos,
                                 "text": raw_span(p["text"], committed, pos)})
                    committed = pos
        final_content = content(final["text"])
        for c in cuts:
            # ASR 在切分之后又改了这段内容：先出的译文会和最终原文对不上
            c["asr_changed"] = final_content[c["start"]:c["end"]] != content(c["text"])
        rest = raw_span(final["text"], committed, len(final_content)) if cuts else ""
        sentences.append({
            "seg": seg, "text": final["text"], "speech": final["audio_end"] - final["audio_start"],
            "final_at": arrival(final),
            "full_translation": full_tr["translation"] if full_tr else "",
            "full_at": arrival(full_tr) if full_tr else None,
            "cuts": cuts, "rest": rest,
        })
    return sentences


def overlap(a, b):
    """中文字符二元组的 Dice 系数，只用来粗筛"两段译文意思可能不一致"的句子。"""
    grams = lambda s: {s[i:i + 2] for i in range(len(s) - 1)}  # noqa: E731
    ga, gb = grams(content(a)), grams(content(b))
    return 2 * len(ga & gb) / (len(ga) + len(gb)) if ga and gb else 0.0


def main():
    parser = argparse.ArgumentParser(description="离线模拟长句分段翻译")
    parser.add_argument("logs", type=Path, nargs="+")
    parser.add_argument("--agree", type=int, default=2, help="连续几次 partial 一致才算稳定")
    parser.add_argument("--min-chars", type=int, default=6, help="片段最少内容字数，太短的不值得单独翻译")
    parser.add_argument("--tiers", default="AB", help="启用哪些档位的切分点")
    parser.add_argument("--long", type=float, default=4.0, help="长句阈值（秒）")
    parser.add_argument("--out", type=Path, default=Path("runs/partial_study"))
    parser.add_argument("--no-translate", action="store_true", help="只统计切分，不跑翻译")
    args = parser.parse_args()

    all_rows = []
    for path in args.logs:
        events = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
        for row in simulate(events, args.agree, args.min_chars, set(args.tiers)):
            row["log"] = path.stem
            all_rows.append(row)

    translator = None
    if not args.no_translate:
        from app.translate.factory import create_translator
        translator = create_translator("hymt")

    for row in all_rows:
        for c in row["cuts"]:
            if translator:
                c["translation"] = translator.translate(c["text"])
                c["translate_seconds"] = translator.last_seconds
                c["ready_at"] = c["at"] + c["translate_seconds"]
                c["lead"] = None if row["full_at"] is None else row["full_at"] - c["ready_at"]
        if row["cuts"] and translator:
            row["rest_translation"] = translator.translate(row["rest"]) if row["rest"] else ""
            joined = "".join(c["translation"] for c in row["cuts"]) + row["rest_translation"]
            row["similarity"] = round(overlap(joined, row["full_translation"]), 2)
            neg_split = any(ch in NEGATION for ch in joined)
            neg_full = any(ch in NEGATION for ch in row["full_translation"])
            row["negation_mismatch"] = neg_split != neg_full

    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "sim.json").write_text(json.dumps(all_rows, ensure_ascii=False, indent=1), encoding="utf-8")
    summarize(all_rows, args.long)
    if translator:
        write_review(all_rows, args.out / "review.md")


def summarize(rows, long_threshold):
    long_rows = [r for r in rows if r["speech"] >= long_threshold]
    print(f"定稿 {len(rows)} 句，长句（≥{long_threshold:g}s）{len(long_rows)} 句")
    for tier in ("A", "A2", "B"):
        cut_rows = [r for r in rows if any(c["tier"] == tier for c in r["cuts"])]
        cuts = [c for r in rows for c in r["cuts"] if c["tier"] == tier]
        if not cuts:
            print(f"  {tier} 档：0 个切分点")
            continue
        leads = [c["lead"] for c in cuts if c.get("lead") is not None]
        changed = sum(c["asr_changed"] for c in cuts)
        line = (f"  {tier} 档：{len(cuts)} 个切分点，分布在 {len(cut_rows)} 句"
                f"（其中长句 {sum(r['speech'] >= long_threshold for r in cut_rows)} 句）；"
                f"切分后 ASR 又改了内容 {changed} 个")
        if leads:
            line += f"；比整句译文提前 中位 {statistics.median(leads):.2f}s 最大 {max(leads):.2f}s"
        print(line)
    uncut = [r for r in long_rows if not r["cuts"]]
    print(f"  没有可切点的长句 {len(uncut)} 句")
    extra = sum(len(r["cuts"]) + (1 if r.get("rest") else 0) - 1 for r in rows if r["cuts"])
    print(f"  额外翻译调用 {extra} 次（原本 {len(rows)} 次）")
    flagged = [r for r in rows if r.get("similarity") is not None
               and (r["similarity"] < 0.4 or r.get("negation_mismatch"))]
    if any(r.get("similarity") is not None for r in rows):
        print(f"  自动粗筛可疑 {len(flagged)} 句（字面重合 < 0.4 或否定词不一致）")


def write_review(rows, path):
    lines = [
        "# 分段翻译对照（只看中文即可）",
        "",
        "每句给出：整句说完后的译文（现在的效果），和分段提前出的译文。",
        "请只判断一件事：**分段译文会不会让人误解**（意思相反、主语搞错、缺了否定等）。",
        "在每句末尾的「判断」后面填：`ok` 没问题 / `差` 意思偏但不误导 / `错` 会误导。",
        "",
        "带 ⚠ 的是脚本自动粗筛出的可疑句（字面重合低或否定词不一致），建议优先看。",
        "",
    ]
    for r in rows:
        if not r["cuts"]:
            continue
        flag = " ⚠" if r["similarity"] < 0.4 or r["negation_mismatch"] else ""
        lines.append(f"## {r['log']} #{r['seg']}（{r['speech']:.1f}s）{flag}")
        lines.append("")
        lines.append(f"- 整句译文：{r['full_translation']}")
        for i, c in enumerate(r["cuts"], 1):
            lead = "" if c.get("lead") is None else f"，提前 {c['lead']:.1f}s"
            asr = "，⚠ 之后识别改了这段" if c["asr_changed"] else ""
            lines.append(f"- 片段 {i}（{c['tier']} 档{lead}{asr}）：{c['translation']}")
        if r["rest"]:
            lines.append(f"- 剩余部分：{r['rest_translation']}")
        lines.append(f"- 原文（参考）：{r['text']}")
        lines.append("- 判断：")
        lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")
    print(f"对照表：{path}")


if __name__ == "__main__":
    main()
