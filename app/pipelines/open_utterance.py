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
才能把已达成一致的文本从缓冲区里裁掉。SenseVoice 这个导出没有 `initial_prompt`
（无法续写上文的 prompt）。（09-29 更正：sherpa-onnx 其实给出逐 token 时间戳，
`early_final` 就是靠它在句中切音频的，见下方 find_cut。）
好在 SenseVoice 的单次成本只有约 0.05 秒/音频秒（base 是固定的约 1.1 秒/次），
**重解码整句本来就负担得起**，不需要靠增量提交去省。

代价是：open 段越长，单次重解码越贵（成本 ∝ 段长²/步长）。
所以有 `max_utterance` 兜底，超长句强制切分，成本有上界。
"""

import concurrent.futures
import re
import time

from ..audio.ring_buffer import PcmBuffer
from ..audio.vad import FINE_VAD_OPTIONS, speech_regions, speech_seconds
from ..events import SubtitleEvent
from .base import Job

SAMPLE_RATE = 16000

# 只由这些音节（及标点）组成的整句视为语气词/笑声。刻意不含「い」「ら」「そ」等，
# 所以「はい」「あら」「そう」这类有意义的短句会保留。
FILLER = re.compile(r"^[\s。、，,.…・?？!！ー〜~]*(?:[うんあえはへふ]|ん[ー〜]?)+[\s。、，,.…・?？!！ー〜~]*$")


def is_filler(text):
    return bool(FILLER.match(text))


# SenseVoice 有时在日语词之间插空格（「脇 が甘い 男性 が」）。日语本身不用空格，
# 翻译模型会把空格当成断句，09-30 上限测试里这样的 6 句有 3 句因此译错
# （「脇が甘い」被译成"周围变得甜美"）。只去掉两侧都是中日文字符的空格，英文不受影响。
_CJK = r"぀-ヿ㐀-鿿ｦ-ﾟ々〆ー"
CJK_SPACE = re.compile(rf"(?<=[{_CJK}])\s+(?=[{_CJK}])")


def join_cjk(text):
    return CJK_SPACE.sub("", text)


# --- 提前定稿（early_final）----------------------------------------------------
# 离线模拟（tools/sim_partial_translate.py）的结论：长句里真正能提前的，几乎都是
# "两句被 VAD 粘在一起"——说话人停顿不到 min_silence，前半句早已说完却要等整段结束。
# 这里在 partial 里找句中的句末边界，用 token 时间戳把前半句的音频切出来单独定稿。

PUNCT = set("。、，,.？?！!…・「」『』")
SENTENCE_END = set("。？?！!")
FINAL_PARTICLES = ("かしら", "ね", "よ", "わ", "ぞ", "ぜ")


def content(text):
    """去掉空格和标点后的内容，partial 之间比较稳定性只看这个。"""
    return "".join(ch for ch in text if ch not in PUNCT and not ch.isspace())


def ends_sentence(text):
    """partial 是否以句末形式结尾：问号，或句末助词（ね/よ/わ/かしら…）。

    不能只看「。」——SenseVoice 每次都在末尾补一个「。」，说到一半也有。
    """
    raw = text.rstrip()
    if raw.endswith(("？", "?", "！", "!")):
        return True
    return content(raw).endswith(FINAL_PARTICLES)


def find_cut(tokens, prev_content, min_chars=6, min_gap=0.2):
    """在一次 partial 的 token 序列里找可以提前定稿的切点，返回相对本段起点的秒数或 None。

    条件（全部满足才切）：
    - 边界是句中的「。？！」，或句末助词（ね/よ/わ/かしら…）后面跟空格或「、」；
    - 边界前至少 min_chars 个内容字，后面至少 2 个；
    - 到"边界后 2 个字"为止的内容与上一次 partial 一致（稳定前缀），末尾在改的不算；
    - 边界两侧的 token 时间间隔 ≥ min_gap，说明这里真有停顿，切在中点不会截断发音。
    """
    chars = []          # [(内容字, 时间)]
    marks = []          # [(边界前的内容字数)]
    for token, stamp in tokens:
        if token.strip() == "":
            body = "".join(ch for ch, _ in chars)
            if chars and body.endswith(FINAL_PARTICLES):
                marks.append(len(chars))
            continue
        if all(ch in PUNCT for ch in token):
            body = "".join(ch for ch, _ in chars)
            if chars and (any(ch in SENTENCE_END for ch in token)
                          or ("、" in token and body.endswith(FINAL_PARTICLES))):
                marks.append(len(chars))
            continue
        for ch in token:
            chars.append((ch, stamp))

    now = "".join(ch for ch, _ in chars)
    stable = next((i for i, (a, b) in enumerate(zip(now, prev_content)) if a != b),
                  min(len(now), len(prev_content)))
    for pos in marks:
        if pos < min_chars or pos + 2 > stable:
            continue
        before, after = chars[pos - 1][1], chars[pos][1]
        if after - before >= min_gap:
            return (before + after) / 2
    return None


class OpenUtterancePipeline:
    name = "open_utterance"

    def __init__(self, engine, language="ja", sample_rate=SAMPLE_RATE,
                 min_speech=0.4, min_silence=0.35, partial_step=1.0,
                 max_utterance=10.0, call_timeout=None, drop_fillers=True, early_final=False,
                 adaptive_silence=None, final_engine=None):
        self.engine = engine
        # 定稿专用引擎（None = 与 partial 同一个）。09-30：Parakeet 识别更准，但每 0.5s 重解码
        # 整句让草稿延迟翻倍；混合模式下草稿用快的 SenseVoice，只在定稿时让 Parakeet 解码一次。
        # 翻译只吃定稿，所以准确度收益全部保留。
        self.final_engine = final_engine
        self.language = language
        self.sample_rate = sample_rate
        self.min_speech = min_speech
        self.min_silence = min_silence
        self.partial_step = partial_step
        self.max_utterance = max_utterance
        self.call_timeout = call_timeout
        self.drop_fillers = drop_fillers
        self.early_final = early_final
        # 自适应静音阈值（秒）；None = 关闭。partial 以句末形式结尾时，静音达到它就定稿。
        self.adaptive_silence = adaptive_silence
        self._prev_partial = ""      # 上一次 partial 的内容，用来判断稳定前缀
        self._pending_cut = None     # run_job 找到的切点（绝对秒数），由下一次 next_job 执行
        self._partial_end = -1.0     # 最近一次 partial 覆盖到的音频位置
        self._sentence_like = False  # 最近一次 partial 是否以句末形式结尾

        self.buffer = PcmBuffer(sample_rate)
        self._open_start = None      # 当前开放段的起点（音频位置秒）
        self._segment_id = 0
        self._revision = 0
        self._last_decode_end = 0.0
        self._executors = {}
        self.stats = {"calls": 0, "skipped": 0, "partials": 0, "finals": 0,
                      "forced_cuts": 0, "timeouts": 0, "fillers": 0, "early_finals": 0,
                      "adaptive_finals": 0}

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

        if self.adaptive_silence is not None:
            fine_end = self._fine_speech_end()
            if fine_end is not None and self.buffer.end - fine_end >= self.adaptive_silence:
                if self._partial_end >= fine_end:
                    # 最近一次 partial 已经覆盖了到目前为止的全部语音，而且它以句末形式结尾：
                    # 用短静音阈值定稿，不必等满 min_silence。切点留一点余量，别削掉词尾。
                    if self._sentence_like:
                        self.stats["adaptive_finals"] += 1
                        return self._close(min(fine_end + 0.05, self.buffer.end),
                                           forced=False, adaptive=True)
                elif self.buffer.end > self._last_decode_end:
                    # 停顿刚开始、还没解码过这段语音的结尾：立刻补一次 partial 看看是不是句末，
                    # 不等 partial_step（SenseVoice 一次只要约 0.05s/音频秒，负担得起）。
                    return self._partial_job()

        cut = self._pending_cut
        if cut is not None and cut <= self._open_start + self.min_speech:
            self._pending_cut = cut = None      # 切点太靠前，切出来的前半句会被门控丢掉
        if cut is not None and cut < speech_end:
            # 前半句提前定稿：只重解码 [开放段起点, 切点]，剩下的音频留在缓冲区，
            # 下一次 next_job 会从切点之后的语音重新开一段。
            self.stats["early_finals"] += 1
            return self._close(cut, forced=False, early=True)

        # 还没结束：按步长刷新 partial。
        if self.buffer.end - self._last_decode_end >= self.partial_step:
            return self._partial_job()
        return None

    def _fine_speech_end(self, tail=1.5):
        """用细粒度 VAD 只看缓冲区最后 tail 秒，返回最后一个语音区间的终点（绝对秒）。"""
        start = max(self._open_start, self.buffer.end - tail)
        pcm = self.buffer.slice(start, self.buffer.end)
        if pcm.size == 0:
            return None
        regions = speech_regions(pcm, FINE_VAD_OPTIONS)
        return start + regions[-1][1] if regions else None

    def _partial_job(self):
        start, end = self._open_start, self.buffer.end
        self._revision += 1
        self._last_decode_end = end
        pcm = self.buffer.slice(start, end)
        self.stats["partials"] += 1
        return Job("partial", start, end, pcm,
                   meta={"segment_id": self._segment_id, "revision": self._revision})

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
        engine = self.final_engine if is_final and self.final_engine is not None else self.engine
        started = time.perf_counter()
        try:
            result = self._call(job.pcm, engine, job.kind)
        except TimeoutError:
            # 单次调用必须有硬时间预算：台架上曾观察到某一块耗时 1255 秒且不复现，
            # 当时流水线没有超时也没有看门狗，字幕会彻底停摆且不会恢复。
            self.stats["timeouts"] += 1
            return [SubtitleEvent(
                segment_id=segment_id, revision=revision, text="", is_final=is_final,
                engine=engine.name, audio_start=job.audio_start,
                audio_end=job.audio_end,
                detail={"timeout": True, "call_timeout": self.call_timeout},
            )]
        elapsed = time.perf_counter() - started

        text = join_cjk(result.text)
        filtered = bool(text) and self.drop_fillers and is_filler(text)
        if filtered:
            # 整句只有语气词/笑声（「う。」「へへ。」「ふふふ。」）：评测集上几乎都是
            # 短停顿里的呼吸或笑声被识别成音节，送去翻译只会产生噪声字幕。
            self.stats["fillers"] += 1
            text = ""

        if is_final:
            self.stats["finals"] += 1
        elif segment_id == self._segment_id:
            if self.early_final:
                tokens = getattr(result, "tokens", None) or []
                rel = find_cut(tokens, self._prev_partial) if tokens else None
                if rel is not None:
                    self._pending_cut = job.audio_start + rel
            self._prev_partial = content(result.text)
            self._partial_end = job.audio_end
            self._sentence_like = ends_sentence(result.text)
        self.stats["calls"] += 1

        return [SubtitleEvent(
            segment_id=segment_id, revision=revision, text=text,
            is_final=is_final, engine=engine.name,
            audio_start=job.audio_start, audio_end=job.audio_end,
            detail={
                **({"filler": result.text} if filtered else {}),
                "speech_seconds": round(speech, 3),
                "utterance_seconds": round(job.audio_end - job.audio_start, 3),
                "engine_seconds": round(elapsed, 4),
                "forced_cut": job.meta.get("forced", False),
                **({"early_final": True} if job.meta.get("early") else {}),
                **({"adaptive_final": True} if job.meta.get("adaptive") else {}),
            },
        )]

    # --- 内部 -------------------------------------------------------------

    def _close(self, speech_end, forced, early=False, adaptive=False):
        """结束当前开放段，产出一个 final（或门控跳过）作业。"""
        self._prev_partial = ""
        self._pending_cut = None
        self._partial_end = -1.0
        self._sentence_like = False
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
                   meta={"segment_id": segment_id, "revision": revision, "forced": forced,
                         "early": early, "adaptive": adaptive})

    def _call(self, pcm, engine=None, kind="partial"):
        engine = engine or self.engine
        if not self.call_timeout:
            return engine.transcribe(pcm, language=self.language)
        # 服务端可能在另一个线程里跑定稿（Session.async_finals），草稿和定稿各用一个执行器，
        # 否则一边超时 shutdown 会把另一边正在等的执行器一起关掉。
        executor = self._executors.get(kind)
        if executor is None:
            executor = self._executors[kind] = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        future = executor.submit(engine.transcribe, pcm, self.language)
        try:
            return future.result(timeout=self.call_timeout)
        except concurrent.futures.TimeoutError:
            # 注意：ONNX 推理无法真正取消，这里只是"不再等它"。
            # 真实服务必须把识别放进可杀死的子进程，否则线程会一直占着资源。
            executor.shutdown(wait=False)
            self._executors.pop(kind, None)
            raise TimeoutError(f"transcribe exceeded {self.call_timeout}s")
