"""对比"孤立翻译"与"带上文翻译"，用来判断上下文到底有没有用。

用法：
    python tools/diag_translate_context.py                    # 用内置的样本台词
    python tools/diag_translate_context.py --from-reference runs/reference_small.txt

字幕翻译里最伤质量的就是孤立翻译：日语省略主语，「にしても」「お兄さん」
「〜なんて」这类表达的含义完全取决于前一句。这个脚本把两种结果并排打出来，
让人（而不是指标）来判断——中文译文的质量目前没有可靠的自动指标。
"""

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# 取自 sample_0230_0300.wav 的真实台词（顺序即对话顺序）
SAMPLE = [
    "それじゃあ短いデータと行きましょうか。",
    "可愛いお兄さん。",
    "にしても本当にびっくりしたわ。",
    "あなたみたいな人が一人で歩いてるなんて。",
    "この辺も最近は治安が悪くなってきているから。",
    "気をつけた方がいいわよ。",
]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default=None)
    parser.add_argument("--threads", type=int, default=6)
    parser.add_argument("--context-size", type=int, default=2,
                        help="带上文时看前几句（原文+译文）")
    parser.add_argument("--from-reference", type=Path, default=None,
                        help="从参考文本读句子（干净文本，**不代表真实输入**）")
    parser.add_argument("--from-session", type=Path, default=None,
                        help="从真实会话 JSONL 读原文定稿——这才是真正喂给翻译的输入")
    parser.add_argument("--style", default="instruction", choices=("plain", "instruction", "completion"),
                        help="带上文时用哪种写法")
    args = parser.parse_args()

    sentences = SAMPLE
    if args.from_session:
        import json
        rows = [json.loads(line) for line in args.from_session.read_text(encoding="utf-8").splitlines()
                if line.strip()]
        sentences = [
            row["text"] for row in rows
            if row.get("is_final") and row.get("text")
            and not (row.get("detail") or {}).get("translation")
        ]
        print(f"从会话读取到 {len(sentences)} 句 ASR 原文定稿（真实输入，含误识别）")
    elif args.from_reference:
        raw = args.from_reference.read_text(encoding="utf-8")
        sentences = [part.strip() for part in raw.replace("\n", " ").split("。") if part.strip()]
        sentences = [f"{part}。" for part in sentences]
    if not sentences:
        raise SystemExit("没有可翻译的句子")

    from app.translate.factory import create_translator

    print("加载翻译模型...")
    began = time.perf_counter()
    translator = create_translator("instruct", args.model, threads=args.threads)
    print(f"就绪（{time.perf_counter() - began:.1f}s）\n")

    history = []          # 带上文那一路的"已译上文"
    print(f"{'原文（ASR 实际输出）':<30} | {'A plain 无上文':<26} | "
          f"{'B instruction 无上文':<26} | {'C instruction 带上文'}")
    print("-" * 130)

    totals = {"A": 0.0, "B": 0.0, "C": 0.0}
    for sentence in sentences:
        started = time.perf_counter()
        a = translator.translate(sentence, source="ja", target="zh", style="plain")
        totals["A"] += time.perf_counter() - started

        started = time.perf_counter()
        b = translator.translate(sentence, source="ja", target="zh", style="instruction")
        totals["B"] += time.perf_counter() - started

        context = history[-args.context_size:] if args.context_size > 0 else None
        started = time.perf_counter()
        c = translator.translate(sentence, source="ja", target="zh",
                                 context=context, style=args.style)
        totals["C"] += time.perf_counter() - started

        print(f"{sentence:<30} | {a:<26} | {b:<26} | {c}")

        # 只有 C 那一路有上文，用它的译文维护历史
        history.append({"original": sentence, "translation": c})

    print()
    print(f"耗时  A(plain/无上文) {totals['A']:.2f}s   "
          f"B(instruction/无上文) {totals['B']:.2f}s   "
          f"C(instruction/带上文) {totals['C']:.2f}s")
    print()
    print("A vs B 隔离出**提示词改动**的影响；B vs C 隔离出**上下文**的影响。")
    print("质量由人来判断——尤其注意 C 是否出现'抄上一句'或'输出日语'。")


if __name__ == "__main__":
    main()
