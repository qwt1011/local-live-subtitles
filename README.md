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

**推荐：双击 `启动字幕服务.bat`。** 它先做预检（模型是否就绪、端口是否被占用），
再把服务拉起来。注意 `.bat` 本身是纯 ASCII 的薄壳，中文提示都在
`tools/run_service.py` 里——cmd.exe 按当前代码页逐字节解析批处理，
UTF-8 中文会把语句拆碎（实测会把 `echo` 的中文当成命令去执行）。

```powershell
# 等价的手动命令。必须先 cd 到项目目录，否则报 No module named 'app'
cd C:\text\实验\asmr_transcription
& $py -u -m app.server --engine sensevoice --model sensevoice-2024 `
    --translate --translate-engine instruct --log runs\live.jsonl
```

协议：客户端先发 `{"type":"start","language":"ja"}`，然后连续发送
**16 kHz 单声道 s16le 裸 PCM** 二进制帧（客户端不做任何分段决策）；
服务端回 JSON 文本帧 `{"type":"event", "segment_id", "revision", "text", "is_final", ...}`。
`revision` 单调递增，渲染端只允许前进，否则字幕会回跳。

译文作为**同一个 `segment_id` 的更高 `revision`** 回传（带 `translation` 字段），
所以原文先显示、中文稍后原地补上，不会闪烁。实测中文出现在说完后 **0.67–0.88 秒**。

`--log` 写出的 JSONL 与离线台架同格式，可以直接用 `tools/metrics.py` 分析真实会话。

### 服务端日志能直接看出问题在哪一层

扩展点了「开始捕获」后，服务端窗口会打印：

| 日志 | 含义 |
|---|---|
| `客户端已连接` | 扩展连上服务了（popup 探测也打这条，但它**不会**打下面那条） |
| `采集开始：language=ja` | 扩展真的开始推音频了 |
| `警告：已开始采集，但 3 秒内没有收到任何音频` | 问题在 offscreen / AudioWorklet |
| 什么都没有 | 消息没到服务端，去看 service worker 的 Console |

验证：

```powershell
& $py -u tools\ws_client_test.py --wav sample_0230_0300.wav --speed 1.0
```

## 本地翻译（M5）

用本地小参数量指令模型 `Qwen2.5-0.5B-Instruct` 做 ja→zh：

```powershell
# 1) 下载模型
& $py -u tools\setup_models.py --engine qwen2.5-0.5b-instruct

# 2) 转成 CTranslate2 int8（473MB，比 fp32 快 2.4 倍、省 4 倍内存）
& "C:\text\.venv\Scripts\ct2-transformers-converter.exe" `
    --model models\Qwen2.5-0.5B-Instruct `
    --output_dir models\Qwen2.5-0.5B-Instruct-ct2-int8 --quantization int8 --force

# 3) 并排对比"孤立翻译"与"带上文翻译"
& $py -u tools\diag_translate_context.py --context-size 2 --style instruction
```

**为什么不用 NLLB**：NLLB 依赖的 `target_prefix` 语言标记机制在 CTranslate2 4.8.1 下
**被完全忽略**——换任何目标语言都输出同一段英文回声；两份预转换仓库和本地转换都一样，
而同一个 CT2 转出来的 Marian 模型工作正常。完整排查过程见 `BENCHMARK_RESULTS.md` 第 12 节。
指令模型用自然语言 prompt 表达翻译意图，绕开了这个机制。

**提示词用最简单的写法，不要"改进"它。** 默认 `--translate-style plain`，
system 只有一句 `把日语翻译成中文，只输出译文。`——这是**用户实测认可的基线**。

我一度把它改长（加"这是字幕翻译""不要输出日语"之类说明）并加上前文上下文，
离线看着更好，**真实使用却明确更差**：`この辺` 从"这边"退化成"条边"，
6 句耗时从 1.79s 涨到 2.74s。**0.5B 对提示词措辞极其敏感，
"写得更清楚"不等于"效果更好"。**

`--translate-style` 保留了另外两种写法（`instruction` / `completion`）供实验，
但**默认关闭**；`--translate-context` 默认也是 **0**。失败写法留在代码里，
因为它们记录了"什么写法会坏事"。

> **方法上的教训**：当时我用的离线 A/B 是**干净参考文本**，而真实运行喂进来的是
> **ASR 输出**（含误识别与碎片）。上下文在这种输入上会把错误传播下去。
> **评测输入必须来自真实链路**，否则结论会完全相反。

**质量的真实上限**：0.5B 就是这台机器的极限。再换 1.5B 约需 3 倍翻译算力，
`cpu_ratio` 会**超过 1.0 即跑不动**（详见 `BENCHMARK_RESULTS.md` 13.4）。
想继续提升只能腾预算或改预期——**别指望"换个更大的模型"或"再调调提示词"**。

**另一部分误差来自识别**：例如 `それじゃあ少しだけお話ししましょうか` 被识别成
`それじゃあ短いデータと行きましょうか`，这种句子翻译再准也没用——那是识别问题。

## 旧的 HTTP 服务（将被 M4 替换）

启动后模型只加载一次。`--min-speech` 以下的请求会被 VAD 门控直接跳过，不消耗模型调用。

```powershell
& $py local_service.py --model base --language ja
Invoke-RestMethod http://127.0.0.1:8765/health
```

## 翻译

**已完成（M5）**：本地 `Qwen2.5-0.5B-Instruct`（CT2 int8）直连 ja→zh，
中文出现在说完后 0.67–0.88 秒，只对定稿触发、不占原文延迟。见上面的「本地翻译」一节。

历史上的 Argos 方案（`ja→en→zh` 双重中转）已被放弃，原因是 `ja→en` 语言包始终装不上，
而且实测双重中转的英文本身就语法破碎、中文更糊。


