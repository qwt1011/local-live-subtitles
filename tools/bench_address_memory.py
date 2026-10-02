"""Compare single-pass terminology prompts; synthetic cases are labeled explicitly."""

import argparse
import json
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.translate.address_memory import AddressMemory
from app.translate.hymt_gguf import HyMtTranslator, PROMPT

GLOSSARY = "\n\n称呼一致性参考（仅在原文确实出现该词时遵守）：\n{items}"


GROUPS = [
    ("ja", [
        ("かわいいお兄さん。", "runs/eval/parakeet_ja_clean2/ja_asmr_0230.jsonl"),
        ("お兄さん、こっちに来て。", "synthetic"),
        ("ねえ、お兄さん。", "synthetic"),
        ("お兄さん、もう帰っちゃうの？", "synthetic"),
    ]),
    ("en", [
        ("Whatever, Dork. Listen.", "runs/eval/en_pk_unified/en_asmr_0100.jsonl"),
        ("You better pick up Dork", "runs/eval/en_pk_unified/en_asmr_2600.jsonl"),
        ("Come here, dork.", "synthetic"),
        ("Don't be shy, dork.", "synthetic"),
    ]),
    ("en", [("Hey, dummy.", "synthetic"), ("Come on, dummy.", "synthetic"),
            ("Listen, dummy. I need your help.", "synthetic")]),
]


def percentile(values, q):
    return sorted(values)[min(len(values) - 1, int(len(values) * q))]


def summarize(rows):
    for row in rows:
        row["prompt_leak"] = "称呼" in row["output"] or "→" in row["output"]
        row["term_present"] = not row["prompt_leak"] and all(v in row["output"] for v in row["glossary"].values())
    summary = {}
    for name in ("compact", "reference"):
        selected = [r for r in rows if r["mode"] == name]
        deltas = [r["seconds"] - next(b["seconds"] for b in rows
                  if b["mode"] == "baseline" and b["round"] == r["round"] and b["text"] == r["text"])
                  for r in selected]
        summary[name] = dict(paired_overhead_p50=statistics.median(deltas),
                             paired_overhead_p95=percentile(deltas, .95),
                             term_present=sum(r["term_present"] for r in selected), total=len(selected),
                             prompt_leaks=sum(r["prompt_leak"] for r in selected))
    summary["baseline"] = dict(term_present=sum(r["term_present"] for r in rows if r["mode"] == "baseline"),
                                total=len(rows) // 3)
    return summary


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--repeat", type=int, default=3)
    parser.add_argument("--out", type=Path, default=ROOT / "runs/address_memory/prompt_ab.json")
    parser.add_argument("--reanalyze", action="store_true", help="Recompute leakage-aware metrics without inference")
    args = parser.parse_args()
    if args.reanalyze:
        report = json.loads(args.out.read_text(encoding="utf-8"))
        report["summary"] = summarize(report["rows"])
        args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(report["summary"], ensure_ascii=False, indent=2))
        return
    translator = HyMtTranslator(threads=4)
    translator.warm_up()
    cases, seeds = [], []
    for language, group in GROUPS:
        memory = AddressMemory()
        text, source = group[0]
        output = translator.translate(text, source=language)
        memory.observe(text, output, language)
        seeds.append(dict(language=language, text=text, translation=output, source=source,
                          learned=memory.constraints(text, language)))
        for text, source in group[1:]:
            glossary = memory.constraints(text, language)
            if glossary:
                cases.append(dict(language=language, text=text, source=source, glossary=glossary))
    if not cases:
        raise SystemExit("No unambiguous seeds learned")
    rows = []
    templates = {"baseline": None, "compact": "\n称呼：{items}", "reference": GLOSSARY}
    for round_id in range(args.repeat):
        for case_id, case in enumerate(cases):
            names = list(templates)
            offset = (round_id + case_id) % len(names)
            for name in names[offset:] + names[:offset]:
                # Measure full prompt cost; do not reward repeated identical inputs with cache reuse.
                translator.llm.reset()
                if name == "baseline":
                    output = translator.translate(case["text"], source=case["language"])
                else:
                    began = time.perf_counter()
                    prompt = PROMPT.format(target="中文", text=case["text"])
                    prompt += templates[name].format(items="；".join(f"{k} → {v}" for k, v in case["glossary"].items()))
                    response = translator.llm.create_chat_completion(
                        messages=[{"role": "user", "content": prompt}],
                        max_tokens=translator.max_new_tokens, temperature=0.0, repeat_penalty=1.05)
                    output = response["choices"][0]["message"]["content"].strip()
                    if output == case["text"]:
                        output = ""
                    translator.last_seconds = time.perf_counter() - began
                row = dict(case, round=round_id, mode=name, output=output,
                           seconds=translator.last_seconds,
                           term_present=all(v in output for v in case["glossary"].values()))
                rows.append(row)
            print(f"round={round_id + 1} case={case_id + 1}/{len(cases)}", flush=True)
    summary = summarize(rows)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(dict(method="rotating order, cold prompt cache, 4 translation threads",
                                       seeds=seeds, summary=summary, rows=rows), ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)


if __name__ == "__main__":
    main()
