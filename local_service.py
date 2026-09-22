"""Local-only Whisper service for the browser extension prototype."""

import argparse
import json
import threading
import tempfile
import os
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from faster_whisper import WhisperModel

try:
    import argostranslate.translate as argos_translate
except ImportError:
    argos_translate = None


class Handler(BaseHTTPRequestHandler):
    model = None
    model_name = "unknown"
    translation_enabled = False
    language = "ja"
    lock = threading.Lock()

    def do_OPTIONS(self):  # noqa: N802
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Max-Age", "600")
        self.end_headers()

    def do_GET(self):  # noqa: N802
        if self.path == "/health":
            self.send_json({"ok": True, "model": self.model_name})
            return
        self.send_error(404)

    def do_POST(self):  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path != "/transcribe":
            self.send_error(404)
            return
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if size <= 0 or size > 20 * 1024 * 1024:
                raise ValueError("audio body must be between 1 byte and 20 MB")
            audio = self.rfile.read(size)
            print(f"Transcribe request: {size} bytes, type={self.headers.get('Content-Type', '')}", flush=True)
            request_started = time.perf_counter()
            options = parse_qs(parsed.query)
            language = options.get("language", [self.language])[0]
            with self.lock:
                content_type = self.headers.get("Content-Type", "")
                suffix = ".webm" if "webm" in content_type else ".wav"
                fd, temp_name = tempfile.mkstemp(suffix=suffix)
                try:
                    with os.fdopen(fd, "wb") as temp:
                        temp.write(audio)
                    segments, info = self.model.transcribe(
                        temp_name, language=language, beam_size=1,
                        vad_filter=True, condition_on_previous_text=True,
                    )
                finally:
                    try:
                        os.unlink(temp_name)
                    except FileNotFoundError:
                        pass
                    rows = [
                    {"start": segment.start, "end": segment.end, "text": segment.text.strip()}
                    for segment in segments
                    ]
            result = {"language": info.language, "segments": rows}
            if self.translation_enabled:
                if argos_translate is None:
                    result["translation_error"] = "Argos Translate is not installed"
                else:
                    try:
                        text = " ".join(row["text"] for row in rows)
                        if info.language == "ja":
                            english = argos_translate.translate(text, "ja", "en")
                            result["translation"] = argos_translate.translate(english, "en", "zh")
                            result["translation_via"] = "ja-en-zh"
                        else:
                            result["translation"] = argos_translate.translate(text, "en", "zh")
                            result["translation_via"] = "en-zh"
                    except Exception as exc:
                        result["translation_error"] = str(exc)
            result["processing_seconds"] = round(time.perf_counter() - request_started, 3)
            self.send_json(result)
            print(f"Transcribe response: {len(rows)} segment(s), processing={result['processing_seconds']}s, text={rows}", flush=True)
        except Exception as exc:  # return diagnostic without killing the service
            print(f"Transcribe error: {exc}", flush=True)
            self.send_json({"error": str(exc)}, status=400)

    def send_json(self, value, status=200):
        payload = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *_):
        return


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default="base", choices=("tiny", "base", "small"))
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--language", default="ja", choices=("ja", "en"))
    parser.add_argument("--translate", action="store_true", help="Use installed Argos Translate language packages")
    args = parser.parse_args()
    print(f"Loading {args.model} on CPU INT8...")
    Handler.model = WhisperModel(args.model, device="cpu", compute_type="int8")
    Handler.model_name = args.model
    Handler.language = args.language
    Handler.translation_enabled = args.translate
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"Listening on http://127.0.0.1:{args.port}")
    server.serve_forever()


if __name__ == "__main__":
    main()
