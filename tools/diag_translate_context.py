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
                        help="改成从参考文本读句子（按标点切分）")
    parser.add_argument("--style", default="instruction", choices=("instruction", "completion"),
                        help="带上文的写法；completion 是'原文→译文'接龙（0.5B 会崩）")
    args = parser.parse_args()

    sentences = SAMPLE
    if args.from_reference:
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
    print(f"{'原文':<32} {'孤立翻译':<28} {'带上文翻译':<28} 上文?")
    print("-" * 120)

    alone_total = 0.0
    context_total = 0.0

    for sentence in sentences:
        started = time.perf_counter()
        alone = translator.translate(sentence, source="ja", target="zh")
        alone_total += time.perf_counter() - started

        context = history[-args.context_size:] if args.context_size > 0 else None
        started = time.perf_counter()
        with_context = translator.translate(sentence, source="ja", target="zh",
                                            context=context, style=args.style)
        context_total += time.perf_counter() - started

        used = len(context) if context else 0
        print(f"{sentence:<32} {alone:<28} {with_context:<28} {used}")

        # 带上文那一路的译文进入历史，保证两路的上下文一致
        history.append({"original": sentence, "translation": with_context})

    print()
    print(f"孤立翻译总耗时 {alone_total:.2f}s，带上文总耗时 {context_total:.2f}s"
          f"（多出 {context_total - alone_total:+.2f}s）")
    print("说明：质量要人来判断，这里只把两种结果并排摆出来。")


if __name__ == "__main__":
    main()
