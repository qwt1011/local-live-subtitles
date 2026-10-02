"""流式字幕服务：PCM 流进，partial / final 事件流出。

这是 M3 的核心，替换原来"每 2 秒一个 HTTP 请求 + 服务端全局锁串行"的做法。

## 协议

客户端 → 服务端
  - 文本帧（控制）：`{"type":"start","language":"ja"}` / `{"type":"stop"}` / `{"type":"ping"}`
  - 二进制帧：裸 PCM，**s16le / 16 kHz / 单声道**，连续追加。
    客户端不做任何分段决策（docs/ARCHITECTURE_REVIEW.md 第 4 节不变式 1）。

服务端 → 客户端（JSON 文本帧）
  - `{"type":"event","segment_id":n,"revision":n,"text":"...","is_final":bool,
      "audio_start":..,"audio_end":..,"latency":..,"engine_seconds":..}`
  - `{"type":"status", ...}` 回应 ping / start
  - `{"type":"error","error":"..."}`

## 延迟怎么算

服务端知道两件事：会话开始时的墙钟、以及累计收到的音频长度。
音频按 1x 连续到达，所以"现在已经播到第几秒"≈ 会话已过去的时间。于是

    该事件的延迟 = 已过去的时间 - 事件覆盖到的音频位置

这和离线台架 `tools/replay.py` 的口径完全一致，因此**真实浏览器会话可以落成
同样格式的 JSONL，直接用 tools/metrics.py 分析**（--log 参数）。

## 线程模型

- asyncio 主循环：收 PCM、发事件。
- 一个识别工作线程：不停地"取作业 → 跑识别 → 投事件"。
- 一把锁只保护流水线状态（缓冲区与作业颁发），**识别调用在锁外执行**，
  这样音频不会被识别阻塞。
- 队列满时丢弃最旧的事件而不是无限堆积（"永不排队"不变式 3）。
"""

import argparse
import asyncio
import json
import os
import queue
import threading
import time
from pathlib import Path

import numpy as np

from .asr.factory import ENGINES, create_engine
from .events import SubtitleEvent, latency_views, percentile
from .pipelines.open_utterance import OpenUtterancePipeline
from .translate.address_memory import AddressMemory

SAMPLE_RATE = 16000
MAX_EVENT_QUEUE = 64
MAX_TRANSLATE_QUEUE = 16


