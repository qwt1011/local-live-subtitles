# 架构评审：asmr_transcription

评审时间：本次会话。评审对象：`C:\text\实验\asmr_transcription` 全部代码与文档。
所有耗时数据均为本次在本机（i5-13500H / CPU INT8）实测，脚本见 `diag_call_cost.py`、`diag_fallback.py`。

---

## 0. 一句话结论

**当前实现的本质是"离线文件转写"，只是被包装成了流式。**
它的延迟下限被"块长度 + 每次调用固定开销"从架构上锁死，1.5 秒目标在设计层面就不可能达成；
而项目停滞的真正原因是**没有一个不需要浏览器的可复现验证回路**——每次想验证流式延迟，都要开 Chrome、开 YouTube、肉眼看，
所以核心问题无法迭代。这比任何一个具体 bug 都更致命。

---

## 1. 实测证据（先看数据，再看结构）

### 1.1 每次 `transcribe()` 调用的成本几乎是常数，与音频长度无关

`diag_call_cost.py`，16 kHz 单声道，取多次调用的最快值：

| 音频长度 | base 单次调用 | tiny 单次调用 | small 单次调用 |
|---|---|---|---|
| 2 s | 0.91 s | 1.66 s\* | — |
| 3 s | 0.99 s | — | 2.85 s |
| 5 s | 1.00 s | 0.56 s | — |
| 10 s | 1.01 s | 0.57 s | 3.00 s |
| 20 s | 1.25 s | — | — |
| 30 s | 1.51 s | 0.92 s | 4.18 s |

\* 2 s 点的 tiny 是异常值（见 1.2，触发了 temperature 回退）。

**读法：base 从 2 秒到 10 秒，调用耗时几乎不变（0.91 → 1.01 秒）。**
Whisper 的 encoder 永远对补齐到 30 秒的 mel 做一次前向，所以存在一个约等于 **tiny 0.55 s / base 1.0 s / small 3.0 s** 的**固定成本**，
而音频本身的边际成本接近于零。

这一条单独就否定了当前架构的两个核心假设：

1. **固定 2 秒分块 = 2 秒硬延迟下限。** 字幕最快也要等到「块被填满（2 s）+ 调用返回（1 s）+ 翻译」≈ 3 s 以后才可能出现。
   `DEVELOPMENT.md` 第 20 行的「尽量接近 1.5 秒」在当前结构下不是调参问题，而是不可能。
2. **分块越细，总成本越高。** 30 秒音频按 2 秒切 = 15 次调用 = 至少 15 秒算力 = **0.5 倍实时**，
   这还假设零边际成本、零回退、零浪费。真正决定吞吐的是 **"每秒发起多少次调用"**，而不是音频有多长。

### 1.2 13–19 秒的"重块"，元凶是 temperature 回退，不是"耳语段落太难"

`WORKLOG.md` 第 48 行猜的是「whisper-heavy ASMR sections」，`DEVELOPMENT.md` 第 130 行猜的是「静音、耳语和句子边界」。
实测（`diag_fallback.py`，30 秒音频切成 3 秒块，base）：

| 配置 | 总耗时 | 实时倍率 | 最慢块 |
|---|---|---|---|
| 默认 temperature 阶梯 + VAD | 19.85 s | 0.662 | chunk 02：**6.59 s** |
| `temperature=[0.0]` + VAD | 12.00 s | 0.400 | chunk 07：2.55 s |
| 默认阶梯、无 VAD | 16.23 s | 0.541 | chunk 07：**7.21 s** |

逐块对照可以看到，慢块只有两个，而且正是这两个触发了 temperature 回退：

- chunk 02：6.59 s → 2.30 s（VAD 后只剩 **0.66 秒**语音）
- chunk 07：7.21 s → 2.55 s

faster-whisper 默认 `temperature=[0.0, 0.2, 0.4, 0.6, 0.8, 1.0]`，
一旦某块的 `compression_ratio` / `log_prob` 越过阈值，就会用更高温度**重解码整段**，最多 7 遍。
所以"某些块要 13–19 秒"= 7 次解码，而不是"这段音频特别难"。

但**不能简单地关掉回退**：实测 chunk 02 关掉回退后，从"空文本"变成了
`お前はお前はお前はお前は…` 的复读幻觉。回退阶梯同时也在压制复读。
正确的解法是三层一起上：

1. **VAD 语音时长门控**——VAD 后语音 < 0.4 s 的片段**根本不要调用模型**（见 1.3）；
2. `repetition_penalty` / `no_repeat_ngram_size` 显式压制复读，取代温度阶梯的副作用；
3. 收窄温度阶梯（例如 `[0.0, 0.4]`），保留有限的兜底能力。

