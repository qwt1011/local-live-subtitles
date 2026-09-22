"""M2 流式内核：VAD 驱动的"开放段重解码"。

## 为什么是这个结构

固定分块的三个毛病在台架上都量化过（BENCHMARK_RESULTS.md）：

1. **延迟下限 = 块长 + 单次调用成本**（`最坏 ≈ L + C`），块长本身就是等待；
2. **块边界会切在词中间**，且块之间没有重叠 → 边界词必然丢失；
3. **上下文被切断**：2 秒块 CER 0.099 / 3 秒块 0.066 / 整段 0.022。

本实现用"VAD 定句 + 开放段重解码"同时解决这三条：

```
        ┌── 句子开放中：每 partial_step 秒重解码整句 → partial（可被替换）
VAD ────┤
        └── 检测到 ≥ min_silence 的静音 → 解码整句 → final（定稿）
```

- 句子边界由 VAD 决定，**永远不会切在词中间**（第 2 条）；
- final 用整句音频解码，**上下文是完整的**（第 3 条）；
- partial 只是"预览"，可以每 `partial_step` 秒刷新一次，所以延迟不受句子长度限制（第 1 条）。

## 为什么不用 LocalAgreement-2

whisper_streaming 的 LocalAgreement 需要在**文本前缀**和**音频时间**之间建立对应关系，
才能把已达成一致的文本从缓冲区里裁掉。SenseVoice 这个导出既没有 `initial_prompt`
（无法续写上文的 prompt），也没有词级时间戳，做不了这个对应。
好在 SenseVoice 的单次成本只有约 0.05 秒/音频秒（base 是固定的约 1.1 秒/次），
**重解码整句本来就负担得起**，不需要靠增量提交去省。

代价是：open 段越长，单次重解码越贵（成本 ∝ 段长²/步长）。
所以有 `max_utterance` 兜底，超长句强制切分，成本有上界。
"""

import concurrent.futures
import time

from ..audio.ring_buffer import PcmBuffer
from ..audio.vad import speech_regions, speech_seconds
from ..events import SubtitleEvent
from .base import Job

SAMPLE_RATE = 16000


