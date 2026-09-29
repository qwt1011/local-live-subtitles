"""诊断"译文跟不上"：把一次会话日志按扩展的显示规则重放，逐句统计译文是否来得及被看到。

    python app/server.py --engine sensevoice --translate --log runs/live_diag.jsonl
    python tools/ws_client_test.py --wav eval/audio/ja_asmr_0930.wav --speed 1.0
    python tools/diag_display.py runs/live_diag.jsonl --lines 2 3

显示规则与 extension/shared.js 的 SubtitleState 相同：屏幕上只保留 segment_id 最大的 N 句，
包括正在说的那句（partial）。所以一句话一旦有 N 个更新的句子出现，就会被挤出屏幕。

每句输出：
- 开口→中文：从这句开口（audio_start）到译文出现在屏幕上的时间，这是用户真正等待的时长；
- 定稿→中文：纯翻译排队 + 计算的时间；
- 可见：译文在屏幕上停留了多久，0 表示译文到达前这句已被挤掉，用户根本没看到。
"""

import argparse
import json
import statistics
from pathlib import Path

VISIBLE_ENOUGH = 1.5   # 译文停留不足这么久，基本来不及读


def load(path):
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    return [r for r in rows if r.get("type") != "header" and "segment_id" in r]


def arrival(event):
    # 客户端存的日志用 recv_wall（真正到达浏览器端的时刻），服务端日志只有 finish_wall。
    return event.get("recv_wall", event["finish_wall"])


def analyse(events, lines):
    events = sorted(events, key=arrival)
    first_seen = {}     # segment -> 首次出现在屏幕（有文字）的时间
    final_at = {}
    trans_at = {}
    info = {}
    for e in events:
        seg = e["segment_id"]
        if e.get("translation"):
            trans_at.setdefault(seg, arrival(e))
            info[seg]["translation"] = e["translation"]
            continue
        if not e.get("text"):
            continue
        first_seen.setdefault(seg, arrival(e))
        info.setdefault(seg, {"start": e["audio_start"]})
        info[seg].update(end=e["audio_end"], text=e["text"])
        if e.get("is_final"):
            final_at.setdefault(seg, arrival(e))

    # 句子 s 被挤出屏幕的时刻：第 N 个比它新的句子首次出现时。
    order = sorted(first_seen)
    evicted = {}
    for i, seg in enumerate(order):
        newer = order[i + lines] if i + lines < len(order) else None
        evicted[seg] = first_seen[newer] if newer is not None else float("inf")

    rows = []
    for seg in order:
        if seg not in final_at:
            continue
        item = info[seg]
        shown = trans_at.get(seg)
        gone = evicted[seg]
        visible = 0.0 if shown is None or shown >= gone else gone - shown
        rows.append({
            "seg": seg, "text": item["text"], "translation": item.get("translation", ""),
            "speech": item["end"] - item["start"],
            "wait": None if shown is None else shown - item["start"],
            "queue": None if shown is None else shown - final_at[seg],
            "visible": visible,
        })
    return rows


def report(rows, lines):
    print(f"\n==== 显示 {lines} 行 ====")
    print(f"{'seg':>3} {'时长':>5} {'开口→中文':>8} {'定稿→中文':>8} {'可见':>6}  原文")
    for r in rows:
        wait = "-" if r["wait"] is None else f"{r['wait']:.2f}"
        queue = "-" if r["queue"] is None else f"{r['queue']:.2f}"
        vis = "∞" if r["visible"] == float("inf") else f"{r['visible']:.2f}"
        flag = "  ✗没看到" if r["visible"] == 0 else ("  △太短" if r["visible"] < VISIBLE_ENOUGH else "")
        print(f"{r['seg']:>3} {r['speech']:5.1f} {wait:>8} {queue:>8} {vis:>6}  {r['text'][:28]}{flag}")

    translated = [r for r in rows if r["wait"] is not None]
    missed = sum(1 for r in rows if r["visible"] == 0)
    short = sum(1 for r in rows if 0 < r["visible"] < VISIBLE_ENOUGH)
    print(f"\n定稿 {len(rows)} 句，有译文 {len(translated)} 句；"
          f"译文没被看到 {missed} 句，停留不足 {VISIBLE_ENOUGH}s {short} 句")
    if translated:
        waits = sorted(r["wait"] for r in translated)
        queues = sorted(r["queue"] for r in translated)
        print(f"开口→中文 中位 {statistics.median(waits):.2f}s 最大 {waits[-1]:.2f}s；"
              f"定稿→中文 中位 {statistics.median(queues):.2f}s 最大 {queues[-1]:.2f}s")
        long_rows = [r for r in translated if r["speech"] >= 4.0]
        if long_rows:
            print(f"长句（≥4s）{len(long_rows)} 句：开口→中文 中位 "
                  f"{statistics.median(r['wait'] for r in long_rows):.2f}s")


def main():
    parser = argparse.ArgumentParser(description="按扩展显示规则重放会话日志，诊断译文可见性")
    parser.add_argument("log", type=Path)
    parser.add_argument("--lines", type=int, nargs="+", default=[2, 3])
    args = parser.parse_args()
    events = load(args.log)
    for lines in args.lines:
        report(analyse(events, lines), lines)


if __name__ == "__main__":
    main()
