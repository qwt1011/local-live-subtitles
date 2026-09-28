"""用本地小参数量指令模型做翻译（Qwen2.5-0.5B-Instruct）。

## 为什么换成这条路线

NLLB 那条线被实测证明走不通：`target_prefix`（NLLB 用来指定目标语言的机制）
在 CTranslate2 4.8.1 下**被完全忽略**——换任何目标语言都输出同一段英文回声；
两份预转换仓库和本地转换都一样，而同一个 CT2 转出来的 Marian 模型工作正常。
细节见 `BENCHMARK_RESULTS.md` 第 12 节。

指令模型的好处是**用自然语言 prompt 表达翻译意图**，压根不经过语言标记机制，
所以绕开了刚被证实坏掉的那一环。

## 两种后端

| 后端 | 权重 | 单句中位耗时 | 说明 |
|---|---|---|---|
| `torch` | 942MB fp32 | 2.16s（p95 4.14s） | 直接用 transformers，最省事 |
| `ct2`（默认） | 473MB int8 | 见 `tools/diag_translate_cost.py` | 用 CTranslate2，速度快数倍、内存小 4 倍 |

两者共用同一份 tokenizer 与同一套 prompt，只是生成器不同，所以切换后端不影响输出风格。
CT2 那份需要用 `ct2-transformers-converter --quantization int8` 单独转一次。

## 它不占原文延迟

翻译只对**定稿**触发，跑在服务端的独立线程里，识别链路完全不等它
（`app/server.py` 的 `_translate_loop`）。译文作为同一个 `segment_id` 的更高
`revision` 回传，渲染端原地补上。
"""

import time

from ..models_catalog import MODELS_DIR

DEFAULT_MODEL = "Qwen2.5-0.5B-Instruct"

LANGUAGE_NAMES = {"ja": "日语", "en": "英语", "zh": "中文"}

SYSTEM_PROMPT = "你是一个字幕翻译引擎。"

# 提示词保持简短，但**必须带上文**。
#
# 孤立翻译是字幕翻译最大的质量杀手：日语大量省略主语，
# 「にしても」「お兄さん」「〜なんて」这类表达的含义完全取决于前一句。
# 格式用「上文 …… 本次 …… →」，并且上文里同时给**原文和译文**：
# 只给原文的话模型会想自己再翻一遍上文，容易把上文也输出出来；
# 给出译文等于同时锚定了用词（お兄さん 是否一直译成"哥哥"）。
CONTEXT_HEADER = "上文："
TURN_HEADER = "本次："
ARROW = "→"


def _context_lines(context, arrow=ARROW):
    lines = []
    for item in context or []:
        original = (item.get("original") or "").strip()
        translation = (item.get("translation") or "").strip()
        if not original:
            continue
        lines.append(f"{original} {arrow} {translation}" if translation else original)
    return lines


def build_prompt_parts(text, source_name, target_name, context=None, style="instruction"):
    """返回 (system_prompt, user_message)。

    两种带上文的写法，实测差别很大（见 BENCHMARK_RESULTS.md 12.9）：

    - ``completion``：把上文写成「原文 → 译文」的范例，最后留一句「本次：xxx →」
      让模型接着写。**0.5B 会把这个当成接龙**：要么直接抄上一句的译文，
      要么顺着范例继续输出日语。已实测到两种崩法，不能用于小模型。
    - ``instruction``（默认）：上文放进 system prompt 当作背景说明，
      user 消息里只有待翻译的句子。0.5B 不再有"续写范例"的错觉。
    """
    lines = _context_lines(context)

    if style == "completion":
        parts = [f"把{TURN_HEADER}最后一句{source_name}翻译成{target_name}，只输出译文。", ""]
        if lines:
            parts.append(CONTEXT_HEADER)
            parts.extend(lines)
            parts.append("")
        parts.append(TURN_HEADER)
        parts.append(f"{text} {ARROW}")
        return SYSTEM_PROMPT, "\n".join(parts)

    parts = [SYSTEM_PROMPT]
    parts.append("")
    if lines:
        parts.append(f"这是字幕翻译。下面是对白的上文（{source_name}原文 {ARROW} {target_name}译文），"
                     f"仅供理解语境和统一用词，**不要翻译它们**：")
        parts.extend(lines)
        parts.append("")
        parts.append(f"现在把用户发来的那一句{source_name}翻译成{target_name}，"
                     f"只输出译文，不要解释，不要输出日语。")
    else:
        parts.append(f"把用户发来的{source_name}句子翻译成{target_name}，只输出译文，不要解释。")
    return "\n".join(parts), text

# Qwen 的会话结束标记
END_TOKENS = ["<|im_end|>", "<|endoftext|>"]


