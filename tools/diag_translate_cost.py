"""标定翻译层的延迟预算。

背景：字幕延迟目标 1.5 秒，而识别已经吃掉大部分预算，翻译能花多少必须实测，
不能凭感觉。Argos 内部有翻译缓存，用重复句子测量会得到 0 ms 的假数据，
所以这里用互不相同的句子。

用法：
    python tools/diag_translate_cost.py            # 测量已安装的 Argos 语言对
    python tools/diag_translate_cost.py --repeat 3
"""

import argparse
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

SENTENCES = [
    "Hello there.",
    "Let's take a short walk together.",
    "What a cute young man you are.",
    "To be honest, you really startled me just now.",
    "Recently the public order around this area has been getting worse.",
    "You should be careful, walking around alone at night like this.",
    "I never expected to run into someone like you in a place like this.",
    "If you keep standing there without saying anything, I might get the wrong idea.",
    "There is a convenience store just around the next corner if you need anything.",
    "It is already this late, so please make sure you get home safely and lock your door.",
]


def installed_pairs():
    import argostranslate.translate as translate
    pairs = []
    for language in translate.get_installed_languages():
        for translation in language.translations_to:
            pairs.append((translation.from_lang.code, translation.to_lang.code))
    return sorted(pairs)


def measure(from_code, to_code, repeat):
    import argostranslate.translate as translate

    samples = []
    outputs = []
    for _ in range(repeat):
        for index, sentence in enumerate(SENTENCES):
            # 加一个变化的数字后缀，绕开 Argos 的翻译缓存，保证测量的是真实计算。
            text = f"{sentence} ({index}-{_})"
            began = time.perf_counter()
            outputs.append(translate.translate(text, from_code, to_code))
            samples.append(time.perf_counter() - began)
    return samples, outputs


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--repeat", type=int, default=3)
    args = parser.parse_args()

    pairs = installed_pairs()
    print(f"installed Argos pairs: {pairs}")
    if not pairs:
        print("没有已安装的 Argos 语言对，跳过。")
        return

    for from_code, to_code in pairs:
        samples, outputs = measure(from_code, to_code, args.repeat)
        samples_ms = [value * 1000 for value in samples]
        print()
        print(f"---- {from_code} -> {to_code} ({len(samples)} 次调用) ----")
        print(f"median={statistics.median(samples_ms):7.1f}ms  "
              f"p95={sorted(samples_ms)[int(len(samples_ms) * 0.95)]:7.1f}ms  "
              f"max={max(samples_ms):7.1f}ms")
        print(f"示例: {SENTENCES[-1][:40]}... -> {outputs[-1][:40]}")
        print("提示：这是 Marian 量级模型（Argos ~75MB）的成本。"
              "NLLB-200-distilled-600M 约大 8 倍，必须单独实测后才能定实时预算。")


if __name__ == "__main__":
    main()
