"""第 0 步上限测试：识别错误拖累了多少翻译？

    python tools/asr_ceiling.py runs/eval/sv2024_base_0929

对评测回放里的每句 ASR 定稿，在参考文本里找到对应的那一段（字符级对齐），
两边都交给同一个翻译模型，比较译文。参考原文的译文就是"识别完美时"的上限。

输出：
- 每句的原文字错率、两份译文的 chrF（字符 n-gram F 值，1 = 完全相同）；
- runs/asr_ceiling/review.md：按 chrF 从低到高列出，供人工判断哪边译得对。
"""

import argparse
import difflib
import json
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools.metrics import PUNCTUATION, character_error_rate, load_reference  # noqa: E402

REFS = ROOT / "eval" / "refs"


def content_index(text):
    """去掉标点空白后的内容字，以及每个内容字在原串里的位置。"""
    chars, where = [], []
    for i, ch in enumerate(text):
        if ch not in PUNCTUATION and not ch.isspace():
            chars.append(ch)
            where.append(i)
    return chars, where


def ref_text(clip):
    """参考文本各行用「。」连起来，保留行内的「、」，翻译时语气更自然。"""
    raw = (REFS / f"{clip}.txt").read_text(encoding="utf-8")
    lines = [load_reference(line) for line in raw.splitlines()]
    return "。".join(line for line in lines if line) + "。"


def align(finals, ref):
    """把每句 ASR 定稿映射到参考文本里的一段原文。"""
    ref_chars, ref_where = content_index(ref)
    asr_chars, owner = [], []
    for k, text in enumerate(finals):
        chars, _ = content_index(text)
        asr_chars += chars
        owner += [k] * len(chars)
    matcher = difflib.SequenceMatcher(None, asr_chars, ref_chars, autojunk=False)
    spans = [[None, None] for _ in finals]
    for a, b, size in matcher.get_matching_blocks():
        for offset in range(size):
            k = owner[a + offset]
            pos = b + offset
            lo, hi = spans[k]
            spans[k] = [pos if lo is None else min(lo, pos), pos if hi is None else max(hi, pos)]
    out = []
    for lo, hi in spans:
        if lo is None:
            out.append("")
            continue
        start = ref_where[lo]
        end = ref_where[hi] + 1
        # 带上紧跟的标点，让参考原文和 ASR 一样以句末符号结尾
        while end < len(ref) and ref[end] in PUNCTUATION:
            end += 1
        out.append(ref[start:end].lstrip("。、"))
    return out


def chrf(hyp, ref, n=4, beta=2.0):
    """字符 n-gram F 值（中文按字切，n=1..4）。"""
    hyp = [c for c in hyp if not c.isspace()]
    ref = [c for c in ref if not c.isspace()]
    if not hyp or not ref:
        return 1.0 if hyp == ref else 0.0
    scores = []
    for k in range(1, n + 1):
        h = [tuple(hyp[i:i + k]) for i in range(len(hyp) - k + 1)]
        r = [tuple(ref[i:i + k]) for i in range(len(ref) - k + 1)]
        if not h or not r:
            continue
        common = sum(min(h.count(g), r.count(g)) for g in set(h))
        p, rc = common / len(h), common / len(r)
        scores.append(0.0 if p + rc == 0 else (1 + beta ** 2) * p * rc / (beta ** 2 * p + rc))
    return statistics.fmean(scores) if scores else 0.0


def finals_of(path):
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    return [r["text"] for r in rows if r.get("is_final") and r.get("text")]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("eval_dir", type=Path)
    parser.add_argument("--engine", default="hymt")
    parser.add_argument("--out", type=Path, default=ROOT / "runs" / "asr_ceiling")
    args = parser.parse_args()

    from app.translate.factory import create_translator
    translator = create_translator(args.engine)

    pairs = []
    for clip_path in sorted(args.eval_dir.glob("*.jsonl")):
        clip = clip_path.stem
        if not (REFS / f"{clip}.txt").is_file():
            continue
        finals = finals_of(clip_path)
        refs = align(finals, ref_text(clip))
        for asr, ref in zip(finals, refs):
            if not ref:
                continue
            asr_zh = translator.translate(asr)
            ref_zh = translator.translate(ref)
            cer = character_error_rate(asr, ref)
            pairs.append({"clip": clip, "asr": asr, "ref": ref, "cer": round(cer, 3),
                          "asr_zh": asr_zh, "ref_zh": ref_zh, "chrf": round(chrf(asr_zh, ref_zh), 3)})
            print(f"{clip[-4:]} cer={cer:.2f} chrf={pairs[-1]['chrf']:.2f} | {asr}", flush=True)

    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "pairs.json").write_text(json.dumps(pairs, ensure_ascii=False, indent=1), encoding="utf-8")

    exact = [p for p in pairs if p["cer"] == 0]
    wrong = [p for p in pairs if p["cer"] > 0]
    print()
    print(f"共 {len(pairs)} 句；原文完全识别对 {len(exact)} 句，有错 {len(wrong)} 句")
    for name, group in (("原文全对", exact), ("原文有错", wrong)):
        if group:
            print(f"  {name}：译文 chrF 中位 {statistics.median(p['chrf'] for p in group):.2f}")
    bad = [p for p in wrong if p["chrf"] < 0.5]
    print(f"  原文有错且译文差异大（chrF<0.5）：{len(bad)} 句")

    lines = ["# 识别上限测试：ASR 定稿 vs 参考原文，同一个翻译模型", "",
             f"来源 `{args.eval_dir.as_posix()}`，翻译 `{args.engine}`。按译文相似度从低到高排序。", ""]
    for p in sorted(pairs, key=lambda p: p["chrf"]):
        lines += [f"### {p['clip']}  cer={p['cer']}  chrF={p['chrf']}",
                  f"- ASR：{p['asr']}", f"- 参考：{p['ref']}",
                  f"- ASR 译文：{p['asr_zh']}", f"- 参考译文：{p['ref_zh']}", ""]
    (args.out / "review.md").write_text("\n".join(lines), encoding="utf-8")
    print(f"逐句对照：{args.out / 'review.md'}")


if __name__ == "__main__":
    main()
