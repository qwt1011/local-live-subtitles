# ASMR 本地识别基准

这个测试程序用于测量本机 CPU 运行 Whisper/SenseVoice 的速度。它不会上传音频。

**当前状态：M0–M3 已完成。** 架构诊断见 `ARCHITECTURE_REVIEW.md`，实测数据见 `BENCHMARK_RESULTS.md`，
里程碑与下一步见 `WORKLOG.md`。

流式链路已经跑通并验证（M3）：真实浏览器路径的会话（PCM over WebSocket）实测
**延迟 p50 0.46 秒 / p95 0.53 秒 / 定稿 0.83 秒，cpu_ratio 0.39，字错率 0.011**。
对照：停滞版本原型是 p50 19.7 秒 / 最坏 27.3 秒 / cpu_ratio 1.44。

## 目录

```
app/
  server.py               流式字幕服务：PCM 进、partial/final 事件出（WebSocket）
  events.py               字幕事件 schema + 统一的延迟口径 latency_views
  models_catalog.py       模型目录表（安装脚本与引擎共用）
  audio/
    vad.py                Silero VAD 门控与语音区间
    ring_buffer.py        按音频时间轴切片、丢弃已提交部分
  asr/
    factory.py            引擎构造的唯一入口（台架与服务共用）
    faster_whisper_engine.py   对照/离线精修通道
    sensevoice_engine.py       主引擎（非自回归 CTC，无 30 秒补齐的固定成本）
  pipelines/
    base.py               流水线协议（台架只依赖它）
    fixed_chunk.py        基线：复现"固定分块 + 单模型串行"的旧架构
    open_utterance.py     M2 流式内核：VAD 定句 + 开放段重解码
tools/
  replay.py               离线回放台架：虚拟时钟把 wav 按 1x 喂给流水线
  metrics.py              回放/真实会话结果对比：cpu_ratio / 延迟分位 / 字错率
  ws_client_test.py       M3 端到端验证：按实时速度推流给流式服务
  setup_models.py         模型下载（内置按速度排序的镜像列表）
  offline_reference.py    生成伪参考文本（大模型 + 整段上下文）
  diag_call_cost.py       每次 transcribe 调用的固定成本曲线
  diag_fallback.py        temperature 回退与 VAD 门控的对照实验
  diag_translate_cost.py  翻译层延迟预算标定
  smoke_service.py        旧 HTTP 服务的冒烟测试
```

## 使用

```powershell
$py = "C:\text\.venv\Scripts\python.exe"
& $py -m pip install -r requirements.txt

# 一次性：下载主引擎模型（自动走最快的镜像）
& $py -u tools\setup_models.py --engine sensevoice-2024

# 离线回放台架（不需要浏览器）
& $py -u tools\replay.py --wav sample_0230_0300.wav --pipeline open_utterance `
    --engine sensevoice --model sensevoice-2024 --partial-step 0.5 --repeat 5 `
    --out runs\M2.jsonl
& $py -u tools\metrics.py runs\M2.jsonl --reference runs\reference_small.txt
```

## 流式服务（M3）

```powershell
# 必须用 -m 运行（app.server 里有相对导入）
& $py -u -m app.server --engine sensevoice --model sensevoice-2024 --log runs\live.jsonl
```

协议：客户端先发 `{"type":"start","language":"ja"}`，然后连续发送
**16 kHz 单声道 s16le 裸 PCM** 二进制帧（客户端不做任何分段决策）；
服务端回 JSON 文本帧 `{"type":"event", "segment_id", "revision", "text", "is_final", ...}`。
`revision` 单调递增，渲染端只允许前进，否则字幕会回跳。

`--log` 写出的 JSONL 与离线台架同格式，可以直接用 `tools/metrics.py` 分析真实会话。

验证：

```powershell
& $py -u tools\ws_client_test.py --wav sample_0230_0300.wav --speed 1.0
```

## 旧的 HTTP 服务（将被 M4 替换）

启动后模型只加载一次。`--min-speech` 以下的请求会被 VAD 门控直接跳过，不消耗模型调用。

```powershell
& $py local_service.py --model base --language ja
Invoke-RestMethod http://127.0.0.1:8765/health
```

## 翻译

当前 `local_service.py` 里的 Argos 走 `ja→en→zh` 双重中转，而 `ja→en` 语言包至今安装失败，
日语翻译链路实际从未端到端跑通。计划（M5）改用 `opus-mt-ja-zh` + `ctranslate2` 直连，
作为独立 worker 不阻塞识别。详见 `BENCHMARK_RESULTS.md` 第 7 节。