class InstructTranslator:
    name = "qwen-instruct"

    def __init__(self, model_dir=None, backend="ct2", threads=None,
                 max_new_tokens=128, ct2_dir=None):
        from transformers import AutoTokenizer

        directory = MODELS_DIR / (model_dir or DEFAULT_MODEL)
        if not (directory / "model.safetensors").is_file():
            raise SystemExit(
                f"缺少指令模型：{directory}\n"
                f"先运行：python tools/setup_models.py --engine qwen2.5-0.5b-instruct"
            )

        self.model_dir = directory
        self.max_new_tokens = max_new_tokens
        self.tokenizer = AutoTokenizer.from_pretrained(str(directory))
        self.backend = backend

        if backend == "ct2":
            import ctranslate2

            path = MODELS_DIR / (ct2_dir or f"{directory.name}-ct2-int8")
            if not (path / "model.bin").is_file():
                raise SystemExit(
                    f"缺少 CT2 版指令模型：{path}\n"
                    f"转换命令：ct2-transformers-converter --model {directory} "
                    f"--output_dir {path} --quantization int8 --force"
                )
            self.ct2_dir = path
            self.generator = ctranslate2.Generator(
                str(path), device="cpu", compute_type="int8",
                inter_threads=1, intra_threads=threads or 4,
            )
        elif backend == "torch":
            import torch
            from transformers import AutoModelForCausalLM

            if threads:
                torch.set_num_threads(threads)
            self.torch = torch
            # 显式用 float32：CPU 上 bfloat16 支持参差，而且这个规模不缺内存。
            self.model = AutoModelForCausalLM.from_pretrained(str(directory), dtype=torch.float32)
            self.model.eval()
        else:
            raise SystemExit(f"unknown backend: {backend}（可选 ct2 / torch）")

        self.calls = 0
        self.total_seconds = 0.0
        self.last_seconds = 0.0

    def warm_up(self):
        # 与 Argos 那次 15 秒懒加载同类的坑，先付掉。
        self.translate("こんにちは。", source="ja", target="zh")

    def _build_prompt(self, text, source, target, context=None, style="instruction"):
        source_name = LANGUAGE_NAMES.get(source, source)
        target_name = LANGUAGE_NAMES.get(target, target)
        system, user = build_prompt_parts(text, source_name, target_name, context, style)
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]
        return self.tokenizer.apply_chat_template(
            messages, tokenize=False, add_generation_prompt=True)

    @staticmethod
    def _clean(output, source_text):
        """小模型常见的几种跑偏：加引号、加"译文："前缀、直接把原文抄回来。"""
        text = output.strip()
        for prefix in ("译文：", "译文:", "翻译：", "翻译:", "中文：", "中文:"):
            if text.startswith(prefix):
                text = text[len(prefix):].strip()
        pairs = (("「", "」"), ("『", "』"), ("“", "”"), ('"', '"'), ("'", "'"))
        for left, right in pairs:
            if len(text) > 1 and text.startswith(left) and text.endswith(right):
                text = text[1:-1].strip()
        # 抄回原文说明模型没在翻译，宁可不显示也不要给用户看原文。
        if text and text == source_text.strip():
            return ""
        return text

    def _generate_ct2(self, prompt):
        start_tokens = self.tokenizer.convert_ids_to_tokens(self.tokenizer.encode(prompt))
        results = self.generator.generate_batch(
            [start_tokens],
            max_length=len(start_tokens) + self.max_new_tokens,
            end_token=END_TOKENS,
            repetition_penalty=1.1,
            include_prompt_in_result=False,
        )
        return self.tokenizer.decode(results[0].sequences_ids[0], skip_special_tokens=True)

    def _generate_torch(self, prompt):
        inputs = self.tokenizer(prompt, return_tensors="pt")
        with self.torch.inference_mode():
            generated = self.model.generate(
                **inputs,
                max_new_tokens=self.max_new_tokens,
                do_sample=False,          # 字幕要稳定，不要每次不一样的译文
                repetition_penalty=1.1,
                pad_token_id=self.tokenizer.pad_token_id or self.tokenizer.eos_token_id,
            )
        new_tokens = generated[0][inputs["input_ids"].shape[1]:]
        return self.tokenizer.decode(new_tokens, skip_special_tokens=True)

    def translate(self, text, source="ja", target="zh", context=None, style="instruction"):
        text = (text or "").strip()
        if not text:
            return ""

        started = time.perf_counter()
        prompt = self._build_prompt(text, source, target, context, style)
        raw = self._generate_ct2(prompt) if self.backend == "ct2" else self._generate_torch(prompt)
        output = self._clean(raw, text)

        elapsed = time.perf_counter() - started
        self.calls += 1
        self.total_seconds += elapsed
        self.last_seconds = elapsed
        return output

    def stats(self):
        return {
            "engine": self.name,
            "backend": self.backend,
            "model": self.model_dir.name,
            "calls": self.calls,
            "total_seconds": round(self.total_seconds, 3),
            "mean_seconds": round(self.total_seconds / self.calls, 4) if self.calls else 0.0,
            "last_seconds": round(self.last_seconds, 4),
        }
