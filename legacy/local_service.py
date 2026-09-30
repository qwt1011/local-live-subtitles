"""Local-only Whisper service for the browser extension prototype.

M0 收尾版本。相对原型的改动（每条都有实测依据，详见 docs/ARCHITECTURE_REVIEW.md）：

1. VAD 语音时长门控：VAD 后语音不足 `--min-speech` 秒的请求直接返回，不调用模型。
   实测中"最贵且输出幻觉"的调用正是那些 VAD 后只剩 0.66 秒语音、或 0 秒语音的块。
2. temperature 阶梯从 [0.0,0.2,...,1.0] 收窄到 [0.0,0.4]。默认阶梯会在低置信块上重解码整段，
   实测把同一个 3 秒块从 2.3 秒推到 6.6 秒、从 2.6 秒推到 7.2 秒。
3. 用 repetition_penalty / no_repeat_ngram_size 显式压制复读，而不是靠温度阶梯的副作用来压。
4. 内存流解码优先，避免 Windows 上 ffmpeg 持有临时文件造成的锁竞争；失败再退回临时文件。
5. 修掉 `finally` 里的 NameError：原来在 finally 中遍历 `segments`，一旦 transcribe 抛异常
   `segments` 未绑定，finally 会抛 NameError 覆盖真实异常。现在生成器在 try 内消费完。
6. 启动预热：加载模型后立刻跑一次静音推理并预热 Silero VAD，避免把惰性初始化成本
   算到用户的第一个请求上（实测首次真实调用出现过 8.4 秒的异常值）。
7. 翻译移出模型锁，不再与识别串行。

注意：本文件仍然是"固定分块 + 每块一次调用"的基线结构，延迟下限仍由块长度决定。
流式内核见 M2。
"""

import argparse
import io
import json
import os
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import numpy as np

from faster_whisper import WhisperModel
from faster_whisper.audio import decode_audio
from faster_whisper.vad import VadOptions, get_speech_timestamps

try:
    import argostranslate.translate as argos_translate
except ImportError:
    argos_translate = None


# 识别参数集中在这里，便于回放台架引用同一份配置，避免"服务里一套、台架里另一套"。
ASR_PARAMS = {
    "beam_size": 1,
    "temperature": [0.0, 0.4],
    "repetition_penalty": 1.1,
    "no_repeat_ngram_size": 3,
    "vad_filter": True,
    # 隔离的短块没有可信的上文，开启它只会让模型延续上一块的幻觉。
    # 流式内核（M2）会用"已提交文本"自己构造 prompt，而不是依赖这个开关。
    "condition_on_previous_text": False,
}

VAD_OPTIONS = VadOptions(min_silence_duration_ms=350, speech_pad_ms=100)


class ServiceState:
    """服务级状态。不再用 Handler 的类属性，避免测试和多实例时的全局污染。"""

    def __init__(self, model_name, language, translate_enabled, min_speech):
        self.model_name = model_name
        self.language = language
        self.translate_enabled = translate_enabled
        self.min_speech = min_speech
        self.model = None
        self.warmed_up = False
        self.lock = threading.Lock()


def decode_request(body, content_type):
    """把上传的音频解码成 16 kHz 单声道 float32。"""
    try:
        return decode_audio(io.BytesIO(body), sampling_rate=16000)
    except Exception:
        # 少数容器 PyAV 无法从内存流解析，退回临时文件；用完立刻删除。
        suffix = ".webm" if "webm" in content_type else ".wav"
        fd, temp_name = tempfile.mkstemp(suffix=suffix)
        try:
            with os.fdopen(fd, "wb") as temp:
                temp.write(body)
            return decode_audio(temp_name, sampling_rate=16000)
        finally:
            try:
                os.unlink(temp_name)
            except OSError:
                pass


def speech_seconds(pcm):
    """VAD 判定出的语音总时长（秒）。"""
    stamps = get_speech_timestamps(pcm, VAD_OPTIONS, sampling_rate=16000)
    return sum(stamp["end"] - stamp["start"] for stamp in stamps) / 16000.0


def translate_text(text, detected_language):
    """翻译层。仍然保留 Argos，但已明确 ja 走 en 中转是临时方案，M5 换成直连 ja->zh。"""
    if argos_translate is None:
        return None, "Argos Translate is not installed", None
    try:
        if detected_language == "ja":
            english = argos_translate.translate(text, "ja", "en")
            return argos_translate.translate(english, "en", "zh"), None, "ja-en-zh"
        return argos_translate.translate(text, "en", "zh"), None, "en-zh"
    except Exception as exc:  # 翻译失败不应该影响原文返回
        return None, str(exc), None