### 1.3 现有代码在静音上浪费调用，并因此产生幻觉

同一组数据里：

- chunk 03：VAD 后语音 **0.00 s**，模型照样被调用（返回空）；
- chunk 02：VAD 后语音 **0.66 s**，是全场最贵的调用之一，且输出是幻觉。

`local_service.py:64` 的 `vad_filter=True` 只是让 Whisper 在内部丢掉非语音片段，
**它不能阻止这次调用发生**。VAD 应该用在"决定要不要调用、调用哪一段"这一层（服务端的门控），
而不是当作 transcribe 的一个开关。

### 1.4 分块确实在破坏识别质量（不是猜测，是可对照的）

同一个 30 秒样本，两种跑法对照：

| | 3 秒独立块 | 30 秒整段（有上下文） |
|---|---|---|
| chunk 06 | `あなたみたいな人が一人だ` | `あなたみたいな人が一人で歩いてるなん` |
| chunk 08 | `この辺の最近は**散歩**が悪くなってきているから` | `治安が悪くなってきているから`（`DEVELOPMENT.md:121` 的 small 结果） |

第一行是句子被从中间切断；第二行 `散歩`（散步）vs `治安`（治安）是一个**语义级错误**，
纯粹的上下文缺失导致。这两处都是当前客户端 2 秒切块策略的直接代价。

### 1.5 临时文件路径的开销

`transport_3s_vad path=1.077s array=0.961s delta=0.116s`（base）——
每次请求经 MediaRecorder→WebM→HTTP→临时文件→ffmpeg 解码，比直接喂 numpy 数组多约 **0.12 秒（约占单次调用 12%）**。
单独看不致命，但它是纯浪费，而且在正确的流式设计里会整体消失。

---

## 2. 结构性缺陷（按严重程度）

### S1（致命）职责边界画错了：分段决策在客户端，流状态在服务端

```
content.js    渲染
background.js 消息路由（状态存在 service worker 的内存变量里）
offscreen.js  采集 + 2 秒固定分块 + WebM 编码 + 上传 + 拼接文本 + 错误上报
local_service.py  解码 + ASR + 翻译 + 一把全局锁
```

分块是**流语义**，它却被放在客户端（`offscreen.js:29` 的 `setTimeout(..., 2000)`）；
而句子的上下文状态只在服务端。两边都无法知道对方在做什么：

- 客户端不知道句子在哪里结束，所以块边界必然切在词中间，且块与块**没有重叠** → 边界词必丢；
- 服务端不知道下一块是不是同一句话，只能把每个块当独立句子处理；
- 客户端还在做文本拼接（`offscreen.js:37`），而翻译在服务端按块做 → 中文永远是"每 2 秒一坨"的碎片，
  永远不可能按句翻译。

**推论：只改服务端、保留 2 秒客户端分块是没有意义的——延迟下限仍然是 2 秒。要么一起改，要么白改。**

### S2（致命）用无状态传输承载有状态任务，再用一把锁打补丁

`Handler.lock`（`local_service.py:25`）包住了 `model.transcribe` **和 Argos 翻译**（第 55–90 行）。
后果是三重的：

1. `ThreadingHTTPServer` 的多线程不产生任何并发收益，只剩竞态风险；
2. **翻译与识别互相串行**，翻译延迟 100% 叠加在识别延迟上；
3. **没有任何背压策略**。识别 1 秒 + 翻译若干毫秒，块每 2 秒到一次——一旦某次 `processing > 2s`，
   请求就永久堆积，延迟单调增长、永不恢复。系统没有"丢掉最旧的待处理音频"这条规则。
   `WORKLOG.md:85` 说的"改为流水线"只让**录音**不阻塞，识别侧依然是串行队列。

### S3（高）没有提交模型：没有序号、没有 revision、没有 partial/final 之分

`DEVELOPMENT.md:63` 明确写了需求「字幕需要支持中间结果被最终结果替换」，
但架构里**完全没有对应的机制**。

- `offscreen.js:39` 收到响应就直接 `sendMessage`，无序号、无排序；
- 结果按响应到达顺序渲染 → 慢块后到会让字幕**往回跳**；
- 没有 partial/final 区分 → 无法实现"先显示低延迟草稿、稍后用高质量结果原地替换"。

### S4（高）模型/质量/延迟是启动时的静态开关，不是运行时的策略

`--model` 在进程启动时定死（`local_service.py:115`）。这逼出了一道假选择题：
`tiny` 快但糊，`base/small` 准但慢。实测已经给出了真正的答案——

