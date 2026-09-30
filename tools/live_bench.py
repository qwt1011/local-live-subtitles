"""实时推流评测：真的起一个服务，按 1x 速度推评测音频，从客户端收事件、按收到的墙钟时间算延迟。

离线台架（replay.py）用虚拟时钟、单线程顺序执行，量不出"定稿放进独立线程"这类并发改动的效果，
也不含翻译和识别抢 CPU 的影响。这里测的是用户在浏览器里实际感受到的延迟。

    python tools/live_bench.py --tag sync   -- --asr parakeet
    python tools/live_bench.py --tag async  -- --asr parakeet --async-finals

`--` 之后的参数原样交给 app.server（经 tools/run_service.py 的 --asr 映射）。

指标（都相对"这句话的音频结束时刻"，即 audio_end 被推到服务端的墙钟时间）：
- partial_gap：草稿更新间隔的中位 / p90（原文字幕是否在跟着说话走）；
- final：定稿原文到达延迟；
- zh：中文译文到达延迟。
"""

import argparse
import asyncio
import json
import socket
import statistics
import subprocess
import sys
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from faster_whisper.audio import decode_audio  # noqa: E402

SAMPLE_RATE = 16000
FRAME = 1600   # 100ms，与扩展一致


def pct(values, q):
    if not values:
        return None
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(q * len(ordered)))]


def wait_port(port, timeout=120):
    deadline = time.time() + timeout
    while time.time() < deadline:
        with socket.socket() as probe:
            if probe.connect_ex(("127.0.0.1", port)) == 0:
                return True
        time.sleep(0.5)
    return False


async def stream(port, pcm):
    import websockets
    events = []
    async with websockets.connect(f"ws://127.0.0.1:{port}", max_size=None) as ws:
        await ws.send(json.dumps({"type": "start", "language": "ja", "options": {}}))
        t0 = time.perf_counter()

        async def feed():
            for i in range(0, len(pcm), FRAME):
                # 按音频时间对齐墙钟推送，不累积 sleep 误差
                target = t0 + i / SAMPLE_RATE
                delay = target - time.perf_counter()
                if delay > 0:
                    await asyncio.sleep(delay)
                await ws.send(pcm[i:i + FRAME].tobytes())
            await asyncio.sleep(6)   # 让最后一句定稿和翻译回来
            await ws.close()

        task = asyncio.create_task(feed())
        try:
            async for message in ws:
                if isinstance(message, str):
                    event = json.loads(message)
                    if event.get("type") == "event":
                        event["_recv"] = time.perf_counter() - t0
                        events.append(event)
        except Exception:
            pass
        await task
    return events


def analyze(events):
    finals, zh, gaps = [], [], []
    last_partial = {}
    for e in events:
        seg = e["segment_id"]
        if e.get("translation"):
            zh.append(e["_recv"] - e["audio_end"])
        elif e.get("is_final") and e.get("text"):
            finals.append(e["_recv"] - e["audio_end"])
        elif not e.get("is_final") and e.get("text"):
            if seg in last_partial:
                gaps.append(e["_recv"] - last_partial[seg])
            last_partial[seg] = e["_recv"]
    return {"finals": len(finals), "translated": len(zh),
            "partial_gap_p50": pct(gaps, 0.5), "partial_gap_p90": pct(gaps, 0.9),
            "final_p50": pct(finals, 0.5), "final_p90": pct(finals, 0.9),
            "zh_p50": pct(zh, 0.5), "zh_p90": pct(zh, 0.9)}


def main():
    parser = argparse.ArgumentParser(description="实时推流评测")
    parser.add_argument("--tag", required=True)
    parser.add_argument("--clips", nargs="*", default=["ja_asmr_0230", "ja_asmr_0930"])
    parser.add_argument("--port", type=int, default=8767)
    parser.add_argument("server_args", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    extra = [a for a in args.server_args if a != "--"]

    server = subprocess.Popen(
        [sys.executable, "-u", str(ROOT / "tools" / "run_service.py"), "--port", str(args.port),
         "--no-log", *extra],
        cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        if not wait_port(args.port):
            raise SystemExit("服务没起来")
        time.sleep(1)
        rows = []
        for clip in args.clips:
            pcm = decode_audio(str(ROOT / "eval" / "audio" / f"{clip}.wav"), sampling_rate=SAMPLE_RATE)
            pcm = (np.clip(pcm, -1, 1) * 32767).astype("<i2")
            events = asyncio.run(stream(args.port, pcm))
            row = {"clip": clip, **analyze(events)}
            rows.append(row)
            print(json.dumps(row, ensure_ascii=False), flush=True)
    finally:
        server.kill()
        server.wait()

    def mean(key):
        values = [r[key] for r in rows if r[key] is not None]
        return round(statistics.fmean(values), 3) if values else None

    total = {k: mean(k) for k in rows[0] if k not in ("clip", "finals", "translated")}
    print(f"[{args.tag}] " + " ".join(f"{k}={v}" for k, v in total.items()), flush=True)
    out = ROOT / "runs" / "live_bench" / f"{args.tag}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"server_args": extra, "total": total, "clips": rows},
                              ensure_ascii=False, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main()
