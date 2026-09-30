"""基线流水线：忠实复现"停滞版本"的架构。

它模拟的是当前真实链路：
    客户端每 N 秒切一个块（不重叠、不等识别） → HTTP 上传 → 服务端一把全局锁串行识别

存在意义是给流式内核一个**必须打败的对照数字**，而不是当作可用实现。
它天然带有两个在 docs/ARCHITECTURE_REVIEW.md 里论证过的缺陷：
  1. 延迟下限 = chunk_seconds + 单次调用成本（不可通过调参绕过）；
  2. 单模型串行 + 无背压 → 一旦单块成本 > chunk_seconds，墙钟就单调落后，永不恢复。

`min_speech=0.0` 复现"原始原型"；`min_speech=0.4` 复现"M0 修复后"。
两者的差值就是 M0 那几个改动的量化收益。
"""

from collections import deque

import numpy as np

from ..audio.vad import speech_seconds
from ..events import SubtitleEvent
from .base import Job

SAMPLE_RATE = 16000


class FixedChunkPipeline:
    name = "fixed_chunk"

    def __init__(self, engine, chunk_seconds: float = 2.0, language: str = "ja",
                 min_speech: float = 0.0, sample_rate: int = SAMPLE_RATE):
        self.engine = engine
        self.chunk_seconds = chunk_seconds
        self.language = language
        self.min_speech = min_speech
        self.sample_rate = sample_rate
        self._frames = deque()
        self._buffered = 0
        self._cursor = 0.0      # 已被切走的音频位置
        self._segment_id = 0
        self.stats = {"calls": 0, "skipped": 0}

    # --- 台架接口 ---------------------------------------------------------

    def push_audio(self, pcm: np.ndarray) -> None:
        if len(pcm):
            self._frames.append(np.asarray(pcm, dtype=np.float32))
            self._buffered += len(pcm)

    def next_job(self, available_until: float, eof: bool) -> Job | None:
        need = int(round(self.chunk_seconds * self.sample_rate))
        if self._buffered >= need:
            count = need
            span = self.chunk_seconds
        elif eof and self._buffered > 0:
            # 收尾：把不足一块的尾巴也处理掉，否则最后一段永远不出字幕。
            count = self._buffered
            span = count / float(self.sample_rate)
        else:
            return None

        pcm = self._take(count)
        start = self._cursor
        self._cursor = start + span
        return Job("chunk", start, start + span, pcm)

    def run_job(self, job: Job) -> list:
        self._segment_id += 1
        speech = speech_seconds(job.pcm)
        gate = self.min_speech > 0.0

        if gate and speech < self.min_speech:
            self.stats["skipped"] += 1
            return [SubtitleEvent(
                segment_id=self._segment_id,
                revision=0,
                text="",
                is_final=True,
                engine=self.engine.name,
                audio_start=job.audio_start,
                audio_end=job.audio_end,
                detail={"gated": True, "speech_seconds": round(speech, 3)},
            )]

        self.stats["calls"] += 1
        result = self.engine.transcribe(job.pcm, language=self.language)
        return [SubtitleEvent(
            segment_id=self._segment_id,
            revision=0,
            text=result.text,
            is_final=True,
            engine=self.engine.name,
            audio_start=job.audio_start,
            audio_end=job.audio_end,
            detail={
                "gated": False,
                "speech_seconds": round(speech, 3),
                "duration_after_vad": round(result.duration_after_vad, 3),
            },
        )]

    # --- 内部 -------------------------------------------------------------

    def _take(self, count: int) -> np.ndarray:
        parts = []
        got = 0
        while got < count:
            head = self._frames[0]
            take = min(count - got, len(head))
            parts.append(head[:take])
            if take == len(head):
                self._frames.popleft()
            else:
                self._frames[0] = head[take:]
            got += take
        self._buffered -= count
        return parts[0] if len(parts) == 1 else np.concatenate(parts)
