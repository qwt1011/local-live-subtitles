"""Silero VAD 门控。

faster-whisper 自带 Silero VAD（ONNX），不需要新依赖。

注意区分两件事：
- `vad_filter=True` 只是让 Whisper 在内部丢掉非语音片段，**它不能阻止这次调用发生**；
- `speech_seconds()` 才是"决定要不要花一次调用"的门控。

实测依据（ARCHITECTURE_REVIEW.md 1.3）：VAD 后只剩 0.66 秒语音的块是全场最贵的调用之一，
且输出纯幻觉；VAD 后 0 秒语音的块同样被白白调用了一次。
"""

import numpy as np

from faster_whisper.vad import VadOptions, get_speech_timestamps

# 350 ms 静音判定为停顿；100 ms padding 避免把词头词尾削掉。
DEFAULT_VAD_OPTIONS = VadOptions(min_silence_duration_ms=350, speech_pad_ms=100)

SAMPLE_RATE = 16000


def speech_seconds(pcm, options=None) -> float:
    """VAD 判定出的语音总时长（秒）。"""
    stamps = get_speech_timestamps(pcm, options or DEFAULT_VAD_OPTIONS, sampling_rate=SAMPLE_RATE)
    return sum(stamp["end"] - stamp["start"] for stamp in stamps) / float(SAMPLE_RATE)


def speech_regions(pcm, options=None):
    """返回语音区间 [(start_seconds, end_seconds), ...]。"""
    stamps = get_speech_timestamps(pcm, options or DEFAULT_VAD_OPTIONS, sampling_rate=SAMPLE_RATE)
    return [(stamp["start"] / SAMPLE_RATE, stamp["end"] / SAMPLE_RATE) for stamp in stamps]


def trim_to_speech(pcm, options=None) -> np.ndarray:
    """把首尾的非语音裁掉，减少送入模型的静音 padding。"""
    regions = speech_regions(pcm, options)
    if not regions:
        return pcm[:0]
    start = int(regions[0][0] * SAMPLE_RATE)
    end = int(regions[-1][1] * SAMPLE_RATE)
    return pcm[start:end]
