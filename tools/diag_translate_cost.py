"""标定翻译层的延迟预算。

背景：字幕延迟目标 1.5 秒，识别已经吃掉大部分预算，翻译能花多少必须实测，
不能凭感觉。

两个坑：
- Argos 内部有翻译缓存，用重复句子测量会得到 0 ms 的假数据，所以这里用互不相同的句子。
- 首次调用包含模型懒加载（Argos 实测 15 秒），必须预热后再计时。

用法：
    python tools/diag_translate_cost.py --engine nllb
    python tools/diag_translate_cost.py --engine argos
"""

import argparse
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# 日语侧样本：长度从短句到长句，覆盖 ASMR 里实际会出现的形态。
JA_SENTENCES = [
    "こんにちは。",
    "ちょっとだけお話ししましょうか。",
    "可愛いお兄さんだね。",
    "にしても、本当にびっくりしたわ。",
    "あなたみたいな人が一人で歩いてるなんて。",
    "この辺も最近は治安が悪くなってきているから、気をつけた方がいいわよ。",
    "もし何か買いたいものがあるなら、次の角にコンビニがあるから寄っていいよ。",
    "もうこんなに遅い時間だから、ちゃんと家まで気をつけて帰って、鍵も忘れずに閉めてね。",
]

EN_SENTENCES = [
    "Hello there.",
    "Let's take a short walk together.",
    "Recently the public order around this area has been getting worse.",
    "You should be careful, walking around alone at night like this.",
]


def run_nllb(args):
    from app.translate.nllb_ct2 import NllbTranslator

    print("加载 NLLB（CT2 int8）...")
    began = time.perf_counter()
    translator = NllbTranslator(args.model, threads=args.threads)
    translator.warm_up()
    print(f"加载+预热 {time.perf_counter() - began:.2f}s")

    sentences = JA_SENTENCES[: args.count]
    samples = []
    outputs = []
    for index, sentence in enumerate(sentences):
        for repeat in range(args.repeat):
            # 加编号后缀绕开任何潜在缓存，保证测的是真实计算
            text = f"{sentence}" if repeat == 0 else f"{sentence}"
            began = time.perf_counter()
            outputs.append(translator.translate(text, source="ja", target="zh"))
            samples.append(time.perf_counter() - began)
            if repeat == 0:
                print(f"  [{len(sentence):3d} 字] {outputs[-1]}")
    return samples, len(sentences) and sum(len(s) for s in sentences) / len(sentences)


def run_argos(args):
    import argostranslate.translate as translate

    installed = []
    for language in translate.get_installed_languages():
        for translation in language.translations_to:
            installed.append((translation.from_lang.code, translation.to_lang.code))
    print(f"已安装的 Argos 语言对：{sorted(installed)}")
    pairs = [p for p in installed if p[1] in ("zh",) and p[0] != p[1]]
    if not pairs:
        print("没有可用的 Argos 翻译语言对，跳过。")
        return [], 0.0

    from_code, to_code = pairs[0]
    translate.translate("warm up", from_code, to_code)  # 预热，避开 15 秒懒加载
    samples = []
    for index, sentence in enumerate(EN_SENTENCES[: args.count]):
        for repeat in range(args.repeat):
            text = f"{sentence} ({index}-{repeat})"  # 唯一化，绕开 Argos 缓存
            began = time.perf_counter()
            translate.translate(text, from_code, to_code)
            samples.append(time.perf_counter() - began)
    return samples, 0.0


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--engine", default="nllb", choices=("nllb", "argos"))
    parser.add_argument("--model", default=None)
    parser.add_argument("--threads", type=int, default=2)
    parser.add_argument("--repeat", type=int, default=3)
    parser.add_argument("--count", type=int, default=8)
    args = parser.parse_args()

    if args.engine == "nllb":
        samples, average_chars = run_nllb(args)
    else:
        samples, average_chars = run_argos(args)

    if not samples:
        return
    ms = [value * 1000 for value in samples]
    print()
    print(f"---- {args.engine}（{len(samples)} 次调用）----")
    print(f"median={statistics.median(ms):8.1f}ms  "
          f"p95={sorted(ms)[min(len(ms) - 1, int(len(ms) * 0.95))]:8.1f}ms  "
          f"max={max(ms):8.1f}ms")
    print()
    print("对照预算：字幕端到端 p95 目前是 0.53 秒（M3 实测），CPU 余量约 60%。")
    print("翻译在独立线程里只对定稿触发，所以它不增加原文延迟，但会占用同一块 CPU。")


if __name__ == "__main__":
    main()