| 引擎 | 单次调用固定成本 | 假设每 1.0 s 解码一次 | 能否实时 |
|---|---|---|---|
| tiny | ≈ 0.55 s | 0.55× 实时 | 可以，有余量 |
| base | ≈ 1.00 s | 1.00× 实时 | 临界，无余量 |
| small | ≈ 3.00 s | 3.00× 实时 | **完全不行**（只能做离线校正） |

也就是说：`tiny` 应该只做**草稿（partial）**，`base/small` 应该只对**已提交的句子**做二次校正（final）。
这正好同时解决你此前的两个观察——"tiny 准确率不足"和"延迟明显"——
但当前架构里根本没有"两个引擎、两条时间线"的位置。

### S5（高）日语翻译链路从未真正打通，且方案本身是错的

- Argos 没有 `ja→zh` 包，走 `ja→en→zh` 双重转译（`local_service.py:83-84`）：两次误差叠加、两次延迟，日语敬语/语气在英语中转时基本丢失；
- `ja→en` 包 100 MB 下载不下来（`WORKLOG.md:25, 46`），所以**日语翻译端到端从未成功过**；
- 翻译还在 S2 提到的同一把锁里，直接吃识别的时间预算。

替代方案（`ctranslate2 4.8.1` 已经装好，不需要新增大依赖）：`opus-mt-ja-zh` 或
`NLLB-200-distilled-600M` 转 CT2 INT8，**直接 ja→zh，无中转**。翻译应该抽成独立的后端接口 + 独立 worker。

### S6（中）前端实现层会直接卡住验收标准

- **全屏下字幕会消失。** overlay 挂在 `document.documentElement`（`content.js:13`）。
  YouTube 进入全屏后只有 fullscreen element 及其后代参与渲染，挂在 `<html>` 上的兄弟节点不会显示。
  必须挂到 `document.fullscreenElement` 内部，并监听 `fullscreenchange` 重新挂载。
  这直接违反 `DEVELOPMENT.md:141`「全屏可用」。
- overlay 是 `pointer-events:auto` + 最大 z-index（`content.js:15`）→ 会挡住 YouTube 控件。
- `content.js` 不读取已存的 `mode`（只在 popup 切换时靠消息同步，第 20 行）→ **刷新页面后模式回落成 bilingual**。
- 文本直接整体替换，没有 partial→final 的原地更新 → 闪烁、跳动。
- `background.js:1` 的 `activeTabId` 存在 service worker 内存里，worker 被回收后字幕**静默停止**，且没有恢复逻辑。
- 本地服务 `Access-Control-Allow-Origin: *`（`local_service.py:29,103`）→ 任何网页都能向它 POST 音频并拿到转写结果。
  应校验 `Origin` 为扩展 ID 或加一次性 token。

### S7（中）工程质量

- **不是 git 仓库**（`git status` → `fatal: not a git repository`）。无历史、无法回滚、无法开实验分支。
  14 MB 的 mp4 和 1 MB 的 wav 直接躺在工作目录里。
- `Handler` 用类属性当全局状态（`local_service.py:21-25`），模型在 `main()` 里注入 → 不可测试、不可多实例、不能并行跑不同模型。
- **`finally` 里的潜在 `NameError`**：`rows` 在 `finally` 中构建（第 66–74 行）。
  若 `self.model.transcribe` 抛异常，`segments` 从未绑定，`finally` 会抛 `NameError` **覆盖掉真实异常**，
  错误诊断被掩盖。
- 没有测试、没有 fixture、没有任何可复现的输入样本集。
- 端口 `8765` 硬编码在三个地方（`offscreen.js:35`、`popup.js:19`、README）。
- 延迟埋点只有服务端的 `processing_seconds`，**没有 capture→commit 的端到端分解** →
  "延迟明显"这句话无法归因到具体环节，只能靠感觉。

---

## 3. 为什么项目会停滞

把上面所有问题里最要命的一条单独拿出来：

> **核心问题（流式延迟）没有任何可复现的离线验证手段。**

想验证一个改动，必须：启动本地服务 → 打开 Chrome → 加载扩展 → 打开 YouTube → 点 popup → 播视频 → 用眼睛看。
单次反馈周期以分钟计，而且结果不可量化、不可对比、不可回归。
于是只能做"改一个参数、感觉一下"的优化，`WORKLOG.md` 里那些"延迟仍然明显"的条目就是这个循环的产物。

**所以重构的第一件事不是写流式内核，而是先建回放台架。** 见 M1。

---

