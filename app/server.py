"""流式字幕服务：PCM 流进，partial / final 事件流出。

这是 M3 的核心，替换原来"每 2 秒一个 HTTP 请求 + 服务端全局锁串行"的做法。

## 协议

客户端 → 服务端
  - 文本帧（控制）：`{"type":"start","language":"ja"}` / `{"type":"stop"}` / `{"type":"ping"}`
  - 二进制帧：裸 PCM，**s16le / 16 kHz / 单声道**，连续追加。
    客户端不做任何分段决策（ARCHITECTURE_REVIEW.md 第 4 节不变式 1）。

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
import queue
import threading
import time
from pathlib import Path

import numpy as np

from .asr.factory import ENGINES, create_engine
from .events import SubtitleEvent, latency_views, percentile
from .pipelines.open_utterance import OpenUtterancePipeline

SAMPLE_RATE = 16000
MAX_EVENT_QUEUE = 64
MAX_TRANSLATE_QUEUE = 16


class Session:
    def __init__(self, args, engine, translator=None):
        self.args = args
        self.engine = engine
        self.translator = translator
        self.pipeline = OpenUtterancePipeline(
            engine,
            language=args.language,
            min_speech=args.min_speech,
            min_silence=args.min_silence,
            partial_step=args.partial_step,
            max_utterance=args.max_utterance,
            call_timeout=args.call_timeout,
        )
        self.lock = threading.Lock()
        self.translate_queue = queue.Queue(maxsize=MAX_TRANSLATE_QUEUE)
        self.translate_thread = None
        # 已定稿并已翻译的句子，供后续句子当上文用（原文 + 译文）
        self.history = []
        self.loop = None
        self.aset_queue = None
        self.dropped = 0
        self.translated = 0
        self.translate_dropped = 0
        self.started_at = time.perf_counter()
        self.received_samples = 0
        self.thread = None
        self.running = False
        self.eof = False
        self.rows = []
        self.service_seconds = 0.0
        self._warned_no_audio = False
        self.stats = {"events": 0, "dropped": 0}

    # --- 会话生命周期 -----------------------------------------------------

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
        if self.translator is not None:
            self.translate_thread = threading.Thread(target=self._translate_loop, daemon=True)
            self.translate_thread.start()

    def stop(self):
        with self.lock:
            self.eof = True
        self.running = False
        if self.thread:
            self.thread.join(timeout=15)
        if self.translate_thread:
            # 让翻译线程把队列里剩下的翻完再退出（定稿文本不该因为停止捕获而丢翻译）。
            self.translate_queue.put(None)
            self.translate_thread.join(timeout=15)

    def feed(self, payload):
        if not self.running:
            # 客户端直接开始推流而没发 start 时也要能工作。
            self.start()
        pcm = np.frombuffer(payload, dtype="<i2").astype(np.float32) / 32768.0
        with self.lock:
            self.pipeline.push_audio(pcm)
            self.received_samples += pcm.size

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

            began = time.perf_counter()
            events = self.pipeline.run_job(job)
            finished = time.perf_counter()
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

    def _emit(self, event):
        row = event.to_dict()
        self.rows.append(row)
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

            try:
                began = time.perf_counter()
                translated = self.translator.translate(
                    text, source=self.args.language, target="zh", context=context)
                elapsed = time.perf_counter() - began
            except Exception as exc:
                print(f"翻译失败：{type(exc).__name__}: {exc}", flush=True)
                continue
            if not translated:
                continue

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
                detail={"translation": True, "translate_seconds": round(elapsed, 3)},
            )
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


def write_log(path, args, session):
    path.parent.mkdir(parents=True, exist_ok=True)
    header = {
        "type": "header",
        "source": "live-session",
        "pipeline": "open_utterance",
        "engine": args.engine,
        "model": args.model,
        **session.summary(),
    }
    with path.open("w", encoding="utf-8") as handle:
        handle.write(json.dumps(header, ensure_ascii=False) + "\n")
        for row in session.rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    print(f"会话日志已写入 {path}", flush=True)


async def handle(websocket, args, engine, translator=None):
    session = Session(args, engine, translator)
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
                session.start()
                # 这条日志把"扩展真的开始推流了"和"只是 popup 在探测"区分开，
                # 排查时非常关键（探测只发 ping，不会走到这里）。
                print(f"采集开始：language={control.get('language', args.language)}", flush=True)
                await websocket.send(json.dumps({
                    "type": "status", "state": "listening",
                    "engine": args.engine, "model": args.model,
                    "language": control.get("language", args.language),
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
                     "sample_rate": SAMPLE_RATE,
                     **session.summary()},
                    ensure_ascii=False))
            elif kind == "stop":
                break
    except Exception as exc:  # 客户端断开等
        print(f"会话异常：{type(exc).__name__}: {exc}", flush=True)
    finally:
        session.stop()
        sender.cancel()
        summary = session.summary()
        print(f"会话结束：{json.dumps(summary, ensure_ascii=False)}", flush=True)
        if args.log:
            write_log(Path(args.log), args, session)


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
    engine = create_engine(args.engine, args.model, language=args.language,
                           threads=args.threads)
    print(f"引擎就绪（{time.perf_counter() - began:.2f}s，含预热）", flush=True)

    translator = None
    if args.translate:
        from .translate.factory import create_translator
        print(f"加载翻译模型 {args.translate_engine} / {args.translate_model or '(默认)'} ...", flush=True)
        began = time.perf_counter()
        translator = create_translator(args.translate_engine, args.translate_model,
                                       threads=args.translate_threads,
                                       backend=args.translate_backend)
        print(f"翻译就绪（{time.perf_counter() - began:.2f}s，含预热）", flush=True)

    async def handler(websocket):
        await handle(websocket, args, engine, translator)

    async with ws_server.serve(handler, args.host, args.port,
                               max_size=None, ping_interval=20):
        print(f"流式字幕服务已启动：ws://{args.host}:{args.port}", flush=True)
        print("只监听回环地址，不接收局域网连接。", flush=True)
        await asyncio.Future()


def main():
    parser = argparse.ArgumentParser(description="流式字幕服务（PCM over WebSocket）")
    parser.add_argument("--engine", default="sensevoice", choices=ENGINES)
    parser.add_argument("--model", default=None)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument("--language", default="ja")
    parser.add_argument("--threads", type=int, default=None)
    parser.add_argument("--min-speech", type=float, default=0.4)
    parser.add_argument("--min-silence", type=float, default=0.35)
    parser.add_argument("--partial-step", type=float, default=0.5)
    parser.add_argument("--max-utterance", type=float, default=10.0)
    parser.add_argument("--call-timeout", type=float, default=None)
    parser.add_argument("--translate", action="store_true",
                        help="启用本地翻译")
    parser.add_argument("--translate-engine", default="instruct", choices=("instruct", "nllb"),
                        help="instruct=本地小指令模型（当前可用）；nllb 实测不可用，仅保留")
    parser.add_argument("--translate-model", default=None,
                        help="翻译模型目录名，默认取 factory.DEFAULT_MODEL")
    parser.add_argument("--translate-backend", default="ct2", choices=("ct2", "torch"),
                        help="instruct 后端：ct2（int8，快数倍）或 torch（fp32）")
    parser.add_argument("--translate-context", type=int, default=2,
                        help="翻译时带上前几句作为上文（0 = 关闭）。"
                             "孤立翻译是字幕质量最大的杀手：日语省略主语，"
                             "「にしても」「お兄さん」这类表达要靠上文才能定意思")
    parser.add_argument("--translate-threads", type=int, default=4,
                        help="翻译线程数（指令模型用 torch，这个值直接影响其速度）")
    parser.add_argument("--log", type=Path, default=None,
                        help="把本次会话落成台架格式的 JSONL，可直接用 tools/metrics.py 分析")
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