def asr_settings(args, language, engine, model):
    """Resolve per-language defaults without overriding explicit global settings."""
    english_parakeet = language == "en" and engine == "sherpa" and "parakeet" in str(model).lower()
    threads = getattr(args, "en_threads", None) if language == "en" else None
    if threads is None:
        threads = args.threads
    if threads is None and english_parakeet:
        threads = min(3, max(1, (os.cpu_count() or 4) // 3))
    step = getattr(args, "en_partial_step", None) if language == "en" else None
    if step is None:
        step = args.partial_step
    if step is None:
        step = 0.75 if english_parakeet else 0.5
    return threads, step


class EnginePool:
    """按语言提供识别引擎。

    10-01 起服务同时支持日语和英语：启动时按 --engine/--model 加载主引擎（服务的默认语言），
    其他语言的会话在第一次用到时再加载 SenseVoice 的对应语言实例并缓存——
    Parakeet 只能识别日语，SenseVoice 的语言是构造参数，换语言必须另建实例。
    """

    def __init__(self, args, engine, final_engine=None):
        self.args = args
        self.default_language = args.language
        self.engines = {args.language: (engine, final_engine)}
        self.lock = threading.Lock()

    def spec(self, language):
        """非默认语言用哪个引擎：英语看 --en-engine/--en-model（默认 Parakeet unified），其他语言用 SenseVoice。"""
        if language == "en":
            return self.args.en_engine, self.args.en_model
        return "sensevoice", "sensevoice-2024"

    def get(self, language):
        with self.lock:
            if language not in self.engines:
                name, model = self.spec(language)
                print(f"首次使用 {language}：加载 {name} / {model} …", flush=True)
                began = time.perf_counter()
                try:
                    threads, _ = asr_settings(self.args, language, name, model)
                    engine = create_engine(name, model, language=language,
                                           threads=threads)
                except SystemExit as exc:
                    # 模型没下载：退回 SenseVoice，别让整个会话起不来
                    print(f"  加载失败（{exc}），改用 SenseVoice", flush=True)
                    threads, _ = asr_settings(self.args, language, "sensevoice", "sensevoice-2024")
                    engine = create_engine("sensevoice", "sensevoice-2024", language=language,
                                           threads=threads)
                print(f"{language} 引擎就绪（{time.perf_counter() - began:.2f}s）", flush=True)
                # 混合模式的定稿引擎是日语 Parakeet，其他语言不用它
                self.engines[language] = (engine, None)
            return self.engines[language]


SUPPORTED_LANGUAGES = ("ja", "en")


class Session:
    def __init__(self, args, engine, translator=None, final_engine=None, pool=None):
        self.args = args
        self.engine = engine
        self.translator = translator
        self.pool = pool
        self.language = args.language
        self.pipeline = self._build_pipeline(engine, final_engine, args.language)
        self.lock = threading.Lock()
        self.translate_queue = queue.Queue(maxsize=MAX_TRANSLATE_QUEUE)
        self.translate_thread = None
        # 独立定稿线程（--async-finals，默认关闭）：定稿作业交给另一个线程，识别线程接着刷草稿。
        # 09-30 实时推流实测（tools/live_bench.py）反而更慢：两个 ONNX 推理并发抢同一批 CPU 核，
        # Parakeet 中文 p50 从 1.04s 变成 1.87s、p90 从 1.27s 变成 3.89s。保留开关供以后换机器再测。
        # 定稿不能丢（翻译只吃定稿），所以队列不设上限——它的量只有草稿的约 1/6。
        self.async_finals = getattr(args, "async_finals", False)
        self.final_queue = queue.Queue()
        self.final_thread = None
        self.stats_lock = threading.Lock()
        # 已定稿并已翻译的句子，供后续句子当上文用（原文 + 译文）
        self.history = []
        self.address_memory = AddressMemory()
        self.address_consistency = False
        self.address_epoch = 0
        self.address_cutoff = 0.0
        self.loop = None
        self.aset_queue = None
        self.dropped = 0
        self.translated = 0
        self.translate_dropped = 0
        self.started_at = time.perf_counter()
        self.received_samples = 0
        self.configuring = False   # 正在处理 start 消息（可能在换引擎），期间收到的音频先缓存
        self.pending = []
        self.thread = None
        self.running = False
        self.eof = False
        self.rows = []
        self.service_seconds = 0.0
        self._warned_no_audio = False
        self.stats = {"events": 0, "dropped": 0}
        # 边跑边写的草稿日志（见 open_partial_log）。会话正常结束时由 write_log 合成最终文件并删掉它；
        # 服务窗口被直接关掉时进程会被强杀、走不到 finally，这份草稿就是唯一留下来的记录。
        self.partial_log = None
        self.log_path = None   # 会话开始时定下，结束时写到同一个名字

    def _build_pipeline(self, engine, final_engine, language):
        args = self.args
        _, step = asr_settings(args, language, engine.name, getattr(engine, "model_name", ""))
        return OpenUtterancePipeline(
            engine,
            language=language,
            min_speech=args.min_speech,
            min_silence=args.min_silence,
            partial_step=step,
            max_utterance=args.max_utterance,
            call_timeout=args.call_timeout,
            drop_fillers=not args.keep_fillers,
            early_final=args.early_final,
            adaptive_silence=args.adaptive_silence,
            final_engine=final_engine,
        )

    def set_language(self, language):
        """start 消息里的语言：和服务默认语言不同就换引擎重建流水线（必须在开始推音频之前）。

        返回实际生效的语言；不支持的语言回落到服务默认语言。
        """
        if language not in SUPPORTED_LANGUAGES or self.pool is None:
            return self.language
        if language == self.language:
            return language
        engine, final_engine = self.pool.get(language)
        with self.lock:
            early, adaptive = self.pipeline.early_final, self.pipeline.adaptive_silence
            self.pipeline = self._build_pipeline(engine, final_engine, language)
            self.pipeline.early_final, self.pipeline.adaptive_silence = early, adaptive
            self.engine = engine
            self.language = language
            self.address_memory.clear()
            self.address_epoch += 1
            self.address_cutoff = self.audio_seconds
        return language

    # --- 会话生命周期 -----------------------------------------------------

    def apply_options(self, options):
        """扩展在 start 消息里带的实验开关，覆盖命令行默认值，只对本会话生效。

        没带 options（老版本扩展、ws_client_test）就沿用命令行参数。
        """
        if isinstance(options, dict):
            with self.lock:
                if "early_final" in options:
                    self.pipeline.early_final = bool(options["early_final"])
                if "adaptive_silence" in options:
                    value = options["adaptive_silence"]
                    # 只接受 0.05–0.35 之间的数，其余一律视为关闭
                    self.pipeline.adaptive_silence = (
                        float(value) if isinstance(value, (int, float))
                        and not isinstance(value, bool) and 0.05 <= value <= 0.35
                        else None)
                if "address_consistency" in options:
                    enabled = options["address_consistency"] is True
                    if enabled != self.address_consistency:
                        self.address_consistency = enabled
                        self.address_memory.clear()
                        self.address_epoch += 1
                        self.address_cutoff = self.audio_seconds
        return {"early_final": self.pipeline.early_final,
                "adaptive_silence": self.pipeline.adaptive_silence,
                "address_consistency": getattr(self, "address_consistency", False)}

    def reset_address_memory(self):
        with self.lock:
            self.address_memory.clear()
            self.address_epoch += 1
            # Queued sentences from before a reset must not seed the new memory.
            self.address_cutoff = self.audio_seconds

    def consistent_address(self, text, translated, language, epoch, audio_start):
        with self.lock:
            if not self.address_consistency or epoch != self.address_epoch or audio_start < self.address_cutoff:
                return translated, None
            return self.address_memory.apply(text, translated, language)

    def bind_loop(self, loop):
        """把识别线程和 asyncio 事件循环接起来。

        识别线程不能直接 await，所以用 call_soon_threadsafe 投递；
        这比在线程池里阻塞等 queue.get 更省一个线程。
        """
        self.loop = loop
        self.aset_queue = asyncio.Queue(maxsize=MAX_EVENT_QUEUE)

    def start(self):
        if self.running:
            return
        self.started_at = time.perf_counter()
        self.running = True
        self.thread = threading.Thread(target=self._work_loop, daemon=True)
        self.thread.start()
        if self.async_finals:
            self.final_thread = threading.Thread(target=self._final_loop, daemon=True)
            self.final_thread.start()
        if self.translator is not None:
            self.translate_thread = threading.Thread(target=self._translate_loop, daemon=True)
            self.translate_thread.start()

    def stop(self):
        with self.lock:
            self.eof = True
        self.running = False
        if self.thread:
            self.thread.join(timeout=15)
        if self.final_thread:
            # 识别线程退出后，最后几句定稿可能还在队列里，等它们做完再停翻译。
            self.final_queue.put(None)
            self.final_thread.join(timeout=15)
        if self.translate_thread:
            # 让翻译线程把队列里剩下的翻完再退出（定稿文本不该因为停止捕获而丢翻译）。
            self.translate_queue.put(None)
            self.translate_thread.join(timeout=15)

    def feed(self, payload):
        pcm = np.frombuffer(payload, dtype="<i2").astype(np.float32) / 32768.0
        with self.lock:
            if self.configuring:
                # start 还在处理（换语言要换引擎）：先攒着，等新流水线建好再一起交给它。
                # 原来这里会直接 self.start()，用旧语言的流水线开跑——10-01 用户实测，
                # 英语视频的开头被日语 Parakeet 识别了，而且计时起点被重置成负延迟。
                self.pending.append(pcm)
                self.received_samples += pcm.size
                return
        if not self.running:
            # 客户端直接开始推流而没发 start 时也要能工作。
            self.start()
        with self.lock:
            self.pipeline.push_audio(pcm)
            self.received_samples += pcm.size

    def begin_configuring(self):
        with self.lock:
            self.configuring = True

    def finish_configuring(self):
        """start 处理完：把配置期间攒下的音频按顺序交给（可能是新的）流水线。"""
        with self.lock:
            for pcm in self.pending:
                self.pipeline.push_audio(pcm)
            self.pending.clear()
            self.configuring = False

    @property
    def audio_seconds(self):
        return self.received_samples / float(SAMPLE_RATE)

    # --- 识别线程 ---------------------------------------------------------

    def _warn_if_no_audio(self):
        """采集开始了却一直收不到音频时给出明确提示。

        没有这条日志的话，"扩展点了开始捕获但一个字幕都没有"和
        "扩展根本没在推流"在服务端看起来一模一样（都是沉默），
        而这两者的排查方向完全不同：前者要去看 offscreen 的 AudioWorklet，
        后者要去看 popup/background 的取流。
        """
        if self._warned_no_audio:
            return
        if self.received_samples > 0:
            self._warned_no_audio = True
            return
        if time.perf_counter() - self.started_at < 3.0:
            return
        self._warned_no_audio = True
        print(
            "警告：已开始采集，但 3 秒内没有收到任何音频。\n"
            "      请检查 offscreen 的 AudioWorklet 是否在产出采样：\n"
            "      chrome://extensions → 本扩展 → 「检查视图」offscreen.html → Console。",
            flush=True,
        )

    def _work_loop(self):
        while True:
            with self.lock:
                if not self.running and self.pipeline.buffer.empty:
                    break
                job = self.pipeline.next_job(available_until=self.audio_seconds,
                                             eof=self.eof)
            if job is None:
                if self.eof:
                    break
                self._warn_if_no_audio()
                time.sleep(0.02)
                continue

            self._warned_no_audio = True   # 已经有活干，不必再警告

            if self.async_finals and job.kind == "final":
                self.final_queue.put(job)
                continue
            self._run(job)

    def _final_loop(self):
        while True:
            job = self.final_queue.get()
            if job is None:
                break
            self._run(job)

    def _run(self, job):
        began = time.perf_counter()
        events = self.pipeline.run_job(job)
        finished = time.perf_counter()
        with self.stats_lock:
            self.service_seconds += finished - began
        for event in events:
            # finish_wall 与音频位置同一条时间轴（都相对会话开始），
            # 这样 tools/metrics.py 的口径可以直接套用真实会话。
            event.service_seconds = finished - began
            event.finish_wall = finished - self.started_at
            self._emit(event)
            self._maybe_translate(event)

    def _maybe_translate(self, event):
        """只翻译定稿文本。翻译不是关键路径，队列满了就丢，绝不拖累识别。"""
        if self.translator is None or not event.is_final or not event.text:
            return
        try:
            self.translate_queue.put_nowait(
                (event.segment_id, event.revision, event.text, event.audio_start, event.audio_end)
            )
        except queue.Full:
            self.translate_dropped += 1

    def open_partial_log(self, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.partial_log = path.open("w", encoding="utf-8", buffering=1)   # 行缓冲：每行立即落盘

    def close_partial_log(self):
        if self.partial_log is not None:
            self.partial_log.close()
            Path(self.partial_log.name).unlink(missing_ok=True)
            self.partial_log = None

    def _emit(self, event):
        row = event.to_dict()
        self.rows.append(row)
        if self.partial_log is not None:
            try:
                self.partial_log.write(json.dumps(row, ensure_ascii=False) + "\n")
            except (OSError, ValueError):
                pass
        payload = {"type": "event", **row,
                   "latency": round(event.latency, 3),
                   "display_latency": round(event.latency, 3)}
        if self.loop is None:
            return
        self.loop.call_soon_threadsafe(self._offer, payload)

    # --- 翻译线程 ---------------------------------------------------------

    def _translate_loop(self):
        """独立线程翻译定稿文本。

        刻意放在识别线程之外：翻译不能阻塞识别，也不能把延迟加到原文上。
        结果作为**同一个 segment_id 的更高 revision** 回传，
        渲染端只按 revision 单调前进，所以原文先显示、译文稍后补上，不会闪烁
        （这条路径在 tests/test_extension_logic.js 里有专门的用例）。
        """
        while True:
            item = self.translate_queue.get()
            if item is None:
                break
            segment_id, revision, text, audio_start, audio_end = item

            # 上下文在**出队时**才取，而不是入队时。
            # 队列是 FIFO 且单线程，所以处理到这一条时，前面几条的译文已经写进 history——
            # 这样上文里的译文才是最新的，用词一致性才有意义。
            context = self.history[-self.args.translate_context:] if self.args.translate_context > 0 else None
            with self.lock:
                address_epoch = self.address_epoch
                language = self.language

            try:
                began = time.perf_counter()
                translated = self.translator.translate(
                    text, source=language, target="zh",
                    context=context, style=self.args.translate_style)
                elapsed = time.perf_counter() - began
            except Exception as exc:
                print(f"翻译失败：{type(exc).__name__}: {exc}", flush=True)
                continue
            if not translated:
                continue

            address_change = None
            address_seconds = 0.0
            if self.address_consistency:
                began_address = time.perf_counter()
                translated, address_change = self.consistent_address(
                    text, translated, language, address_epoch, audio_start)
                address_seconds = time.perf_counter() - began_address

            # 记进 history 供后续句子做上文（原文 + 译文，便于统一用词）
            self.history.append({"original": text, "translation": translated})
            if len(self.history) > 8:
                del self.history[:-8]

            self.translated += 1
            event = SubtitleEvent(
                segment_id=segment_id,
                revision=revision + 1,
                text=text,
                is_final=True,
                engine=self.translator.name,
                audio_start=audio_start,
                audio_end=audio_end,
                translation=translated,
                service_seconds=elapsed,
                finish_wall=time.perf_counter() - self.started_at,
                detail={"translation": True, "translate_seconds": round(elapsed, 3),
                        **({"address_change": address_change} if address_change else {}),
                        **({"address_seconds": round(address_seconds, 6)} if address_seconds else {})},
            )
            with self.stats_lock:
                self.service_seconds += elapsed
            self._emit(event)

    def _offer(self, payload):
        """在事件循环线程里执行：满了就丢最旧的，永不排队。"""
        if self.aset_queue.full():
            try:
                self.aset_queue.get_nowait()
                self.dropped += 1
            except asyncio.QueueEmpty:
                pass
        try:
            self.aset_queue.put_nowait(payload)
        except asyncio.QueueFull:
            pass

    # --- 结果 -------------------------------------------------------------

    def summary(self):
        views = latency_views(self.rows)
        audio = self.audio_seconds
        return {
            "audio_seconds": round(audio, 2),
            # 与离线台架同口径，方便直接对比（< 1 才代表 CPU 跟得上）
            "cpu_ratio": round(self.service_seconds / audio, 3) if audio > 0 else None,
            "total_service_seconds": round(self.service_seconds, 3),
            "events": len(self.rows),
            "segments": len({row["segment_id"] for row in self.rows}),
            "dropped_events": self.dropped,
            "calls": self.pipeline.stats.get("calls"),
            "partials": self.pipeline.stats.get("partials"),
            "finals": self.pipeline.stats.get("finals"),
            "gated_skips": self.pipeline.stats.get("skipped"),
            "timeouts": self.pipeline.stats.get("timeouts"),
            "translated": self.translated,
            "translate_dropped": self.translate_dropped,
            "first_show_p95": round(percentile(views["first_show"], 0.95), 3),
            "latency_p50": round(percentile(views["mean"], 0.50), 3),
            "latency_p95": round(percentile(views["mean"], 0.95), 3),
            "latency_worst_max": round(max(views["worst"]), 3) if views["worst"] else 0.0,
            "final_p95": round(percentile(views["final_stale"], 0.95), 3),
        }


LOG_KEEP = 20


def session_log_path(args):
    """--log 给的是固定文件；--log-dir 给的是目录，每次会话按开始时间命名一份。"""
    if args.log:
        return Path(args.log)
    if args.log_dir:
        return Path(args.log_dir) / time.strftime("live_%Y%m%d_%H%M%S.jsonl")
    return None


def prune_logs(directory, keep=LOG_KEEP):
    """--log-dir 下只保留最近 keep 份，更早的删掉（文件名带时间，按名字排序即按时间）。"""
    logs = sorted(Path(directory).glob("live_*.jsonl"))
    for old in logs[:-keep]:
        old.unlink(missing_ok=True)


def write_log(path, args, session):
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        # 同一个服务里的多次会话各写一份，别互相覆盖：live.jsonl → live_2.jsonl …
        n = 2
        while (candidate := path.with_name(f"{path.stem}_{n}{path.suffix}")).exists():
            n += 1
        path = candidate
    header = {
        "type": "header",
        "source": "live-session",
        "pipeline": "open_utterance",
        "engine": args.engine,
        "model": args.model,
        "final_engine": args.final_engine,
        "final_model": args.final_model,
        "early_final": session.pipeline.early_final,
        "adaptive_silence": session.pipeline.adaptive_silence,
        **session.summary(),
    }
    with path.open("w", encoding="utf-8") as handle:
        handle.write(json.dumps(header, ensure_ascii=False) + "\n")
        for row in session.rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    print(f"会话日志已写入 {path}", flush=True)


class Activity:
    """闲置退出用：记录正在采集的会话数和最后一次采集结束的时间。

    只有真正推过音频的会话才算"活动"——popup 每 2 秒一次的状态探测不能让服务永远不退出。
    """

    def __init__(self):
        self.capturing = 0
        self.last_active = time.monotonic()

    def begin(self):
        self.capturing += 1
        self.last_active = time.monotonic()

    def end(self):
        self.capturing = max(0, self.capturing - 1)
        self.last_active = time.monotonic()

    def idle_seconds(self):
        return 0.0 if self.capturing else time.monotonic() - self.last_active


ACTIVITY = Activity()


async def handle(websocket, args, engine, translator=None, final_engine=None, pool=None):
    session = Session(args, engine, translator, final_engine, pool)
    capturing = False
    session.bind_loop(asyncio.get_running_loop())
    print(f"客户端已连接，engine={args.engine} model={args.model}", flush=True)
    sender = asyncio.create_task(_sender(websocket, session))
    try:
        async for message in websocket:
            if isinstance(message, (bytes, bytearray)):
                session.feed(message)
                continue
            try:
                control = json.loads(message)
            except (TypeError, ValueError):
                continue
            kind = control.get("type")
            if kind == "start":
                # 换语言可能要现场加载引擎（几秒），放到线程里，别卡住事件循环；
                # 这期间扩展已经在推音频了，feed() 会先把它们攒起来。
                session.begin_configuring()
                try:
                    language = await asyncio.to_thread(session.set_language,
                                                       control.get("language", args.language))
                    applied = session.apply_options(control.get("options"))
                finally:
                    session.finish_configuring()
                if session.log_path is None:
                    session.log_path = session_log_path(args)
                    if session.log_path is not None:
                        session.open_partial_log(
                            session.log_path.with_name(session.log_path.name + ".partial"))
                session.start()
                if not capturing:
                    capturing = True
                    ACTIVITY.begin()
                # 这条日志把"扩展真的开始推流了"和"只是 popup 在探测"区分开，
                # 排查时非常关键（探测只发 ping，不会走到这里）。
                print(f"采集开始：language={language} engine={session.engine.name} "
                      f"early_final={applied['early_final']} "
                      f"adaptive_silence={applied['adaptive_silence']}", flush=True)
                await websocket.send(json.dumps({
                    "type": "status", "state": "listening",
                    "engine": session.engine.name, "model": getattr(session.engine, "model_name", args.model),
                    "language": language,
                    "address_consistency": session.address_consistency,
                    "sample_rate": SAMPLE_RATE,
                }, ensure_ascii=False))
            elif kind == "ping":
                # 必须带上 engine/model：popup 的"检查本地服务"就是靠这条回复显示引擎信息的
                # （tests/test_probe_service.js 会检查这一点——第一版漏了，popup 显示成"（? / ?）"）。
                await websocket.send(json.dumps(
                    {"type": "status", "state": "ok",
                     "engine": args.engine, "model": args.model,
                     "language": args.language,
                     "translate": translator is not None,
                     "translate_engine": args.translate_engine if translator is not None else None,
                     "final_engine": args.final_engine, "final_model": args.final_model,
                     "en_engine": args.en_engine, "en_model": args.en_model,
                     "idle_exit": args.idle_exit,
                     "capturing_sessions": ACTIVITY.capturing,
                     "address_consistency_supported": True,
                     "sample_rate": SAMPLE_RATE,
                     **session.summary()},
                    ensure_ascii=False))
            elif kind == "reset_address_memory":
                session.reset_address_memory()
                await websocket.send(json.dumps({"type": "status", "state": "address_memory_reset"}))
            elif kind == "stop":
                break
    except Exception as exc:  # 客户端断开等
        print(f"会话异常：{type(exc).__name__}: {exc}", flush=True)
    finally:
        if capturing:
            ACTIVITY.end()
        session.stop()
        sender.cancel()
        summary = session.summary()
        print(f"会话结束：{json.dumps(summary, ensure_ascii=False)}", flush=True)
        # popup 的"检查本地服务"也是一次连接：没收到音频就别写日志，
        # 否则它会覆盖掉刚才那次真实会话（09-29 用户实测的日志就是这样丢的）。
        log_path = session.log_path or session_log_path(args)
        if log_path is not None and session.received_samples > 0:
            write_log(log_path, args, session)
            if not args.log:
                prune_logs(args.log_dir)
        session.close_partial_log()


async def _sender(websocket, session):
    """把识别线程投递的事件发回客户端。"""
    while True:
        payload = await session.aset_queue.get()
        try:
            await websocket.send(json.dumps(payload, ensure_ascii=False))
        except Exception:
            return


async def main_async(args):
    import websockets.asyncio.server as ws_server

    print(f"加载 {args.engine} / {args.model} ...", flush=True)
    began = time.perf_counter()
    threads, _ = asr_settings(args, args.language, args.engine, args.model)
    engine = create_engine(args.engine, args.model, language=args.language,
                           threads=threads)
    print(f"引擎就绪（{time.perf_counter() - began:.2f}s，含预热）", flush=True)

    final_engine = None
    if args.final_engine:
        print(f"加载定稿引擎 {args.final_engine} / {args.final_model or '(默认)'} ...", flush=True)
        began = time.perf_counter()
        final_engine = create_engine(args.final_engine, args.final_model, language=args.language,
                                     threads=args.final_threads)
        print(f"定稿引擎就绪（{time.perf_counter() - began:.2f}s，含预热）", flush=True)

    translator = None
    if args.translate:
        from .translate.factory import create_translator
        print(f"加载翻译模型 {args.translate_engine} / {args.translate_model or '(默认)'} ...", flush=True)
        began = time.perf_counter()
        translator = create_translator(args.translate_engine, args.translate_model,
                                       threads=args.translate_threads,
                                       backend=args.translate_backend)
        print(f"翻译就绪（{time.perf_counter() - began:.2f}s，含预热）", flush=True)

    pool = EnginePool(args, engine, final_engine)
    # 第二种语言在后台预加载：首次英语会话如果现场加载 Parakeet（约 18 秒），这段时间推来的音频
    # 会排在 start 处理后面，开头十几秒的字幕全部延后。预加载期间来了英语会话，get() 会等同一把锁。
    other = "en" if args.language != "en" else None
    if other and not args.no_preload:
        threading.Thread(target=pool.get, args=(other,), daemon=True).start()

    async def handler(websocket):
        await handle(websocket, args, engine, translator, final_engine, pool)

    async with ws_server.serve(handler, args.host, args.port,
                               max_size=None, ping_interval=20):
        print(f"流式字幕服务已启动：ws://{args.host}:{args.port}", flush=True)
        print("只监听回环地址，不接收局域网连接。", flush=True)
        if args.idle_exit and args.idle_exit > 0:
            span = (f"{args.idle_exit / 60:.0f} 分钟" if args.idle_exit >= 60
                    else f"{args.idle_exit:.0f} 秒")
            print(f"闲置 {span}（没有任何采集）后自动退出。", flush=True)
            while ACTIVITY.idle_seconds() < args.idle_exit:
                await asyncio.sleep(min(5, args.idle_exit / 4))
            print(f"已闲置 {span}，自动退出以释放内存。", flush=True)
            return
        await asyncio.Future()


def main():
    parser = argparse.ArgumentParser(description="流式字幕服务（PCM over WebSocket）")
    parser.add_argument("--engine", default="sensevoice", choices=ENGINES)
    parser.add_argument("--model", default=None)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument("--language", default="ja")
    parser.add_argument("--threads", type=int, default=None)
    parser.add_argument("--en-threads", type=int, default=None,
                        help="覆盖英语识别线程数（英语 Parakeet 默认最多 3 线程）")
    parser.add_argument("--no-preload", action="store_true",
                        help="不在启动后预加载英语引擎（省约 600MB 内存；首次英语会话会现场加载）")
    parser.add_argument("--en-engine", default="sherpa", choices=ENGINES,
                        help="英语会话的识别引擎（服务默认语言不是英语时，首次英语会话再加载）")
    parser.add_argument("--en-model", default="parakeet-en-unified",
                        help="英语会话的模型；10-01 评测 WER：parakeet-en-unified 0.073，sensevoice-2024 0.102")
    parser.add_argument("--final-engine", default=None, choices=ENGINES,
                        help="定稿改用另一个引擎（草稿仍用 --engine），例如 sherpa")
    parser.add_argument("--final-model", default=None, help="--final-engine 的模型，例如 parakeet-ja")
    parser.add_argument("--final-threads", type=int, default=None)
    parser.add_argument("--idle-exit", type=float, default=0, metavar="SECONDS",
                        help="连续这么多秒没有任何采集就自动退出（0 = 不退出；扩展一键启动时默认 1800）")
    parser.add_argument("--async-finals", action="store_true",
                        help="实验：定稿放进独立线程（09-30 实测因抢 CPU 反而更慢，默认关闭）")
    parser.add_argument("--min-speech", type=float, default=0.4)
    parser.add_argument("--min-silence", type=float, default=0.35)
    parser.add_argument("--partial-step", type=float, default=None,
                        help="覆盖所有语言的草稿间隔（英语 Parakeet 默认 0.75，其他 0.5 秒）")
    parser.add_argument("--en-partial-step", type=float, default=None,
                        help="仅覆盖英语草稿刷新间隔")
    parser.add_argument("--max-utterance", type=float, default=10.0)
    parser.add_argument("--call-timeout", type=float, default=None)
    parser.add_argument("--keep-fillers", action="store_true",
                        help="保留只有语气词/笑声的整句（默认丢弃，见 eval/README.md）")
    parser.add_argument("--early-final", action="store_true",
                        help="实验：两句被 VAD 粘在一起时，前半句提前定稿并翻译（默认关闭）")
    parser.add_argument("--adaptive-silence", type=float, default=None, metavar="SECONDS",
                        help="实验：partial 以句末助词/问号结尾时，静音达到这么多秒就定稿"
                             "（例如 0.2；默认关闭，仍按 --min-silence）")
    parser.add_argument("--translate", action="store_true",
                        help="启用本地翻译")
    parser.add_argument("--translate-engine", default="hymt", choices=("hymt", "instruct", "nllb"),
                        help="hymt=Hy-MT2-1.8B（默认，09-29 实测）；instruct=Qwen2.5-0.5B（备选）；"
                             "nllb 实测不可用，模型已删除，仅保留代码")
    parser.add_argument("--translate-model", default=None,
                        help="翻译模型目录名，默认取 factory.DEFAULT_MODEL")
    parser.add_argument("--translate-backend", default="ct2", choices=("ct2", "torch"),
                        help="instruct 后端：ct2（int8，快数倍）或 torch（fp32）")
    parser.add_argument("--translate-context", type=int, default=0,
                        help="翻译时带上前几句作为上文（**默认 0 = 关闭**）。"
                             "离线测试里带上文更好，但用户真实使用实测更差——真实输入是"
                             "含误识别的 ASR 输出，上下文会把错误传播下去。"
                             "详见 docs/BENCHMARK_RESULTS.md 13.6")
    parser.add_argument("--translate-style", default="plain",
                        choices=("plain", "instruction", "completion"),
                        help="提示词风格；plain 是用户实测认可的基线")
    parser.add_argument("--translate-threads", type=int, default=4,
                        help="翻译线程数（指令模型用 torch，这个值直接影响其速度）")
    parser.add_argument("--log", type=Path, default=None,
                        help="把本次会话落成台架格式的 JSONL，可直接用 tools/metrics.py 分析")
    parser.add_argument("--log-dir", type=Path, default=None,
                        help=f"每次会话各写一份 live_<时间>.jsonl 到这个目录，只保留最近 {LOG_KEEP} 份"
                             "（--log 优先）")
    args = parser.parse_args()
    if args.model is None:
        from .asr.factory import DEFAULT_MODEL
        args.model = DEFAULT_MODEL[args.engine]

    try:
        asyncio.run(main_async(args))
    except KeyboardInterrupt:
        print("已停止", flush=True)


if __name__ == "__main__":
    main()