## 4. 目标架构

原则（每条都是对上面某个缺陷的正面回应）：

1. 客户端只做**哑采集**，不做任何分段决策（回应 S1）。
2. 服务端持有唯一流状态；一个模型实例、一个消费循环（回应 S1/S2）。
3. **永不排队**：队列满时丢弃最旧的待处理音频并合并，而不是等待（回应 S2）。
4. 每次 `transcribe()` 必须值回票价：只对含足够语音的**开放段**解码，绝不解码静音（回应 1.2/1.3）。
5. 事件带 `segment_id` + `revision`，客户端只允许 revision 单调前进（回应 S3）。
6. 延迟必须可分解、可落盘（回应 S7）。

```
[Chrome MV3]
  content.js   YouTube 适配 + overlay（挂到 fullscreenElement；partial/final 两态渲染）
  offscreen.js AudioWorklet 取 PCM（16 kHz mono）→ 只做"哑"采集
        │  binary frames: [ seq ][ capture_ts ] + PCM
        ▼
  localhost WebSocket（单连接、单会话）
        │
[本地服务：单进程，显式管线]
  ① RingBuffer            始终保留最近 N 秒 16 kHz PCM
  ② Silero VAD 门控       faster_whisper.vad 自带，无需新依赖
                          · VAD 后语音 < 0.4 s  → 丢弃，不调用模型
                          · 检测到 ≥0.35 s 静音 → 关闭当前开放段
  ③ Segmenter             维护"开放段"音频 + 已完成段的上下文（prompt）
  ④ ASR worker（单线程，模型常驻）
                          · 只对开放段重解码，步长 1.0–1.5 s
                          · 收窄 temperature + repetition_penalty 抑制幻觉
                          · LocalAgreement-2：连续两次假设的公共前缀 → 提交为 final
  ⑤ Commit 事件           { segment_id, revision, text, is_final, audio_span, timings }
  ⑥ Translator worker     独立线程，仅对 final 触发，后端可替换
        │
        ▼  事件流：partial / final / translated / stats
```

**不变式**（写进代码注释，作为 review 的检查清单）：

- 客户端永不决定"切在哪里"；
- 服务端永远只有一个 ASR 在跑，第二个请求只能替换第一个的输入，不能排队；
- 任何一次模型调用都必须存在"这 0.5–3 秒算力花得值"的理由；
- 渲染只能前进，不能回退。

### 建议的工程骨架

```
asmr_transcription/
  app/
    audio/      ring_buffer.py  vad.py  segmenter.py
    asr/        engine.py(协议)  faster_whisper_engine.py  （未来 sensevoice_engine.py）
    translate/  engine.py(协议)  opus_mt.py  argos.py
    pipeline.py 会话状态机：唯一持有流状态的地方
    server.py   WebSocket / HTTP 传输层，不含任何业务逻辑
    events.py   partial/final/translated 的 schema
  tools/
    replay.py   离线回放台架（M1，最重要）
    metrics.py  延迟分解统计 P50/P95
    diag_call_cost.py / diag_fallback.py  （已存在）
  extension/    （见 M3/M4）
  tests/
  models/       （模型与语言包的本地缓存，加 .gitignore 例外说明）
```

---

## 5. 需要你决策的三件事

### 决策 1：ASR 引擎走哪条路

| 方案 | 内容 | 代价 | 收益 |
|---|---|---|---|
| A | 继续 faster-whisper，按第 4 节重写流式骨架 | 最小，1–2 天能看到结果 | 延迟会显著下降，但 base 单次 1.0 s 的固定成本仍在，实时余量小 |
| B | 引入 SenseVoice-small / sherpa-onnx 流式 zipformer 作为流式引擎，Whisper 保留做离线精修 | 要接新依赖，日语准确率必须实测 | 固定成本远低于 Whisper，才是"真流式" |
| C | **双通道**：tiny 出 partial，base/small 对 final 做二次校正 | 用现有模型资产，无新依赖 | 直接回应"tiny 不准"+"延迟明显"两个已知痛点 |

**我倾向先做 C**：不需要新依赖、能立刻复用已有基准数据、且正好落在当前最痛的两个点上。
B 可以并行做小规模评测，用数据决定要不要换。

