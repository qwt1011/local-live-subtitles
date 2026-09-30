# 开发说明

面向想改代码的人：架构、关键取舍和它们的依据、怎么评测。实验过程和被推翻的结论在 [docs/WORKLOG.md](docs/WORKLOG.md)。

## 架构

```
extension/
  popup.*        弹窗：状态探测、一键启动、设置
  background.js  路由：取流、创建 offscreen、把字幕事件转给标签页、补注入字幕脚本、工具栏角标
  offscreen.js   采集：tabCapture → AudioWorklet（pcm-worklet.js）→ 16 kHz s16le → WebSocket
  content.js     覆盖层：按 segment_id + revision 渲染 3 行字幕，全屏 / 本地文件 popover 顶层
  shared.js      纯逻辑（分帧、字幕状态机、探测），Node 下可测

tools/native_host.py   一键启动宿主（Chrome Native Messaging）：status / start / restart / stop / log
tools/run_service.py   启动前预检 + 选识别模型（--asr parakeet|sensevoice|hybrid）

app/server.py          会话：识别线程 + 翻译线程；事件回推；会话日志；闲置退出
app/pipelines/open_utterance.py
                       流式内核：VAD 找句子边界，开放段每 0.5s 重解码一次出草稿，
                       静音 0.35s 定稿（或 10s 强制切断）；语气词过滤；两个实验开关
app/asr/               sherpa_offline_engine（Parakeet）/ sensevoice_engine / faster_whisper_engine（对照）
app/translate/         hymt_gguf（默认）/ instruct_local（Qwen2.5-0.5B 备选）/ nllb_ct2（已证实不可用，留作记录）
```

### 协议

客户端先发 `{"type":"start","language":"ja","options":{...}}`，然后连续发 100ms 一帧的 16 kHz 单声道 s16le 裸 PCM。
客户端**不做任何分段决策**。服务端回 `{"type":"event","segment_id","revision","text","is_final","translation"?,...}`：

- 同一句话的草稿、定稿、译文共用一个 `segment_id`，`revision` 单调递增，渲染端只接受更大的 revision，所以字幕不会回跳；
- 译文是定稿之后的下一个 revision，原文先显示，中文原地补上；
- `{"type":"ping"}` 返回服务状态（引擎、模型、翻译、闲置设置），弹窗靠它探测。

`options` 目前有两个实验开关：`early_final`、`adaptive_silence`（见下文）。

## 关键取舍与依据

每一条都在评测集或真实会话上量过，数字出处在 `runs/`，过程在 `docs/WORKLOG.md`。

| 决定 | 依据 |
|---|---|
| **不用 Whisper 做实时识别** | 每次调用有约 1 秒固定成本（30 秒补齐），与音频长度无关；温度回退阶梯会让单块从 2.3s 涨到 6.6s（`docs/BENCHMARK_RESULTS.md` 第 2–5 节） |
| **VAD 断句 + 开放段重解码**，而不是固定分块 | 固定分块要么切断词、要么等太久；重解码让草稿跟着说话走，定稿时整句解码一次 |
| **默认识别 Parakeet-ja** | 评测集 CER 0.066，SenseVoice 0.090；译文与参考译文的 chrF 中位 0.86 vs 0.76。实时推流下中文 p50 1.04s，与 SenseVoice（1.22s）相当（`runs/eval/parakeet_ja_clean2`、`runs/live_bench/`） |
| Parakeet 输出要清洗 | 它把笑声转成「フフフ」、句中用空格代替标点、句末不补「。」。少了句号 Hy-MT 的译法就会变，清洗前译文反而比 SenseVoice 差（`app/asr/sherpa_offline_engine.py::clean_text`） |
| SenseVoice 去掉词间空格 | 它会在日语词之间插空格，翻译模型当成断句，6 句里有 3 句因此译错（`join_cjk`） |
| **翻译用 Hy-MT2-1.8B** | Qwen2.5-0.5B 快一倍，但 22 句真实定稿里 6–7 句严重错误；NLLB 的目标语言标记在 CTranslate2 下被忽略（`docs/BENCHMARK_RESULTS.md` 第 12 节） |
| 翻译不带上下文、用官方默认提示词 | 加上下文和"更清楚"的提示词离线看着更好，真实 ASR 输入上明确更差：错误会沿上下文传播 |
| 只翻译定稿，放独立线程 | 翻译不占原文延迟；草稿变化太快，翻了也白翻 |
| 识别不拆成草稿/定稿两个线程 | 实测两路 ONNX 并发抢核，中文 p90 从 1.27s 变成 3.89s（`--async-finals`，默认关） |
| 字幕 3 行 | 译文平均在定稿后 2–3 秒到，2 行时长句的译文常在到达前被挤出屏幕 |
| 语气词整句过滤 | 「う」「へへ」「ふふふ」单独成句时置空，CER 0.093 → 0.071 |
| 静音阈值保持 0.35s | 0.5 / 0.6 的 CER 差异在噪声内，但定稿延迟变长 |

