"""SenseVoice（sherpa-onnx）识别引擎。

为什么值得测：Whisper 的 encoder 永远对**补齐到 30 秒的 mel** 做一次前向，
所以它的单次调用成本 ≈ 0.5–1.1 秒且**与音频长度无关**（见 BENCHMARK_RESULTS.md 第 4 节），
这个结构性固定成本直接封死了"1.5 秒 + 可用质量"的可能性。

SenseVoice-small 是非自回归（CTC）多语言模型，支持 zh/en/ja/ko/yue，
没有 30 秒补齐的问题，成本应随音频长度线性增长。本文件只做适配，
"它到底快多少、日语准不准"由 tools/replay.py 用同一套指标回答。
"""

import os

import numpy as np

try:
    import sherpa_onnx
except ImportError:  # 引擎可选，未安装时不影响 Whisper 路径
    sherpa_onnx = None

from .faster_whisper_engine import AsrResult
from ..models_catalog import resolve as resolve_model_dir

SAMPLE_RATE = 16000

# SenseVoice 的语言标记与项目里的 ja/en 一致，另外多一个 auto。
LANGUAGE_TAGS = {"ja", "en", "zh", "ko", "yue", "auto", ""}


class SenseVoiceEngine:
    name = "sensevoice"

    def __init__(self, model_dir, num_threads=None, use_itn=True, language="ja"):
        if sherpa_onnx is None:
            raise SystemExit("未安装 sherpa-onnx：python -m pip install sherpa-onnx")
        directory = resolve_model_dir(model_dir)
        model = directory / "model.int8.onnx"
        if not model.is_file():
            model = directory / "model.onnx"
        tokens = directory / "tokens.txt"
        for path in (model, tokens):
            if not path.is_file():
                raise SystemExit(f"缺少模型文件：{path}")

        self.model_dir = directory
        self.model_name = directory.name
        self.language = language
        self.kwargs = {
            "language": language,
            "use_itn": use_itn,
        }
        self.recognizer = sherpa_onnx.OfflineRecognizer.from_sense_voice(
            model=str(model),
            tokens=str(tokens),
            num_threads=num_threads or max(1, (os.cpu_count() or 4) // 3),
            use_itn=use_itn,
            language=language,
            debug=False,
        )

    def warm_up(self, language="ja"):
        silence = np.zeros(SAMPLE_RATE, dtype=np.float32)
        self.transcribe(silence, language=language)

    def transcribe(self, pcm, language="ja", initial_prompt=None, **overrides):
        # SenseVoice 不支持 prompt 续写上下文，这里显式忽略并说明，
        # 避免"传了 initial_prompt 却被静默丢弃"这种隐蔽的对比失真。
        if initial_prompt:
            raise ValueError("SenseVoice 不支持 initial_prompt；流式上下文需在分段层实现")
        # 语言是构造参数（decode_stream 不接受 language），换语言需要另建一个引擎实例。
        if language != self.language:
            raise ValueError(
                f"该 SenseVoice 实例构造时语言为 {self.language!r}，无法在调用时改成 {language!r}；"
                f"请为不同语言各建一个引擎实例"
            )

        stream = self.recognizer.create_stream()
        stream.accept_waveform(SAMPLE_RATE, np.asarray(pcm, dtype=np.float32))
        self.recognizer.decode_stream(stream)
        result = stream.result

        text = (getattr(result, "text", "") or "").strip()
        # sherpa-onnx 的 result 带逐 token 时间戳（CTC 峰值位置，相对本段起点）。
        # 注意末尾补的「。」时间戳会落在整段末尾，不代表真实发音位置。
        tokens = list(zip(getattr(result, "tokens", []) or [], getattr(result, "timestamps", []) or []))
        # 整段作为一条 segment 返回；分段由 Segmenter 负责。
        rows = [{
            "start": 0.0,
            "end": round(len(pcm) / float(SAMPLE_RATE), 3),
            "text": text,
        }] if text else []

        return AsrResult(
            text=text,
            segments=rows,
            language=getattr(result, "lang", self.language) or self.language,
            language_probability=1.0,
            duration_after_vad=0.0,
            tokens=tokens,
        )