> **2026-09 更新：本节结论已被实测修正，请以 `BENCHMARK_RESULTS.md` 第 5、8 节为准。**
>
> M1 台架建好后的实测显示：`base` 的单次调用固定成本 ≈ **1.10 秒**，与音频长度无关。
> 于是固定分块下 `cpu_ratio ≈ C/L` 与 `最坏延迟 ≈ L + C` 两个约束**无法同时满足**
> （`base` 需要 L ≥ 1.83 秒才够省 CPU，又需要 L ≤ 0.40 秒才能进 1.5 秒）。
>
> 所以：
> - **C 的双通道结构仍然正确**（草稿早到 + 定稿准确），
> - 但 **C 不能兑现"1.5 秒内看到正确文本"**：能进 1.5 秒的只有 tiny，而 tiny 的字错率是 base 的两倍以上
>   （0.648–0.725 vs 0.308–0.341）；base 定稿通道的 P95 结构性落在 2.8–3.2 秒。
> - 真正能同时拿到低延迟与可用质量的路径是 **B（换固定成本更低的流式引擎）**，
>   因此建议先做 B 的低成本判定实验，再决定 M2 建在哪个引擎上。

### 决策 2：翻译后端

| 方案 | 说明 |
|---|---|
| opus-mt-ja-zh + CT2 | 小（约 75 MB）、快、直连 ja→zh，质量中等 |
| NLLB-200-distilled-600M + CT2 INT8 | 质量更好、`ja→zho_Hans` 直连，较重（约 600 MB） |
| 暂时只保留 en→zh | 先放弃日语翻译，把原文链路做扎实（当前 en→zh 已可用） |

建议：抽象出 `translate/engine.py` 协议，先接 opus-mt，NLLB 作为可选后端；**彻底删掉 ja→en→zh 中转**。

### 决策 3：采集层是否一起重写

建议**一起改**。若保留 2 秒客户端分块，延迟下限仍是 2 秒（见 S1、1.1），流式内核的收益会被吃掉一半以上。

---

## 6. 迁移路线（每个里程碑都可独立验证）

| # | 内容 | 验证方式 | 量级 |
|---|---|---|---|
| **M0** | 收尾：`git init` + `.gitignore`（排除 mp4/wav/模型）；修 `finally` 的 `NameError`；收窄 temperature 阶梯 + `repetition_penalty`；启动时做一次 dummy 预热（实测首次调用曾达 8.4 s）；`/health` 返回模型与预热状态 | 服务能起、能测 | 半小时 |
| **M1** | **离线回放台架**：`replay.py` 把 wav 按实时速度喂给管线，输出 JSONL（逐事件延迟分解 + 文本），`metrics.py` 出 P50/P95；与全量离线转录对照算字错率 | 一条命令给出可对比的延迟/质量数字 | 半天 |
| **M2** | 流式内核：RingBuffer + VAD 门控 + 开放段重解码 + LocalAgreement-2 提交 + 双通道校正 | **用 M1 台架**调到 P95 < 1.5 s | 2–3 天 |
| **M3** | 传输替换：AudioWorklet PCM over WebSocket；删掉 MediaRecorder / WebM / 临时文件路径 | 台架 + 浏览器双验证 | 1 天 |
| **M4** | 渲染：partial/final 两态、不闪烁、挂载到 fullscreenElement、拖动/缩放持久化；popup 显示连接状态与实时延迟 | 对照 `DEVELOPMENT.md` 第 9 节验收清单 | 1 天 |
| **M5** | 翻译后端接入（独立 worker，不阻塞 ASR） | 台架测量翻译附加延迟 | 半天 |
| **M6** | 端到端浏览器验证，更新 `WORKLOG.md` / `DEVELOPMENT.md` | 真实 YouTube 视频 | 半天 |

**M1 是整个计划的关键**：有了它，M2 的每一次调参都是"跑一条命令、看 P95 变了多少"，
而不是"开浏览器感觉一下"。这直接消除第 3 节说的停滞成因。

---

## 7. 如果只想做最小改动

如果暂时不想重构，以下三处是**性价比最高的独立修改**（互相不依赖，可单独上）：

1. **VAD 语音时长门控**：VAD 后语音 < 0.4 s 的块直接返回空，不调用模型。
   预计能消掉 1.3 节那类"最贵且输出幻觉"的调用。
2. **收窄 temperature 阶梯 + `repetition_penalty=1.1` / `no_repeat_ngram_size=3`**：
   直接打击 1.2 节测到的 6.6–7.2 秒慢块。
3. **启动时垃圾预热**：加载模型后立刻用 1 秒静音跑一次 transcribe 丢弃结果
   （实测首次真实调用出现过 8.4 秒的异常）。

这三条不需要动架构，改完用 `diag_call_cost.py` / `diag_fallback.py` 就能量化前后差异。
但它们**不能**把延迟下限从 2 秒降下来——那需要 S1/S2/S3 的结构性修改。
