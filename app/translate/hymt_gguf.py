"""腾讯 Hy-MT2-1.8B 翻译模型（GGUF，llama.cpp 纯 CPU 推理）。

## 为什么试它

它是专用翻译模型（2026-05 开源，Apache-2.0，含日中互译），不是通用指令模型，
官方报告称 1.8B 整体超过主流商用翻译 API。之前"1.5B 在这台机器上跑不动"的结论
是在"带上下文提示词、约 2.1 秒/句"的前提下估算的；现在默认是短提示词，需要重新实测。

## 提示词

逐字使用官方模型卡的"默认翻译"中文模板（目标语言用中文全称），不自己加约束——
0.5B 那边已经证明给小模型加规则往往更差（docs/BENCHMARK_RESULTS.md 12.8）。

需要：pip install llama-cpp-python==0.3.35 --extra-index-url https://abetlen.github.io/llama-cpp-python/whl/cpu
模型：models/Hy-MT2-1.8B-GGUF/Hy-MT2-1.8B-Q4_K_M.gguf
"""

import time

from ..models_catalog import MODELS_DIR

DEFAULT_MODEL = "Hy-MT2-1.8B-GGUF/Hy-MT2-1.8B-Q4_K_M.gguf"

TARGET_NAMES = {"zh": "中文", "en": "英语", "ja": "日语"}

PROMPT = "将以下文本翻译为{target}，注意只需要输出翻译后的结果，不要额外解释：\n\n{text}"


class HyMtTranslator:
    name = "hy-mt2"

    def __init__(self, model=None, threads=None, max_new_tokens=128):
        try:
            from llama_cpp import Llama
        except ImportError:
            raise SystemExit("未安装 llama-cpp-python，安装命令见 app/translate/hymt_gguf.py 模块说明")

        path = MODELS_DIR / (model or DEFAULT_MODEL)
        if not path.is_file():
            raise SystemExit(f"缺少模型文件：{path}\n"
                             f"下载：https://huggingface.co/tencent/Hy-MT2-1.8B-GGUF")
        self.model_path = path
        self.max_new_tokens = max_new_tokens
        # n_ctx 只需容纳一句台词；小上下文让 KV cache 更小、更快。
        self.llm = Llama(model_path=str(path), n_ctx=512, n_threads=threads or 4,
                         n_batch=256, verbose=False)
        self.calls = 0
        self.total_seconds = 0.0
        self.last_seconds = 0.0

    def warm_up(self):
        self.translate("こんにちは。", source="ja", target="zh")

    def translate(self, text, source="ja", target="zh", context=None, style="plain"):
        # context / style 与 InstructTranslator 接口保持一致；这里暂不使用上下文。
        text = (text or "").strip()
        if not text:
            return ""
        started = time.perf_counter()
        result = self.llm.create_chat_completion(
            messages=[{"role": "user",
                       "content": PROMPT.format(target=TARGET_NAMES.get(target, target), text=text)}],
            max_tokens=self.max_new_tokens,
            temperature=0.0,           # 字幕要稳定，不要每次不一样的译文
            repeat_penalty=1.05,
        )
        output = result["choices"][0]["message"]["content"].strip()
        if output == text:
            output = ""                # 抄回原文说明没在翻译，宁可不显示
        elapsed = time.perf_counter() - started
        self.calls += 1
        self.total_seconds += elapsed
        self.last_seconds = elapsed
        return output

    def stats(self):
        return {
            "engine": self.name,
            "backend": "llama.cpp",
            "model": self.model_path.name,
            "calls": self.calls,
            "total_seconds": round(self.total_seconds, 3),
            "mean_seconds": round(self.total_seconds / self.calls, 4) if self.calls else 0.0,
            "last_seconds": round(self.last_seconds, 4),
        }