class OpenUtterancePipeline:
    name = "open_utterance"

    def __init__(self, engine, language="ja", sample_rate=SAMPLE_RATE,
                 min_speech=0.4, min_silence=0.35, partial_step=1.0,
                 max_utterance=10.0, call_timeout=None):
        self.engine = engine
        self.language = language
        self.sample_rate = sample_rate
        self.min_speech = min_speech
        self.min_silence = min_silence
        self.partial_step = partial_step
        self.max_utterance = max_utterance
        self.call_timeout = call_timeout

        self.buffer = PcmBuffer(sample_rate)
        self._open_start = None      # 当前开放段的起点（音频位置秒）
        self._segment_id = 0
        self._revision = 0
        self._last_decode_end = 0.0
        self._executor = None
        self.stats = {"calls": 0, "skipped": 0, "partials": 0, "finals": 0,
                      "forced_cuts": 0, "timeouts": 0}

    # --- 台架接口 ---------------------------------------------------------

    def push_audio(self, pcm):
        self.buffer.push(pcm)

    def next_job(self, available_until, eof):
        if self.buffer.empty:
            return None

        regions = [(self.buffer.start + start, self.buffer.start + end)
                   for start, end in speech_regions(self.buffer.to_array())]

        if self._open_start is None:
            if not regions:
                # 纯静音：直接丢掉，别让缓冲区无界增长（"永不排队"不变式）。
                if self.buffer.end > 0:
                    self.buffer.drop_before(self.buffer.end)
                return None
            self._open_start = regions[0][0]
            self._last_decode_end = self._open_start

        speech_end = max((end for _, end in regions if end > self._open_start),
                         default=self._open_start)
        trailing_silence = self.buffer.end - speech_end
        utterance_seconds = speech_end - self._open_start

        forced = utterance_seconds >= self.max_utterance
        closing = eof or trailing_silence >= self.min_silence or forced

        if closing:
            return self._close(speech_end, forced)

        # 还没结束：按步长刷新 partial。
        if self.buffer.end - self._last_decode_end >= self.partial_step:
            start, end = self._open_start, self.buffer.end
            self._revision += 1
            self._last_decode_end = end
            pcm = self.buffer.slice(start, end)
            self.stats["partials"] += 1
            return Job("partial", start, end, pcm,
                       meta={"segment_id": self._segment_id, "revision": self._revision})
        return None

    def run_job(self, job):
        segment_id = job.meta["segment_id"]
        revision = job.meta["revision"]
        is_final = job.kind == "final"

        if job.kind == "skip":
            return [SubtitleEvent(
                segment_id=segment_id, revision=revision, text="", is_final=True,
                engine=self.engine.name, audio_start=job.audio_start,
                audio_end=job.audio_end, detail={"gated": True},
            )]

        speech = speech_seconds(job.pcm)
        started = time.perf_counter()
        try:
            result = self._call(job.pcm)
        except TimeoutError:
            # 单次调用必须有硬时间预算：台架上曾观察到某一块耗时 1255 秒且不复现，
            # 当时流水线没有超时也没有看门狗，字幕会彻底停摆且不会恢复。
            self.stats["timeouts"] += 1
            return [SubtitleEvent(
                segment_id=segment_id, revision=revision, text="", is_final=is_final,
                engine=self.engine.name, audio_start=job.audio_start,
                audio_end=job.audio_end,
                detail={"timeout": True, "call_timeout": self.call_timeout},
            )]
        elapsed = time.perf_counter() - started

        if is_final:
            self.stats["finals"] += 1
        self.stats["calls"] += 1

        return [SubtitleEvent(
            segment_id=segment_id, revision=revision, text=result.text,
            is_final=is_final, engine=self.engine.name,
            audio_start=job.audio_start, audio_end=job.audio_end,
            detail={
                "speech_seconds": round(speech, 3),
                "utterance_seconds": round(job.audio_end - job.audio_start, 3),
                "engine_seconds": round(elapsed, 4),
                "forced_cut": job.meta.get("forced", False),
            },
        )]

    # --- 内部 -------------------------------------------------------------

    def _close(self, speech_end, forced):
        """结束当前开放段，产出一个 final（或门控跳过）作业。"""
        start = self._open_start
        end = max(speech_end, start)
        pcm = self.buffer.slice(start, end)
        speech = speech_seconds(pcm) if pcm.size else 0.0

        self._revision += 1
        segment_id = self._segment_id
        revision = self._revision

        self._segment_id += 1
        self._revision = 0
        self._open_start = None
        # 已定稿的音频立刻丢掉，缓冲区只保留未提交部分。
        if end > self.buffer.start:
            self.buffer.drop_before(end)
        self._last_decode_end = end

        if speech < self.min_speech or pcm.size == 0:
            self.stats["skipped"] += 1
            return Job("skip", start, end, pcm,
                       meta={"segment_id": segment_id, "revision": revision})
        if forced:
            self.stats["forced_cuts"] += 1
        return Job("final", start, end, pcm,
                   meta={"segment_id": segment_id, "revision": revision, "forced": forced})

    def _call(self, pcm):
        if not self.call_timeout:
            return self.engine.transcribe(pcm, language=self.language)
        if self._executor is None:
            self._executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        future = self._executor.submit(self.engine.transcribe, pcm, self.language)
        try:
            return future.result(timeout=self.call_timeout)
        except concurrent.futures.TimeoutError:
            # 注意：ONNX 推理无法真正取消，这里只是"不再等它"。
            # 真实服务必须把识别放进可杀死的子进程，否则线程会一直占着资源。
            self._executor.shutdown(wait=False)
            self._executor = None
            raise TimeoutError(f"transcribe exceeded {self.call_timeout}s")
