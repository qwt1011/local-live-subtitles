"""流水线协议。

台架只依赖这个协议：喂音频 → 要作业 → 执行作业 → 收事件。
这样"固定分块基线"和"流式内核"可以用完全相同的台架和指标对比，
换架构不需要改台架，换台架也不需要改流水线。
"""

from dataclasses import dataclass, field
from typing import Protocol

import numpy as np


@dataclass
class Job:
    """一次模型调用。`audio_start/audio_end` 是它覆盖的音频区间。"""

    kind: str
    audio_start: float
    audio_end: float
    pcm: np.ndarray
    meta: dict = field(default_factory=dict)


class Pipeline(Protocol):
    name: str

    def push_audio(self, pcm: np.ndarray) -> None:
        """追加按顺序到达的 16 kHz 单声道音频。"""

    def next_job(self, available_until: float, eof: bool) -> Job | None:
        """返回下一个要执行的作业；None 表示当前无事可做。

        `eof=True` 表示音频已经全部到达，此时应把剩余工作排空。
        """

    def run_job(self, job: Job) -> list:
        """执行作业并返回 SubtitleEvent 列表。真实耗时由台架测量。"""
