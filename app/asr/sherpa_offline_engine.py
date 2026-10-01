"""其他 sherpa-onnx 离线模型的通用适配：NeMo CTC（Parakeet）与 transducer（ReazonSpeech Zipformer）。

和 SenseVoice 一样是非自回归或轻量解码，没有 Whisper 的 30 秒补齐固定成本，
可以直接放进开放段重解码流水线。模型目录里有什么文件就用哪种构造方式：

- model.int8.onnx / model.onnx          → from_nemo_ctc
- encoder*.onnx + decoder*.onnx + joiner*.onnx → from_transducer

NeMo 导出的 transducer（Parakeet 英语版：encoder.int8.onnx 等，不带 epoch 后缀）要传 model_type="nemo_transducer"，
否则 sherpa 按 k2 icefall 的格式解析会出错；用目录名里有没有 "nemo" 区分。

语言由模型决定（都是单语模型），transcribe 的 language 参数只做校验，构造时给的 language 要与模型一致。
"""

import os
import re

import numpy as np

try:
    import sherpa_onnx
except ImportError:
    sherpa_onnx = None

from .faster_whisper_engine import AsrResult
from ..models_catalog import resolve as resolve_model_dir
from ..pipelines.open_utterance import FINAL_PARTICLES

SAMPLE_RATE = 16000

# Parakeet 句中几乎不出标点，而是用空格隔开短语（「大丈夫 ひょっとして あなたが…」）。
# 流水线的 join_cjk 会删掉中日文之间的空格，句子边界就丢了，所以这里先把空格换成「、」。
_CJK = r"぀-ヿ㐀-鿿ｦ-ﾟ々〆ー"
PHRASE_SPACE = re.compile(rf"(?<=[{_CJK}])\s+(?=[{_CJK}])")
# Parakeet 会把笑声转成片假名（「フフフフ」「アハ」「ウフフ」），09-30 评测里这是它字错率
# 偏高的主因，送去翻译也只会多出噪声。只删成串的笑声，单个「フ」「ハ」可能是正常词的一部分。
LAUGH = re.compile(r"(?:ウ?フ{2,}|ア?ハ{2,}|アハ|ウフ)[ッー〜]*")


def _space_mark(match):
    """短语间的空格：前面是句末助词就当句号，否则当逗号。"""
    before = match.string[:match.start()]
    return "。" if before.endswith(FINAL_PARTICLES) else "、"


def clean_text_en(text):
    """英语 Parakeet：自带标点，但近一半定稿句末没有（10-01 评测 75 句里 35 句）。
    补上句号让 SenseVoice 和 Parakeet 送进流水线的形式一致；离线 A/B 对人工译文 chrF 0.264 → 0.269，
    差异在噪声范围内，但不会更差。句末是逗号的换成句号（下一句是新的定稿，逗号会让翻译以为没说完）。
    """
    text = " ".join(text.split())
    if text and text[-1] not in ".?!…\"'":
        text = text.rstrip(",;:") + "."
    return text


def clean_text(text):
    text = LAUGH.sub("", text)
    text = PHRASE_SPACE.sub(_space_mark, text.strip())
    # 删掉笑声后可能留下「、、」或开头结尾的「、」。
    text = re.sub(r"[、\s]*、[、\s]*", "、", text).strip("、 ")
    text = re.sub(r"、([。？！?!])", r"\1", text)
    # 句末补「。」：Parakeet 很少出句末标点，而 Hy-MT 对有无句号很敏感
    # （09-30：识别全对的句子仅因少了「。」，译文就从"有什么好惊讶的。"变成"真需要这么惊讶吗"）。
    # SenseVoice 本来就总补「。」，这样两个引擎送进流水线的形式一致。
    if text and text[-1] not in "。？！?!…":
        text += "。"
    return text


def _pick(directory, stem):
    """优先 int8，其次 fp32；找不到返回 None。"""
    candidates = sorted(directory.glob(f"{stem}*.onnx"))
    int8 = [p for p in candidates if "int8" in p.name]
    return (int8 or candidates or [None])[0]


class SherpaOfflineEngine:
    name = "sherpa"

    def __init__(self, model_dir, num_threads=None, language="ja"):
        if sherpa_onnx is None:
            raise SystemExit("未安装 sherpa-onnx：python -m pip install sherpa-onnx")
        directory = resolve_model_dir(model_dir)
        tokens = directory / "tokens.txt"
        if not tokens.is_file():
            raise SystemExit(f"缺少模型文件：{tokens}")
        threads = num_threads or max(1, (os.cpu_count() or 4) // 3)

        self.model_dir = directory
        self.model_name = directory.name
        self.language = language
        encoder = _pick(directory, "encoder")
        if encoder is not None:
            self.kind = "transducer"
            self.recognizer = sherpa_onnx.OfflineRecognizer.from_transducer(
                encoder=str(encoder), decoder=str(_pick(directory, "decoder")),
                joiner=str(_pick(directory, "joiner")), tokens=str(tokens),
                num_threads=threads, decoding_method="greedy_search", debug=False,
                model_type="nemo_transducer" if "nemo" in directory.name else "")
        else:
            model = _pick(directory, "model")
            if model is None:
                raise SystemExit(f"{directory} 里既没有 encoder/decoder/joiner，也没有 model*.onnx")
            self.kind = "nemo_ctc"
            self.recognizer = sherpa_onnx.OfflineRecognizer.from_nemo_ctc(
                model=str(model), tokens=str(tokens), num_threads=threads, debug=False)

    def warm_up(self, language="ja"):
        self.transcribe(np.zeros(SAMPLE_RATE, dtype=np.float32), language=language)

    def transcribe(self, pcm, language="ja", initial_prompt=None, **overrides):
        if initial_prompt:
            raise ValueError(f"{self.model_name} 不支持 initial_prompt")
        if language != self.language:
            raise ValueError(f"{self.model_name} 是 {self.language!r} 专用模型，不能识别 {language!r}")

        stream = self.recognizer.create_stream()
        stream.accept_waveform(SAMPLE_RATE, np.asarray(pcm, dtype=np.float32))
        self.recognizer.decode_stream(stream)
        result = stream.result

        raw = getattr(result, "text", "") or ""
        # 日语清洗（笑声、空格转标点、补句号）只对日语模型；英语模型自带标点和大小写
        text = clean_text(raw) if self.language == "ja" else clean_text_en(raw)
        tokens = list(zip(getattr(result, "tokens", []) or [], getattr(result, "timestamps", []) or []))
        rows = [{"start": 0.0, "end": round(len(pcm) / float(SAMPLE_RATE), 3), "text": text}] if text else []
        return AsrResult(text=text, segments=rows, language=self.language, language_probability=1.0,
                         duration_after_vad=0.0, tokens=tokens)