class Handler(BaseHTTPRequestHandler):
    state = None

    def do_OPTIONS(self):  # noqa: N802
        self.send_response(204)
        self.send_cors_headers()
        self.end_headers()

    def do_GET(self):  # noqa: N802
        if urlparse(self.path).path == "/health":
            state = self.state
            self.send_json({
                "ok": True,
                "model": state.model_name,
                "warmed_up": state.warmed_up,
                "language": state.language,
                "min_speech": state.min_speech,
                "translate": state.translate_enabled,
            })
            return
        self.send_error(404)

    def do_POST(self):  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path != "/transcribe":
            self.send_error(404)
            return

        state = self.state
        started = time.perf_counter()
        timings = {}
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if size <= 0 or size > 20 * 1024 * 1024:
                raise ValueError("audio body must be between 1 byte and 20 MB")
            body = self.rfile.read(size)
            content_type = self.headers.get("Content-Type", "")
            print(f"Transcribe request: {size} bytes, type={content_type}", flush=True)

            language = parse_qs(parsed.query).get("language", [state.language])[0]

            # 解码与 VAD 不碰共享模型，放在锁外，避免所有请求排在同一条队列上。
            mark = time.perf_counter()
            pcm = decode_request(body, content_type)
            timings["decode_seconds"] = round(time.perf_counter() - mark, 3)

            mark = time.perf_counter()
            speech = speech_seconds(pcm)
            timings["vad_seconds"] = round(time.perf_counter() - mark, 3)
            timings["audio_seconds"] = round(len(pcm) / 16000.0, 3)
            timings["speech_seconds"] = round(speech, 3)

            # 关键门控：语音太少的块不值得花一次调用，而且实测这类块最容易产出幻觉或空文本。
            if speech < state.min_speech:
                result = {
                    "language": language,
                    "segments": [],
                    "skipped": "insufficient_speech",
                    "processing_seconds": round(time.perf_counter() - started, 3),
                    **timings,
                }
                print(f"Transcribe skip: speech={speech:.2f}s < {state.min_speech}s", flush=True)
                self.send_json(result)
                return

            with state.lock:
                mark = time.perf_counter()
                segments, info = state.model.transcribe(pcm, language=language, **ASR_PARAMS)
                # 生成器必须在锁内、在返回前消费完：transcribe 是惰性的，
                # 提前离开作用域会把真实异常暴露成 NameError。
                rows = [
                    {"start": round(s.start, 3), "end": round(s.end, 3), "text": s.text.strip()}
                    for s in segments
                ]
                timings["asr_seconds"] = round(time.perf_counter() - mark, 3)

            result = {"language": info.language, "segments": rows}

            if state.translate_enabled:
                mark = time.perf_counter()
                text = " ".join(row["text"] for row in rows)
                translation, error, via = translate_text(text, info.language)
                if translation is not None:
                    result["translation"] = translation
                    result["translation_via"] = via
                if error:
                    result["translation_error"] = error
                timings["translate_seconds"] = round(time.perf_counter() - mark, 3)

            result["processing_seconds"] = round(time.perf_counter() - started, 3)
            result.update(timings)
            self.send_json(result)
            print(
                f"Transcribe response: {len(rows)} segment(s), "
                f"asr={timings.get('asr_seconds')}s speech={timings['speech_seconds']}s "
                f"total={result['processing_seconds']}s text={rows}",
                flush=True,
            )
        except Exception as exc:  # 返回诊断信息，但不让服务退出
            print(f"Transcribe error: {type(exc).__name__}: {exc}", flush=True)
            self.send_json({"error": f"{type(exc).__name__}: {exc}"}, status=400)

    def send_cors_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def send_json(self, value, status=200):
        payload = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_cors_headers()
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *_):
        return


def warm_up(state):
    """在开始监听之前把惰性初始化成本付掉，并预热 Silero VAD 的 ONNX 会话。"""
    print("Warming up (model + VAD)...", flush=True)
    started = time.perf_counter()
    silence = np.zeros(16000, dtype=np.float32)
    segments, _ = state.model.transcribe(silence, language=state.language, **ASR_PARAMS)
    list(segments)
    get_speech_timestamps(silence, VAD_OPTIONS, sampling_rate=16000)
    state.warmed_up = True
    print(f"Warm-up done in {time.perf_counter() - started:.2f}s", flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default="base", choices=("tiny", "base", "small"))
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--language", default="ja", choices=("ja", "en"))
    parser.add_argument("--translate", action="store_true", help="Use installed Argos Translate language packages")
    parser.add_argument(
        "--min-speech", type=float, default=0.4,
        help="VAD 后语音短于该秒数的请求直接跳过识别（默认 0.4，设 0 关闭门控）",
    )
    parser.add_argument("--no-warmup", action="store_true", help="跳过启动预热（仅用于对比实验）")
    args = parser.parse_args()

    state = ServiceState(args.model, args.language, args.translate, args.min_speech)
    print(f"Loading {args.model} on CPU INT8...", flush=True)
    started = time.perf_counter()
    state.model = WhisperModel(args.model, device="cpu", compute_type="int8")
    print(f"Model loaded in {time.perf_counter() - started:.2f}s", flush=True)
    if not args.no_warmup:
        warm_up(state)

    Handler.state = state
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"Listening on http://127.0.0.1:{args.port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
