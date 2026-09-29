"""M3 端到端验证：把一段 wav 按实时速度推给流式服务，检查事件与延迟。

这是唯一能证明"服务真的能按流式工作"的测试——离线台架证明的是算法，
这里证明的是传输、线程模型和事件顺序。

用法：
    # 先启动服务
    python app/server.py --engine sensevoice --log runs/live_session.jsonl
    # 再跑客户端
    python tools/ws_client_test.py --wav sample_0230_0300.wav --speed 1.0
"""

import argparse
import asyncio
import json
import sys
import time
import wave
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import websockets  # noqa: E402

FRAME_MS = 100


def load_pcm(path):
    with wave.open(str(path), "rb") as source:
        params = source.getparams()
        if params.framerate != 16000 or params.nchannels != 1 or params.sampwidth != 2:
            raise SystemExit(
                f"这个测试需要 16 kHz 单声道 16-bit wav，实际是 "
                f"{params.framerate}Hz/{params.nchannels}ch/{params.sampwidth * 8}bit"
            )
        return source.readframes(source.getnframes())


async def run(args):
    pcm = load_pcm(args.wav)
    frame_bytes = int(16000 * 2 * FRAME_MS / 1000)
    url = f"ws://{args.host}:{args.port}"
    print(f"连接 {url}，音频 {len(pcm) / 2 / 16000:.2f}s，速度 {args.speed}x")

    events = []
    started = time.perf_counter()

    async with websockets.connect(url, max_size=None) as websocket:
        await websocket.send(json.dumps({"type": "start", "language": args.language}))
        first = json.loads(await websocket.recv())
        print(f"服务状态：{first}")

        receiving = True

        async def receiver():
            while receiving:
                try:
                    message = await asyncio.wait_for(websocket.recv(), timeout=3.0)
                except asyncio.TimeoutError:
                    continue
                except Exception:
                    return
                payload = json.loads(message)
                if payload.get("type") != "event":
                    print(f"[status] {payload}")
                    continue
                stamp = time.perf_counter() - started
                payload["recv_wall"] = round(time.perf_counter() - wall_start, 4)
                events.append(payload)
                translation = payload.get("translation") or ""
                kind = "FINAL" if payload["is_final"] else "part "
                if translation:
                    kind = "TRANSL"   # 同一句话的更高 revision，只带译文
                print(f"[{stamp:6.2f}s] seg={payload['segment_id']:02d} "
                      f"rev={payload['revision']:02d} {kind} "
                      f"audio={payload['audio_start']:5.2f}-{payload['audio_end']:5.2f} "
                      f"lat={payload['latency']:5.2f} | {payload['text']}")
                if translation:
                    print(f"{'':19s} 译文 -> {translation}")

        # recv_wall 以推流开始为零点，与 audio_start / audio_end 同一条时间轴。
        wall_start = time.perf_counter()
        task = asyncio.create_task(receiver())

        # 按实时速度推流：这正是浏览器届时会做的事。
        sent = 0
        while sent < len(pcm):
            block = pcm[sent:sent + frame_bytes]
            await websocket.send(block)
            sent += len(block)
            target = (sent / 2 / 16000) / args.speed
            delay = wall_start + target - time.perf_counter()
            if delay > 0:
                await asyncio.sleep(delay)

        # 让服务把最后一句冲刷出来
        await asyncio.sleep(args.flush_wait)
        await websocket.send(json.dumps({"type": "stop"}))
        await asyncio.sleep(0.5)
        receiving = False
        task.cancel()

    elapsed = time.perf_counter() - started
    if args.save:
        args.save.parent.mkdir(parents=True, exist_ok=True)
        with args.save.open("w", encoding="utf-8") as handle:
            for event in events:
                handle.write(json.dumps(event, ensure_ascii=False) + "\n")
        print(f"事件已保存：{args.save}")
    # 译文事件也是 is_final=True（它靠更高的 revision 覆盖原句），
    # 统计"原文定稿延迟"时必须排除它们，否则数字会被译文拖高一大截，
    # 看起来像识别变慢了——实际原文路径完全没变。
    def is_translation(event):
        return bool((event.get("detail") or {}).get("translation"))

    originals = [event for event in events if not is_translation(event)]
    finals = [event for event in originals if event["is_final"] and event["text"]]
    translations = [event for event in events if is_translation(event)]
    partials = [event for event in originals if not event["is_final"]]
    print()
    print("---- 结果 ----")
    print(f"wall_seconds={elapsed:.2f} events={len(events)} "
          f"partials={len(partials)} finals={len(finals)} translations={len(translations)}")

    for label, group in (("partial", partials), ("final", finals), ("译文", translations)):
        if group:
            latencies = sorted(event["latency"] for event in group)
            print(f"{label:8s} latency p50={latencies[len(latencies) // 2]:.3f} "
                  f"max={latencies[-1]:.3f}")

    # 顺序性检查：同一个 segment 的 revision 必须单调递增，否则字幕会回跳。
    order_ok = True
    highest = {}
    for event in events:
        key = event["segment_id"]
        if event["revision"] < highest.get(key, -1):
            order_ok = False
        highest[key] = max(highest.get(key, -1), event["revision"])
    print(f"revision 单调性：{'OK' if order_ok else '失败（字幕会回跳）'}")

    text = " ".join(event["text"] for event in finals)
    print()
    print("---- 定稿文本 ----")
    print(text)
    return 0 if finals and order_ok else 1


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--wav", type=Path, required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument("--language", default="ja")
    parser.add_argument("--speed", type=float, default=1.0,
                        help="推流速度倍数；1.0 才是真实延迟测量")
    parser.add_argument("--flush-wait", type=float, default=1.5)
    parser.add_argument("--save", type=Path, default=None,
                        help="把收到的事件存成 JSONL（附客户端接收时刻 recv_wall），供 tools/diag_display.py 分析")
    args = parser.parse_args()
    raise SystemExit(asyncio.run(run(args)))


if __name__ == "__main__":
    main()