### 实验开关（默认关闭）

- `early_final`（粘连句提前定稿）：两句话之间停顿不到 0.35s 被 VAD 粘在一起时，用 token 时间戳把前半句切出来先定稿；
- `adaptive_silence`（句末短静音定稿）：草稿以ね/よ/わ/かしら/？结尾时，静音 0.2s 就定稿；
- `hybrid` 识别：草稿 SenseVoice、定稿 Parakeet。

三个都做过评测，收益在噪声范围内或更差，所以默认关闭。长句延迟是结构性的：要等这句话说完。

## 评测

**评测集**（`eval/`）：同一个 ASMR 视频的 5 段 × 60 秒，按音量和语音密度挑选（最轻、稀疏、密集等）。
参考文本是 whisper large-v3 与 small 交叉比对后语义裁定的（`status: consensus`），**没有人工听写**，
适合比较两个配置的相对好坏，不代表绝对准确率。音频不入库，用 `tools/build_eval.py` 从源视频切出。

**三种台架**，按"越接近真实越慢"排列：

| 工具 | 测什么 | 注意 |
|---|---|---|
| `tools/replay.py` / `tools/eval_suite.py` | 离线回放：虚拟时钟按 1x 喂音频，计算耗时计入墙钟。输出字错率、延迟分位、cpu_ratio | 单线程顺序执行，量不出并发改动；单段 CER 单次波动约 ±0.02，要 `--repeat 3` |
| `tools/asr_ceiling.py` | 识别错误拖累了多少翻译：ASR 定稿与参考原文分别翻译，比较译文 chrF | 翻译模型本身的误差不在比较范围内 |
| `tools/live_bench.py` | 真起服务、按 1x 推流、按客户端收到的墙钟算，含翻译抢 CPU | 最接近浏览器里的体验；受机器负载影响大 |

真实会话日志（`runs/live/`，与台架同格式）可以用 `tools/metrics.py`、`tools/diag_display.py` 复盘。

## 测试

```powershell
python tests\test_open_utterance.py      # 流水线：语气词、断句、提前定稿、混合引擎、Parakeet 清洗
node tests\test_extension_logic.js       # 扩展：分帧、字幕状态机、挂载点、本地媒体识别
node tests\test_probe_service.js         # 需要 8766 上有服务
node tests\test_ws_protocol.js           # 需要服务 + sample_0230_0300.wav
```

## 改扩展代码后怎么生效

扩展是"已解压"方式加载的，Chrome 不会自动重读文件：`chrome://extensions` 里点本扩展的刷新 → 关掉再打开弹窗 →
刷新视频页（开始字幕时也会自动补注入字幕脚本）。三块各自的 Console：页面 F12（覆盖层）、扩展卡片上的
「Service Worker」（路由）、「检查视图 offscreen.html」（采集）。更多排查见 [extension/README.md](extension/README.md)。
