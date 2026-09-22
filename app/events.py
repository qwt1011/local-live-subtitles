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
    translation: str = ""
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
        if self.translation:
            row["translation"] = self.translation
        return row


def latency_views(rows, samples=20):
    """按"某个音节第一次出现在屏幕上的延迟"计算延迟。

    这是唯一在**固定分块**和**流式增量**之间可比的口径，台架和报告都必须用它，
    否则两边的数字会各说各话。

    - 固定分块：每个 segment 只有一个事件，覆盖 [a, b]。区间内任意 t 的延迟都是
      `finish_wall - t`，于是均值退化为 `finish_wall - 中点`、最坏为 `finish_wall - a`。
    - 流式增量：一个 segment 有多个 partial。靠前的音节被较早的 partial 显示，
      靠后的音节约到最后一个事件才显示。必须逐点取"第一个覆盖到 t 的事件"的
      finish_wall，而不是拿最后那个事件去惩罚整句。

    不做这个区分会把流式流水线算得比实际差得多：M2 用最后事件算 p95 是 3.26 秒，
    用正确口径是 0.66 秒。

    `rows` 是 `SubtitleEvent.to_dict()` 的结果（或同结构 dict）。
    """
    per_segment = {}
    for row in rows:
        per_segment.setdefault(row.get("segment_id", 0), []).append(row)

    means, worsts, final_stale, first_show = [], [], [], []

    for rows_in_segment in per_segment.values():
        ordered = sorted(rows_in_segment, key=lambda item: item.get("revision", 0))
        if not any(row.get("text") for row in ordered):
            continue  # 门控/超时产生的事件不参与延迟统计

        start = min(row["audio_start"] for row in ordered)
        end = max(row["audio_end"] for row in ordered)
        finishes = [row["finish_wall"] for row in ordered]

        points = 1 if end <= start else samples
        local = []
        for index in range(points):
            t = start if points == 1 else start + (end - start) * index / (points - 1)
            candidates = [row["finish_wall"] for row in ordered if row["audio_end"] >= t - 1e-9]
            local.append((min(candidates) if candidates else min(finishes)) - t)
        means.append(sum(local) / len(local))
        worsts.append(max(local))

        # 说完最后一个字 → 屏幕上的文本不再变化（定稿延迟）
        finals = [row for row in ordered if row.get("is_final")]
        if finals:
            final_stale.append(min(row["finish_wall"] for row in finals) - end)
        # 句子开口 → 屏幕上有字（首字延迟）
        first_show.append(min(finishes) - start)

    return {"mean": means, "worst": worsts,
            "final_stale": final_stale, "first_show": first_show}


def percentile(values, q):
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * q
    low = int(position)
    high = min(low + 1, len(ordered) - 1)
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)
