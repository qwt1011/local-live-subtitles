"""faster-whisper 引擎封装。

服务（local_service.py）与回放台架（tools/replay.py）共用这里的参数，
避免出现"服务里一套参数、台架里另一套"导致对比失真。
"""

from dataclasses import dataclass, field

import numpy as np

from faster_whisper import WhisperModel

# 默认识别参数。
#
# temperature 阶梯从 faster-whisper 默认的 [0.0, 0.2, ..., 1.0] 收窄到 [0.0, 0.4]：
# 默认阶梯会在低置信块上用更高温度**重解码整段**（最多 7 遍），实测把同一个 3 秒块
# 从 2.30s 推到 6.59s、从 2.55s 推到 7.21s（diag_fallback.py）。
# 但温度阶梯同时也在压制复读，所以不能只是删掉，必须用下面两个参数显式补上。
DEFAULT_ASR_KWARGS = {
    "beam_size": 1,
    "temperature": [0.0, 0.4],
    "repetition_penalty": 1.1,
    "no_repeat_ngram_size": 3,
    "vad_filter": True,
    # 隔离的短块没有可信上文，开启只会让模型延续上一块的幻觉。
    # 流式内核用"已提交文本"自己构造 prompt。
    "condition_on_previous_text": False,
}


@dataclass
class AsrResult:
    text: str
    segments: list = field(default_factory=list)
    language: str = "unknown"
    language_probability: float = 0.0
    duration_after_vad: float = 0.0


class WhisperEngine:
    name = "faster-whisper"

    def __init__(self, model_name: str = "base", device: str = "cpu",
                 compute_type: str = "int8", **overrides):
        self.model_name = model_name
        self.kwargs = dict(DEFAULT_ASR_KWARGS)
        self.kwargs.update(overrides)
        self.model = WhisperModel(model_name, device=device, compute_type=compute_type)

    def warm_up(self, language: str = "ja") -> None:
        """把惰性初始化成本在开始计时之前付掉。

        实测首次真实调用出现过 8.4 秒的异常值；不预热会把它算到用户第一个请求上。
        """
        silence = np.zeros(16000, dtype=np.float32)
        segments, _ = self.model.transcribe(silence, language=language, **self.kwargs)
        list(segments)

    def transcribe(self, pcm, language: str = "ja", initial_prompt=None, **overrides) -> AsrResult:
        kwargs = dict(self.kwargs)
        kwargs.update(overrides)
        if initial_prompt:
            kwargs["initial_prompt"] = initial_prompt
        segments, info = self.model.transcribe(pcm, language=language, **kwargs)
        # transcribe 是惰性的，必须在这里把生成器消费完；
        # 否则真实异常会被后续的 NameError 掩盖（原型的 finally bug）。
        rows = [
            {"start": round(s.start, 3), "end": round(s.end, 3), "text": s.text.strip()}
            for s in segments
        ]
        text = " ".join(row["text"] for row in rows).strip()
        return AsrResult(
            text=text,
            segments=rows,
            language=info.language,
            language_probability=info.language_probability,
            duration_after_vad=info.duration_after_vad,
        )
