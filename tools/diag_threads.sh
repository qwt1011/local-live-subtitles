#!/usr/bin/env bash
# 识别 / 翻译线程分配对比：每组在 8767 端口另起一个服务，实时推流一段评测音频，再用 diag_display 统计。
# 用法：bash tools/diag_threads.sh [clip]   （默认 ja_asmr_0930，结果在 runs/diag_threads/）
set -u
cd "$(dirname "$0")/.."
PY=/c/text/.venv/Scripts/python.exe
CLIP=${1:-ja_asmr_0930}
PORT=8767
OUT=runs/diag_threads
mkdir -p "$OUT"
export PYTHONIOENCODING=utf-8

# 格式：标签 识别线程 翻译线程
CONFIGS=("asr5_tr4 5 4" "asr3_tr4 3 4" "asr2_tr6 2 6" "asr4_tr8 4 8")

for cfg in "${CONFIGS[@]}"; do
  set -- $cfg
  tag=$1; asr=$2; tr=$3
  $PY -u -m app.server --engine sensevoice --model sensevoice-2024 --translate \
      --port $PORT --threads "$asr" --translate-threads "$tr" > "$OUT/$tag.server.log" 2>&1 &
  pid=$!
  for _ in $(seq 1 60); do
    grep -q "8767" "$OUT/$tag.server.log" 2>/dev/null && break
    sleep 2
  done
  sleep 2
  $PY tools/ws_client_test.py --wav "eval/audio/$CLIP.wav" --port $PORT --speed 1.0 --flush-wait 6 \
      --save "$OUT/$tag.jsonl" > /dev/null 2>&1
  kill $pid 2>/dev/null; wait $pid 2>/dev/null
  echo "== $tag (asr=$asr tr=$tr)"
  $PY tools/diag_display.py "$OUT/$tag.jsonl" --lines 3 | tail -3
  $PY - "$OUT/$tag.jsonl" <<'EOF'
import json, statistics, sys
ev = [json.loads(l) for l in open(sys.argv[1], encoding="utf-8")]
tr = [e["detail"]["translate_seconds"] for e in ev if e.get("translation")]
fin = [e["latency"] for e in ev if e.get("is_final") and e.get("text") and not e.get("translation")]
if tr:
    print(f"服务内翻译计算 中位 {statistics.median(tr):.2f}s 最大 {max(tr):.2f}s；原文定稿延迟 中位 {statistics.median(fin):.2f}s")
EOF
done
