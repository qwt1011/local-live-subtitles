"""对比两次评测回放的"文字多久能定稿"：按音频时间对齐，而不是按句子。

    python tools/compare_early.py runs/eval/sv2024_base_0929 runs/eval/sv2024_early

提前定稿会改变句子的个数（一句拆成两句），逐句比没有意义。
这里对每个时刻 t 的语音问：包含 t 的那句话在第几秒定稿？
把定稿时间减去 t 就是"这一刻说的话要等多久才定稿"，在所有语音时刻上取分布。
这个时刻差再加上约 0.9s 翻译，就是用户看到中文的等待时间。
"""

import argparse
import json
import statistics
from pathlib import Path


def finals(path):
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    return [r for r in rows if r.get("is_final") and r.get("text")]


def waits(rows, step=0.1):
    out = []
    for r in rows:
        t = r["audio_start"]
        while t < r["audio_end"]:
            out.append(r["finish_wall"] - t)
            t += step
    return sorted(out)


def pct(values, q):
    return values[min(len(values) - 1, int(len(values) * q))]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("dirs", type=Path, nargs="+")
    args = parser.parse_args()
    print(f"{'':>24} {'定稿句':>6} {'中位':>6} {'p90':>6} {'最大':>6}  CER")
    for d in args.dirs:
        summary = json.loads((d / "summary.json").read_text(encoding="utf-8"))
        all_rows, all_waits = [], []
        for clip in sorted(d.glob("*.jsonl")):
            rows = finals(clip)
            all_rows += rows
            all_waits += waits(rows)
        all_waits.sort()
        early = sum(1 for r in all_rows if r.get("detail", {}).get("early_final"))
        adaptive = sum(1 for r in all_rows if r.get("detail", {}).get("adaptive_final"))
        print(f"{d.name:>24} {len(all_rows):>6} {statistics.median(all_waits):6.2f} "
              f"{pct(all_waits, 0.9):6.2f} {all_waits[-1]:6.2f}  {summary['total']['cer']:.4f}"
              f"  （提前定稿 {early} 句，短静音定稿 {adaptive} 句）")


if __name__ == "__main__":
    main()
