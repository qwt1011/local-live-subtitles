"""独立对比翻译引擎的速度与译文：句子取自会话日志里的定稿原文。

    python tools/bench_translate.py runs/live_diag_0930.jsonl runs/live_diag_0230.jsonl --engines instruct hymt

不经过服务，只测单句翻译本身，避免被识别线程或浏览器抢占干扰。
"""

import argparse
import json
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.translate.factory import create_translator  # noqa: E402


def sentences(paths):
    seen = []
    for path in paths:
        for line in path.read_text(encoding="utf-8").splitlines():
            e = json.loads(line) if line.strip() else {}
            if e.get("is_final") and e.get("text") and not e.get("translation") and e["text"] not in seen:
                seen.append(e["text"])
    return seen


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("logs", type=Path, nargs="+")
    parser.add_argument("--engines", nargs="+", default=["instruct", "hymt"])
    parser.add_argument("--threads", type=int, default=4)
    parser.add_argument("--out", type=Path, default=Path("runs/bench_translate.json"))
    args = parser.parse_args()

    texts = sentences(args.logs)
    print(f"{len(texts)} 句", flush=True)
    results = {}
    for name in args.engines:
        translator = create_translator(name, threads=args.threads)
        outputs, times = [], []
        for text in texts:
            outputs.append(translator.translate(text))
            times.append(translator.last_seconds)
        results[name] = {"outputs": outputs, "seconds": times}
        print(f"{name}: 中位 {statistics.median(times):.2f}s 最大 {max(times):.2f}s "
              f"合计 {sum(times):.1f}s", flush=True)
        del translator

    for i, text in enumerate(texts):
        print(f"\n[{i}] {text}")
        for name in args.engines:
            r = results[name]
            print(f"  {name:>8} {r['seconds'][i]:.2f}s  {r['outputs'][i]}")

    args.out.write_text(json.dumps({"texts": texts, **results}, ensure_ascii=False, indent=1),
                        encoding="utf-8")


if __name__ == "__main__":
    main()
