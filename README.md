# ASMR 本地识别基准

这个测试程序用于测量本机 CPU 运行 Whisper 的速度。它不会上传音频。

**当前状态：正在重构。** 架构诊断见 `ARCHITECTURE_REVIEW.md`，实测数据见 `BENCHMARK_RESULTS.md`，
里程碑与下一步见 `WORKLOG.md`。原型的端到端延迟实测为 p50 19.7 秒 / 最坏 27.3 秒
（`cpu_ratio=1.44`，即识别 30 秒音频要烧 43 秒 CPU）。

## 目录

```
app/                    分层骨架：events / audio.vad / asr 引擎 / pipelines
  pipelines/base.py     流水线协议（台架只依赖它）
  pipelines/fixed_chunk.py  基线：复现"固定分块 + 单模型串行"的旧架构
tools/
  replay.py             离线回放台架：虚拟时钟把 wav 按 1x 喂给流水线，输出逐事件延迟分解
  metrics.py            回放结果对比：cpu_ratio / 延迟分位 / 字错率
  offline_reference.py  生成伪参考文本（大模型 + 整段上下文）
  diag_call_cost.py     每次 transcribe 调用的固定成本曲线
  diag_fallback.py      temperature 回退与 VAD 门控的对照实验
  diag_translate_cost.py 翻译层延迟预算标定
  smoke_service.py      对 local_service.py 的 HTTP 冒烟测试
```

## 使用

```powershell
$py = "C:\text\.venv\Scripts\python.exe"
& $py -m pip install -r requirements.txt

# 离线回放台架（推荐入口，不需要浏览器）
& $py -u tools\replay.py --wav sample_0230_0300.wav --pipeline fixed_chunk --model base `
    --chunk 2.0 --min-speech 0.4 --repeat 3 --out runs\demo.jsonl
& $py -u tools\metrics.py runs\demo.jsonl

# 单文件基准
& $py benchmark.py path\to\audio.wav --model base
```

首次运行会下载模型文件，模型缓存通常位于用户目录下。建议先使用 30-60 秒的音频片段。

支持 `wav`、`mp3`、`m4a` 等 ffmpeg 可读取的格式。

## 本地常驻服务

启动后模型只加载一次。`tiny` 适合低延迟，`base` 适合平衡，`small` 适合准确率：

```powershell
..\..\.venv\Scripts\python.exe local_service.py --model base --language ja
```

健康检查：

```powershell
Invoke-RestMethod http://127.0.0.1:8765/health
```

服务接口：向 `http://127.0.0.1:8765/transcribe?language=ja` POST 一个 WAV 音频 body，返回 JSON 字幕片段。
服务只监听本机回环地址，不接受局域网连接。VAD 后语音不足 `--min-speech`（默认 0.4 秒）的请求会被直接跳过，
不消耗模型调用。

## 可选本地翻译

翻译层支持 Argos Translate。注意 Argos 没有 `ja→zh` 语言包，当前 `ja` 走 `ja→en→zh` 双重中转，
而 `ja→en` 语言包至今未安装成功——日语翻译链路实际上从未端到端跑通。
替代方案（`ctranslate2` 已装）是 `opus-mt-ja-zh` 或 `NLLB-200-distilled-600M` 转 CT2 int8 直连 `ja→zh`，
详见 `BENCHMARK_RESULTS.md` 第 7 节。

用 `--translate` 启动服务；未安装语言包时仍会正常返回原文，并在 JSON 中说明翻译不可用。

