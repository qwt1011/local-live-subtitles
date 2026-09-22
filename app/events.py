"""字幕事件 schema。"""

from dataclasses import dataclass, field


@dataclass
class SubtitleEvent:
    """一次识别产出的字幕状态。

    partial 与 final 用同一个结构表示，靠 `is_final` 区分，靠 `revision` 排序。
    渲染端只允许 revision 单调前进，否则字幕会回跳。
    """

    segment_id: int
    revision: int
    text: str
    is_final: bool
    engine: str
    audio_start: float
    audio_end: float
    service_seconds: float = 0.0
    finish_wall: float = 0.0
    detail: dict = field(default_factory=dict)

    @property
    def latency(self) -> float:
        """该段**最后**一个音节的延迟（乐观值）。

        对固定分块来说这个数字会明显偏小：一个 2 秒块在 2.9 秒时提交，
        块尾的语音只等了 0.9 秒，但块首的语音等了 2.9 秒。
        只报这一个数会把分块架构的代价算漏。
        """
        return self.finish_wall - self.audio_end

    @property
    def latency_worst(self) -> float:
        """该段**第一个**音节的延迟（最坏值）。"""
        return self.finish_wall - self.audio_start

    @property
    def latency_mean(self) -> float:
        """段内语音的平均延迟。对均匀分布的语音是精确值，作为头条指标。"""
        return self.finish_wall - (self.audio_start + self.audio_end) / 2.0

    def to_dict(self) -> dict:
        row = {
            "segment_id": self.segment_id,
            "revision": self.revision,
            "text": self.text,
            "is_final": self.is_final,
            "engine": self.engine,
            "audio_start": round(self.audio_start, 3),
            "audio_end": round(self.audio_end, 3),
            "service_seconds": round(self.service_seconds, 4),
            "finish_wall": round(self.finish_wall, 4),
            "latency_end": round(self.latency, 4),
            "latency_mean": round(self.latency_mean, 4),
            "latency_worst": round(self.latency_worst, 4),
        }
        if self.detail:
            row["detail"] = self.detail
        return row
