"""NLLB-200 + CTranslate2 翻译引擎（直连 ja -> zh，无中转）。

用法（模型要先下载）：

    python tools/setup_models.py --engine nllb-ja-zh

NLLB 的输入输出约定：
- 源语言靠 tokenizer 本身区分（同一个 spm 模型服务所有语言）；
- **目标语言必须作为 target_prefix 传给解码器**，否则模型不知道该输出哪种语言。
  这里的 `jpn_Jpan` / `zho_Hans` 就是 NLLB 的语言标记。

只用 sentencepiece 分词，不需要 transformers —— 模型仓库里带了
`sentencepiece.bpe.model`，而 NLLB 的语言标记本身就在 spm 词表里。
"""

import time

import ctranslate2
import sentencepiece as spm

from ..models_catalog import MODELS_DIR, NLLB_LANGS

DEFAULT_MODEL = "nllb-200-distilled-600M-ct2-int8"


class NllbTranslator:
    name = "nllb-ct2"

    def __init__(self, model_dir=None, compute_type="int8", threads=None,
                 beam_size=1, max_batch=1, repetition_penalty=1.2,
                 no_repeat_ngram_size=3, max_decoding_length=256):
        directory = MODELS_DIR / (model_dir or DEFAULT_MODEL)
        model_file = directory / "model.bin"
        spm_file = directory / "sentencepiece.bpe.model"
        for path in (model_file, spm_file):
            if not path.is_file():
                raise SystemExit(
                    f"缺少翻译模型文件：{path}\n"
                    f"先运行：python tools/setup_models.py --engine nllb-ja-zh"
                )
        self.model_dir = directory
        self.beam_size = beam_size
        # 防重复参数是**必需**的，不是调优。实测（BENCHMARK_RESULTS.md 12.3）：
        # 不加这些参数，模型会一直复读到 max_decoding_length 才停，
        # 单句从 0.85 秒劣化到 1.65 秒且输出完全不可用。
        # 这和 M0 在 Whisper 上发现的 temperature 回退属于同一类问题。
        self.repetition_penalty = repetition_penalty
        self.no_repeat_ngram_size = no_repeat_ngram_size
        self.max_decoding_length = max_decoding_length
        # 从字节加载，而不是 `SentencePieceProcessor(model_file=路径)`。
        #
        # sentencepiece 的 C++ 层在 Windows 上打不开含非 ASCII 字符的路径，
        # 而本项目就在 C:\text\实验\... 下面：用绝对路径会报
        # `NOT_FOUND: "...\sentencepiece.bpe.model"`，用相对路径能过只是因为
        # 相对路径恰好全是 ASCII。从字节加载可以完全绕开路径编码问题，
        # 无论项目放在哪个目录都成立。
        with open(spm_file, "rb") as handle:
            self.processor = spm.SentencePieceProcessor()
            self.processor.load_from_serialized_proto(handle.read())
        self.translator = ctranslate2.Translator(
            str(directory),
            device="cpu",
            compute_type=compute_type,
            inter_threads=1,
            intra_threads=threads or 2,
        )
        self.calls = 0
        self.total_seconds = 0.0

    def _target_token(self, target):
        token = NLLB_LANGS.get(target)
        if token is None:
            raise ValueError(f"NLLB 不知道目标语言 {target!r}；已知：{sorted(NLLB_LANGS)}")
        return token

    def warm_up(self):
        # 与 Argos 那次 15 秒懒加载同类的坑，先付掉。
        self.translate("こんにちは。", source="ja", target="zh")

    def translate(self, text, source="ja", target="zh"):
        text = (text or "").strip()
        if not text:
            return ""

        started = time.perf_counter()
        # NLLB 的源语言不需要前缀，但加一个明确的源语言标记有助于定位问题；
        # 真正决定输出语言的是 target_prefix。
        source_tokens = self.processor.encode(text, out_type=str)
        results = self.translator.translate_batch(
            [source_tokens],
            target_prefix=[[self._target_token(target)]],
            beam_size=self.beam_size,
            max_batch_size=1,
            max_decoding_length=self.max_decoding_length,
            repetition_penalty=self.repetition_penalty,
            no_repeat_ngram_size=self.no_repeat_ngram_size,
        )
        tokens = results[0].hypotheses[0]
        # 去掉回传的目标语言标记
        if tokens and tokens[0] == self._target_token(target):
            tokens = tokens[1:]
        output = self.processor.decode(tokens).strip()

        self.calls += 1
        self.total_seconds += time.perf_counter() - started
        return output

    def stats(self):
        return {
            "engine": self.name,
            "calls": self.calls,
            "total_seconds": round(self.total_seconds, 3),
            "mean_seconds": round(self.total_seconds / self.calls, 4) if self.calls else 0.0,
        }
