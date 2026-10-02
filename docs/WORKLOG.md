# 开发日志

按时间顺序记录每一轮的改动、实测数字和结论（包括被推翻的结论）。开头几节是 08 月的英文原型记录，
09-22 之后是重构（M0–M5）和之后的迭代。**当前状态看 [README](../README.md) 和 [DEVELOPMENT](../DEVELOPMENT.md)**，
这里只是过程。文中的 `runs/...` 路径都是相对项目根目录；早期脚本已移到 `legacy/`。


## Current status

The project has a working offline transcription prototype, a localhost service, and a Chrome/Edge extension scaffold. Local Chinese translation is partially installed but Japanese translation is blocked by an unreliable Argos package download.

## Completed

- Confirmed target platform: Windows 11, Chrome/Edge, YouTube only for MVP.
- Confirmed subtitle modes: original, Chinese, and bilingual.
- Confirmed local-first and free operation as product constraints.
- Extracted `02:30-03:00` from the reference ASMR video to `sample_0230_0300.wav`.
- Benchmarked `faster-whisper base`: 30 seconds transcribed in 1.21 seconds.
- Benchmarked `faster-whisper small`: 30 seconds transcribed in 9.54 seconds with better Japanese accuracy.
- Tested naive 3-second chunks and overlapping windows; both were unstable for strict realtime ASMR.
- Implemented `local_service.py`, listening only on `127.0.0.1:8765`.
- Implemented `/health` and `/transcribe` endpoints with a persistent Whisper model.
- Added optional Argos translation and Japanese routing through `ja -> en -> zh`.
- Implemented Manifest V3 extension scaffold, YouTube subtitle overlay, service health check, tab audio capture, offscreen recording, and audio playback passthrough.
- Installed Argos Translate engine.
- Installed Argos `en -> zh` language package.

## In progress

- Argos `ja -> en` package download. The package is roughly 100 MB and repeated network interruptions prevent completion; a run exceeded 3000 seconds and was stopped.
- End-to-end browser test after translation packages are available.

## Latest browser test fixes

- Fixed CORS preflight handling for extension requests (`OPTIONS`).
- Confirmed browser audio requests reach the local service.
- Fixed Windows temporary WebM file locking before Whisper reads the file.
- Changed extension recording to send complete 3-second WebM recordings instead of non-decodable incremental blobs.
- Removed duplicate recorder callbacks in the offscreen document.
- Added service logging of recognized segment text and extension-side response logging.
- Current server logs show successful recognition responses; final verification of message delivery to the YouTube overlay is still pending.
- Subtitle overlay is now working in the browser; original/Chinese/bilingual display modes are being added.
- User feedback: perceived delay is at least one second; subtitle should be draggable and resizable; bilingual text should be stacked vertically; Japanese Chinese translation remains unavailable because `ja -> en` is not installed; English output intermittently does not appear despite successful server recognition.
- Implemented pointer dragging and mouse-wheel font resizing on the overlay.
- User reports English subtitles are now generally stable; main remaining issue is noticeable latency.
- Reduced extension recording interval from 3 seconds to 2 seconds and added `tiny` as a low-latency service model option.

## Known issues

- Argos has no direct `ja -> zh` package; Japanese translation uses English as an intermediate language and may lose nuance.
- Argos downloads do not expose byte progress through the library API. The installer now prints a heartbeat every 10 seconds.
- A sandbox-only Argos config-path error was observed at `C:\Users\asus\.config\argos-translate`; the user's normal PowerShell can run Argos and is the authoritative environment for package installation.
- Naive independent chunks can take 13-19 seconds on whisper-heavy ASMR sections.
- The browser audio capture path has not yet completed a live YouTube end-to-end verification.

## Next actions

1. Keep `base` as the default realtime model; do not use `tiny` as the recommended mode.
2. Reduce latency through capture and request pipelining without lowering recognition quality.
3. Add visible connection/recognition status to the extension popup.
4. Revisit Japanese translation only after a reliable language-package source is available.

## Run commands

Install remaining Argos packages:

```powershell
C:\text\.venv\Scripts\python.exe C:\text\实验\asmr_transcription\install_argos_languages.py
```

Start the Japanese service with translation:

```powershell
C:\text\.venv\Scripts\python.exe C:\text\实验\asmr_transcription\local_service.py --model base --language ja --translate
```

## 2026-08-15 开发记录

- 英语视频端到端链路已经打通：标签页音频捕获、本地 Whisper 识别、Argos 英译中和页面双语字幕均能运行。
- 修复 CORS 预检、WebM 增量块不可解码、Windows 临时文件锁和 offscreen 重复录音回调问题。
- 字幕支持双语、仅原文、仅中文三种模式；双语为上下排列。
- 字幕支持鼠标拖动位置，并可通过滚轮调整字号。
- 用户反馈英语字幕总体可用，但延迟明显；3 秒录音块已缩短为 2 秒。
- 测试 `tiny` 低延迟模型后，用户认为准确率不足。因此 `tiny` 只保留为实验选项，默认方案恢复为 `base`。
- 后续延迟优化将集中在音频分块、请求调度和流水线处理，不再以明显降低模型准确率为代价。
- 日语原文识别可用；日译中仍因 Argos `ja -> en` 语言包无法稳定下载而阻塞。

## 2026-08-16 开发记录

- 发现此前录音和识别为串行：每个 2 秒录音块必须等待识别及翻译结束后才开始下一段，造成音频空档和额外延迟。
- 改为流水线处理：上一段上传识别时，下一段立即开始录制。
- 本地服务响应新增 `processing_seconds`，PowerShell 日志会显示每个音频块的实际处理耗时。

## 停滞期评审 + M0/M1（本次会话）

架构评审结论与完整证据见 `ARCHITECTURE_REVIEW.md`，实测数据见 `BENCHMARK_RESULTS.md`。

- 完成架构评审。核心结论：当前实现本质是"离线文件转写包装成流式"，
  延迟下限被"块长度 + 每次调用固定开销"锁死；项目停滞的真正原因是没有可复现的离线验证回路。
- 用 `diag_call_cost.py` 证明每次 `transcribe()` 的固定成本 ≈ 0.9–1.1 秒，**与音频长度几乎无关**
  （base：2 秒音频 0.91s，10 秒音频 1.01s）。Whisper 的 encoder 永远处理补齐到 30 秒的 mel。
- 用 `diag_fallback.py` 定位 `WORKLOG` 中"某些块要 13–19 秒"的真正元凶是 **temperature 回退阶梯**
  （失败时用更高温度重解码整段，最多 7 遍），不是此前猜测的"耳语段落太难"。
  同一个 3 秒块：默认阶梯 6.59s → `temperature=[0.0]` 2.30s。但直接关掉会让该块从"空文本"变成复读幻觉，
  因此必须同时用 `repetition_penalty` / `no_repeat_ngram_size` 压制。
- **M0 完成**：
  - `git init` + `.gitignore`（排除 14MB 源视频与模型缓存，保留 sample wav 作为台架 fixture）；
    需要为本目录加 `safe.directory`（目录属主是之前的沙箱账户，与当前用户不同），仓库级身份设为 `asus/asus@localhost`。
  - `local_service.py` 重写：VAD 语音时长门控、收窄温度阶梯、内存流解码（不再落临时文件）、
    翻译移出模型锁、修掉 `finally` 中 `segments` 未绑定导致的 `NameError` 掩盖真实异常、启动预热 + `/health` 上报预热状态。
- **M1 完成**：离线回放台架 `tools/replay.py`（虚拟时钟，音频 1x 到达，计算耗时推进墙钟）
  + `tools/metrics.py` + `tools/offline_reference.py` + `tools/smoke_service.py` + `tools/diag_translate_cost.py`，
  以及 `app/` 分层骨架（events / audio.vad / asr 引擎 / pipelines 协议 + 固定分块基线）。
- **最重要的实测发现**：原型的 `cpu_ratio = 1.44`（识别 30 秒音频要烧 43 秒 CPU），
  端到端延迟 p50 **19.7 秒**、最坏 **27.3 秒**，且因为没有背压会单调落后、永不恢复。
  这才是"延迟明显"的量化答案，此前记录里的"至少一秒"严重低估了问题。
- 台架暴露的两个工程要求：单次 `cpu_ratio` 波动大（0.42–0.60），必须取中位数；
  一次病态调用耗时 1255 秒且不复现 —— **流水线必须有每次调用的硬时间预算与看门狗**。
- 修正了 `ARCHITECTURE_REVIEW.md` 第 5 节的推荐：双通道（方案 C）结构正确，
  但受 `base` 单次成本 1.10s 的结构性限制，**无法兑现"1.5 秒内看到正确文本"**。
  建议先做"换更低固定成本引擎"（SenseVoice / sherpa-onnx）的低成本判定实验，再决定 M2 建在哪个引擎上。

## 下一步

1. 判定实验：接一个非 Whisper 的流式引擎到 `Pipeline` 接口，用同一条 `replay.py` 命令对比
   `cpu_ratio` 与字错率，决定 M2 的引擎底座。
2. 在 `local_service.py` 与未来的流式内核里加每次调用的硬超时与丢弃策略。
3. M2：VAD 门控 + 开放段重解码 + LocalAgreement-2 增量提交 + 草稿/定稿两态。

## 换引擎判定实验 + M2（本次会话续）

### 判定实验：结论是换引擎

把 SenseVoice（sherpa-onnx）接到同一个 `Pipeline` 接口，**台架一行没改**（这正是 M1 的目的）。
完整数据见 `BENCHMARK_RESULTS.md` 第 10 节。

- 单次调用成本：SenseVoice 1 秒块 **0.038 秒**，base 是 **1.10 秒**，差 **29 倍**；
  而且 SenseVoice 的成本**随音频长度走**（30 秒整段 1.78 秒），没有 Whisper"补齐到 30 秒"的固定成本。
- 质量反而更好：SenseVoice 整段字错率 **0.022**（几乎与伪参考逐字相同），base 是 0.121。
- 因此第 5 节那两道把 base 逼死的约束自动松开，1.5 秒目标在改引擎后就已达成。
- 环境坑（已记录并自动处理）：GitHub Releases 直连约 2 KB/s（158 MB 要 20 小时以上），
  `gh-proxy.com` 776 KB/s、`hf-mirror.com` 669 KB/s、ModelScope 4.1 MB/s，已写进 `tools/setup_models.py`；
  PyPI 的 `sherpa-onnx-core` 只有 `py3-none-*` 标签（Python 3.13 可用），但 pip 联网解析会卡死，
  改用离线 wheel + `--no-index --find-links` 安装。

### M2 完成：VAD 驱动的"开放段重解码"

`app/pipelines/open_utterance.py`。句子边界由 VAD 决定（永不切在词中间），
开放期间每 `partial_step` 秒重解码整句出 partial，检测到静音则解码整句出 final。

**没有用 LocalAgreement-2**：它需要文本前缀与音频时间的对应关系才能裁剪缓冲区，
而 SenseVoice 既没有 `initial_prompt` 也没有词级时间戳；好在它单次成本只有约 0.05 秒/音频秒，
重解码整句本来就负担得起。

**达成情况（同一段 30 秒 ASMR，中位数，step=0.5s / sil=0.35s）：**

| 指标 | 目标 | 实测 |
|---|---|---|
| P95 延迟 | ≤ 1.5 秒 | **0.677 秒** |
| 最坏延迟 | — | 1.093 秒 |
| 首字延迟 P95 | — | 0.783 秒 |
| 定稿延迟 P95 | — | 1.076 秒 |
| 原文质量 | 不低于 base 整段（0.121） | **0.011**（好约 10 倍） |
| cpu_ratio | < 1 | 0.404（余 60% 给翻译） |

对照：停滞原型是 p50 19.7 秒 / 最坏 27.3 秒 / cpu_ratio 1.44 / 字错率 0.462。

- 修正了一个会把流式架构算错 5 倍的延迟口径问题：延迟必须按"音节第一次出现在屏幕上的延迟"算，
  不能拿最后一个事件的墙钟去惩罚整句。口径统一放在 `app/events.py::latency_views`，台架与报告共用，
  已验证两边数字完全一致；对固定分块会自动退化成原算法。
- 同引擎对照确认换引擎是必需的：M2 + Whisper base 能把质量从 0.341 提到 0.143，
  但 p95 仍是 3.2 秒——救得了质量，救不了延迟。

## 下一步（更新）

1. **M3**：把流式内核包成 WebSocket 服务（PCM 流进、partial/final 事件流出），替换
   `local_service.py` 的"每块一个 HTTP 请求"；扩展端改 AudioWorklet 取 PCM。
2. **M4**：partial/final 两态渲染、全屏挂载（当前 overlay 挂在 `documentElement`，全屏会消失）、
   设置持久化。
3. **M5**：改用 `opus-mt-ja-zh` CT2 直连翻译（已放弃 NLLB 与 Argos ja→en→zh 双重中转），
   独立 worker 不阻塞识别。
4. **M6**：端到端浏览器验证。

## M3 完成：流式服务（PCM over WebSocket）

`app/server.py`，替换原来"每 2 秒一个 HTTP 请求 + 服务端全局锁串行"的做法。

- 协议：客户端先发 `{"type":"start"}`，随后连续发送 16 kHz 单声道 s16le 裸 PCM；
  服务端回 `{"type":"event", segment_id, revision, text, is_final, ...}`。
  **客户端不做任何分段决策**（不变式 1）。
- 线程模型：asyncio 收音频/发事件，一个识别工作线程"取作业→跑识别→投事件"；
  锁只保护流水线状态，**识别调用在锁外执行**，音频不会被识别阻塞；
  事件队列满时丢最旧的（不变式 3"永不排队"）。
- `--log` 把真实会话落成与离线台架**同格式**的 JSONL，可直接用 `tools/metrics.py` 分析。
  这条统一让"真实浏览器路径"从此刻起也是可量化、可回归的。

**M3 端到端验证（`tools/ws_client_test.py`，按 1.0x 实时速度推流 30 秒音频）：**

| 指标 | 实测 |
|---|---|
| 延迟 p50 / p95 | **0.464 / 0.529 秒** |
| 最坏 | 0.858 秒 |
| 首字 P95 | 0.728 秒 |
| 定稿 P95 | 0.825 秒 |
| cpu_ratio | 0.386 |
| 字错率 | 0.011 |
| revision 单调性 | OK（不会回跳） |
| 事件丢失 | 0 |

进度：**M0 ✓ M1 ✓ 换引擎判定 ✓ M2 ✓ M3 ✓**，剩下 M4（扩展渲染/全屏）、M5（opus-mt 翻译）、M6（浏览器实测）。

## M4 完成：扩展端（AudioWorklet + 两态渲染 + 全屏修复）

扩展从 0.1.0 升到 0.2.0，采集层彻底换掉。

**采集层（去掉 MediaRecorder / WebM / 临时文件）**

- 新增 `extension/pcm-worklet.js`：AudioWorklet 直接把裸 PCM 交出来。
  `AudioContext({sampleRate:16000})` 让浏览器负责重采样，不在 JS 里手写。
- `extension/offscreen.js` 只做"哑采集 + 推 WebSocket"，**不做任何分段决策**（不变式 1）；
  没连上时直接丢帧而不是排队（不变式 3）。
- `manifest.json` 增加 `content_security_policy.connect-src`（允许连 `ws://127.0.0.1:8766`）、
  `minimum_chrome_version: 116`（`offscreen.hasDocument()` 需要）。

**渲染层**

- **修全屏消失**：覆盖层改为挂到 `document.fullscreenElement` 内部，并在 `fullscreenchange`
  时重新挂载。这是 `REQUIREMENTS_0815.md` 验收项"全屏可用"一直没过的原因。
- **partial / final 两态**：`SubtitleState` 按 `segment_id + revision` 维护，
  partial 原地替换成草稿样式（半透明 + 结尾 `…`），final 到达后原地转成定稿样式，不闪烁；
  **迟到的旧 revision 直接丢弃**——这是字幕回跳的根治办法。
- 覆盖层容器 `pointer-events:none`，只有文字块 `pointer-events:auto`，不再吃掉 YouTube 控件的点击。
- 设置（模式/字号/位置）读写 `chrome.storage.local` 并监听 `storage.onChanged`，
  刷新页面不再回落成"双语"。

**状态与健壮性**

- `background.js` 把 `activeTabId` / `capturing` 存进 `chrome.storage.session`：
  原来放在内存变量里，MV3 的 service worker 一被回收字幕就**静默停止**且不恢复。
- offscreen 的消息监听器显式 `sendResponse`：否则 `background` 那边 `await sendMessage`
  会以 "message port closed" 失败，表现为"点了开始捕获没反应"。
- popup 显示引擎名、已接收音频秒数、最近一条延迟（并标明是草稿还是定稿）。

**没有浏览器也能跑的验证（本次新增 `tests/`）**

| 测试 | 覆盖 | 结果 |
|---|---|---|
| `node tests/test_extension_logic.js` | PCM 分帧样点守恒、int16 截断、revision 单调、全屏挂载点、显示模式 | **24 项通过** |
| `node tests/test_ws_protocol.js` | 用扩展真实的 `PcmFramer`+`floatToInt16` 走真实 WebSocket，校验帧格式与事件契约 | **7 项通过** |

刻意做这两个测试的原因：掉帧、revision 回跳、挂载点选错在浏览器里只表现为"字幕偶尔怪一下"，
几乎无法复现；在 Node 里是确定的失败。写这套测试时它确实抓到了一个错误（那次错的是测试自己的算术）。

**仍需真机验收**：安装步骤与 10 项人工检查清单见 `extension/README.md`。

**进度：M0 ✓ M1 ✓ 换引擎判定 ✓ M2 ✓ M3 ✓ M4 ✓（待真机验收）**，剩 M5（opus-mt 翻译）、M6（浏览器端到端）。

## 下一步（最新）

1. **M6 真机验收**：按 `extension/README.md` 的清单逐条过，重点是第 2 项（全屏下字幕可见）
   和第 7 项（刷新后设置保留）——这两项正是原来坏掉的地方。
2. **M5**：`opus-mt-ja-zh` + ctranslate2 直连翻译，独立 worker 不阻塞识别；
   翻译结果作为同一 segment 的更高 `revision` 回传，渲染端已经支持这条路径
   （`tests/test_extension_logic.js` 里有一条专门测它）。

## M5 攻关记录：翻译模型这条线暂时没走通

详细数据见 `BENCHMARK_RESULTS.md` 第 12 节。这里只记结论和教训。

**代码是完整的**：`app/translate/`（协议 + NLLB/CT2 实现）、服务端独立翻译线程、
译文按 `segment_id` + 更高 `revision` 回传（原文先显示、译文稍后补上），
渲染端与单元测试都已覆盖。**换模型只需改 catalog 里的目录名，不必再动管线。**

**但模型这条线被卡住了**，逐条排除如下：

| 路线 | 结果 |
|---|---|
| `Helsinki-NLP/opus-mt-ja-zh` | **模型不存在**（只有反向的 zh-ja），也没有 `opus-mt-mul-zh` |
| 两份预转换 NLLB CT2 int8 仓库 | 退化解码；逐文件 sha256 校验一致，**不是下载损坏** |
| 本地转换 NLLB（自己下的 2.35GB fp32） | 同样失效：`target_prefix` 被完全忽略 |
| 重映射词表修补现成模型 | 偏移确认存在（`sv[i]==spm[i-1]` 19996/19996），但修不了 |
| ja→en→zh 双重中转 | 质量不可用（英文自身破碎并复读） |

**决定性对照**：同一个 CT2 4.8.1 转换出来的 **Marian 模型工作正常**，
所以 CT2 本体和调用方式都没问题——是 NLLB 转换与 CT2 4.8.1 之间的不兼容。
最硬的证据：换任何目标语言（zh/fr/de/ja/ru），模型都输出同一段英文回声。

**这一轮我自己犯的两个错，都值得记住**：

1. **推荐 `opus-mt-ja-zh` 之前没有先确认它存在**。这和当年 Argos `ja→en` 下载失败是
   同一类错误——先假设、再动手。现在 catalog 里每一项都有 `status` 字段记录实测结论。
2. 一开始把两份预转换模型判成"下载损坏"，实际 sha256 完全一致。
   **"换个独立来源做对照"才是定位这类问题的正确手法**，而不是反复重下。

**顺带修好的两件事**：

- `tools/setup_models.py` 现在支持**断点续传**。这是必需的：`huggingface_hub` 的 xet
  传输在这个网络下走了一小时、到 86% 报 `CAS Client Error`，且没有可用残留；
  换用自己的下载器后，2.35GB 中途断了两次都成功续上。
- 解码必须加 `repetition_penalty` / `no_repeat_ngram_size`：不加会复读到上限才停
  （单句 1.65s→0.85s，输出从不可用变正常）。这和 M0 在 Whisper 上发现的
  temperature 回退是同一类问题。

**环境坑**：sentencepiece 的 C++ 层在 Windows 上打不开非 ASCII 路径
（本项目在 `C:\text\实验\` 下），绝对路径报 `NOT_FOUND`；已改为从字节加载。

**剩余可试的路线**（见 `BENCHMARK_RESULTS.md` 12.7）：小参数量指令模型（不依赖语言标记机制，
而语言标记正是坏掉的那环）／降级 CTranslate2 再试 NLLB／先只做原文。

**进度：M0 ✓ M1 ✓ 换引擎判定 ✓ M2 ✓ M3 ✓ M4 ✓（待真机验收）M5 代码就绪、模型未走通 M6 待做。**

## M5 完成：换成指令模型后跑通

NLLB 那条线判定不可用之后（详见上一节与 `BENCHMARK_RESULTS.md` 12.6），
改用**小参数量指令模型** `Qwen2.5-0.5B-Instruct`：它用自然语言 prompt 表达翻译意图，
**不经过被证实坏掉的语言标记机制**。

**两种后端实测对比**（8 句日语，6 线程）：

| 后端 | 权重 | 单句中位 | p95 | 加载+预热 |
|---|---|---|---|---|
| torch（fp32） | 942 MB | 2163 ms | 4142 ms | 95.4 s |
| **ct2（int8，默认）** | **473 MB** | **893 ms** | **1561 ms** | **19.4 s** |

**真实会话端到端**（1.0x 推流，服务开着 `--translate`）：

- **中文出现在说完后 0.67–0.88 秒**，双字幕整体仍在 **1.5 秒**目标内。
- 翻译只对定稿触发、跑在独立线程，**原文延迟完全没被拖慢**
  （partial p50 0.012s、final p50 0.5s，与不开翻译时一致）。
- 译文作为同一 `segment_id` 的更高 `revision` 回传，渲染端原地补上。

**质量说实话**：6 句里 5 句可用，1 句生硬（`気をつけた方がよいわよ。` → "你一定要注意的。"）。
0.5B 就是这个水平，够看懂大意但不精致。换 1.5B 可提升，且只需改
`app/models_catalog.py` 与 `app/translate/factory.py` 的目录名，管线不用动。

> **⚠ 本段末尾的"换 1.5B 可提升"后来被实测推翻，见下面 09-28 第 6 节**：
> 0.5B + 上下文时 `cpu_ratio` 已到 0.689，换 1.5B 会超过 1.0，在这台机器上跑不动。

新增/改动：
- `app/translate/instruct_local.py`（两种后端 + prompt + 输出清洗）
- `app/translate/factory.py`（服务与诊断共用同一构造路径）
- `app/server.py` 增加 `--translate-engine` / `--translate-backend`
- `tools/diag_translate_cost.py` 支持 instruct/nllb/argos 三种后端
- `tools/ws_client_test.py` 现在会显示译文，并把译文事件标成 `TRANSL`

**进度：M0 ✓ M1 ✓ 换引擎判定 ✓ M2 ✓ M3 ✓ M4 ✓（待真机验收）M5 ✓ M6 待做。**

## 2026-09-28 扩展真机联调（6 个提交，全部由用户实测驱动）

这一段是项目里第一次真正"在浏览器里用"，暴露出来的几乎全是**联调与可见性**问题，
而不是算法问题。按发现顺序记下来，因为每一个都花了来回。

### 1. popup 永远显示"未连接本地服务"（`9b04c8e`）

**根因是设计缺陷，不是真的连不上。** popup 读的是 offscreen 写的 `captureStatus`，
而 offscreen 文档只有点过「开始捕获」之后才会被创建 —— 在那之前那个状态根本不存在，
于是服务跑得好好的也一直显示未连接。「检查本地服务」按钮更糟：它把消息转发给
offscreen，而 offscreen 不存在时那条消息**无人应答**，按钮形同虚设。

修复：新增 `Shared.probeService()`，popup **自己**连一次 WebSocket 发 ping，
**独立于采集**。
顺带抓到服务端一个真 bug：`ping` 回复漏了 `engine`/`model` 字段（只有 `start` 回复里有），
所以即便连上也会显示成 `(? / ?)`。
新增 `tests/test_probe_service.js`（4 项）专门守这个坑 —— "缺少 engine 字段"那条
当场抓到了上述服务端 bug。

### 2. `.bat` 里的中文把语句拆碎（`1f7580a`）

第一版启动脚本把中文提示直接写进 `.bat`。cmd.exe 按当前代码页**逐字节**解析批处理，
于是它把 `echo` 的中文当成命令去执行，stderr 全是"不是内部或外部命令"。
改为**纯 ASCII 薄壳** + `tools/run_service.py`（含模型/端口预检，中文都在 Python 里）。
同时解决了"必须在项目目录下运行否则报 `No module named 'app'`"这个坑。

### 3. "点了开始捕获却毫无反应"（`c6ce375`）

**用户给的服务端日志是决定性的**：只有 4 次「客户端已连接」且 `audio_seconds` 全是 0、
**没有任何「采集开始」**。offscreen 只在 `getUserMedia` 和 `addModule` 都成功后才去连
WebSocket，所以这说明失败发生在那之前。

两个 bug 叠加才导致完全无声：

- **竞态**：`chrome.offscreen.createDocument()` 返回时文档只是"被创建"，
  里面的 JS 还没执行完、`onMessage` 还没注册。紧接着发的 `offscreen-start`
  无人接收 → promise reject → 被 `.catch(() => {})` **吞掉**。
  采集从未开始，而调用方以为成功了。
  修复：`sendToOffscreen()` 带重试（30 × 100ms）直到接收端就绪。
- **错误不可见**：offscreen 里只有 `getUserMedia` 一步有错误处理，而且失败后
  只记状态就 `return`；`AudioContext`/`addModule`/`AudioWorkletNode` 三步**完全没有保护**。
  更糟的是 popup 的 `render()` **每秒重写状态框**，就算写进了报错也会在 1 秒内被覆盖 ——
  用户于是"什么也看不到"。
  修复：六步各自具名（①获取标签页音频 … ⑥连接本地服务），失败时把**是哪一步**
  写进状态并 `rethrow`；popup 用 `lastError` 保存，由 `render()` 一并画出。

### 4. service worker 从来没有加载过 `shared.js`（`39a3248`）

manifest 的 `content_scripts` 只管页面，`popup.html`/`offscreen.html` 各自用
`<script>` 加载，而 `background.js` **只加载自己** → `self.SubtitleShared` 在 SW 里是
`undefined`。修复：`importScripts('shared.js')`。

同时把状态上报改成**永不抛错**的存储封装（`session` → `local` → 内存 三级兜底）：
用户报的 `TypeError: Cannot read properties of undefined (reading 'session')` 之所以能把
整个采集带崩，是因为它发生在上报路径上 —— **遥测不该有能力弄坏被测功能**，这是设计错误。
新增 8 项 `createStore` 契约测试，含"访问 `chrome.storage` 本身抛异常时也不能炸"
（就是这次的故障形态）。

### 5. 服务端诊断日志（`a3c78d8`）

新增 `采集开始：language=xx` 与"3 秒没收到音频"警告，让服务端日志能直接区分
三种情况：只开了 popup 没点捕获／采集了但没音频／消息根本没到服务端。已实测验证
（纯探测 6 次都不打「采集开始」；故意发 `start` 不发音频则立刻警告）。

### 6. 翻译质量：上下文才是主要矛盾（`a7df70b`）

用户反馈"有些句子翻译出来语境和上下文明显不符合" → 指向**孤立翻译**这个结构性缺陷
（日语大量省略主语，「にしても」「お兄さん」的含义取决于前一句）。

- 第一版把上文写成「原文 → 译文」范例、末尾留「本次：xxx →」让模型接着写，
  **0.5B 直接把它当接龙**：要么抄上一句译文（"那当然了，短数据就先出发吧"），
  要么顺着输出日语（"注意しておいた方がいいわよ"）。
- 改成把上文放进 **system prompt 当背景说明**（并写明"不要翻译它们"）后正常，
  6 句里 3–4 句明显变好。

**代价（如实记录）**：原文定稿延迟 0.46 → **0.97 s**，`cpu_ratio` 0.386 → **0.689**，
译文约 2.4 s 后到达。

**重要结论**：换 1.5B 约需 3 倍翻译算力 → `cpu_ratio` 会**超过 1.0**，
在 i5-13500H 纯 CPU 上**不可行**。**0.5B + 上下文基本就是这台机器的上限。**
（此前 WORKLOG 里"换 1.5B 可提升"的说法据此作废。）

也修了 `tools/ws_client_test.py` 一个误导指标：译文事件也是 `is_final=True`，
原来被算进"原文定稿延迟"，把 0.97 秒虚报成 1.98 秒，看起来像识别变慢了。

### 7. ⚠ 第 6 条被真实使用推翻：上下文和我的新提示词都是负面的

用户又跑了一遍，反馈：**"引进上下文后翻译效果受到完全负面的影响，还不如之前。"**

**我第 6 条的评估方法本身有缺陷**：离线 A/B 用的是**干净参考文本**，
而真实运行里喂进翻译的是 **ASR 输出**（含误识别、错字、被 VAD 切碎的片段）。
上下文在这种输入上会**把错误传播下去**；孤立翻译至少保证每句互不牵连。

改用**真实会话的 ASR 原文**重做三路对比，把两个变量分开：

| 原文（ASR 实际输出） | **A plain 无上文** | B instruction 无上文 | C instruction 带上文 |
|---|---|---|---|
| `この辺も最近は治安が悪くなってきているから。` | **这边**最近治安也变差了 ✓ | 这**条边**最近治安不好 ✗ | 这**条路**最近治安不好 ✗ |
| `にしても本当にびっくりしたわ。` | 我也非常惊讶 ✓ | 我也非常惊讶 | 我也震惊了 |
| `気をつけた方がいいわよ。` | 你一定要注意的 | 你还是要注意的 | 小心点吧 ✓ |
| `それじゃあ短いデータと行きましょうか。` | 那您打算用短数据吗？ ✓ | 那您们要短数据吗？ ✗ | 那您们要短数据吗？ ✗ |
| **6 句总耗时** | **1.79 s** | 1.87 s | 2.74 s |

**结论比"上下文有害"更严重：我改的那版提示词本身就是负面的。**

- **A→B 隔离出提示词的影响**：`この辺` 在 A 里正确译成"这边"，在我改的长提示词里
  变成"条边"——**加长提示词让 0.5B 更字面化**。
- **B→C 隔离出上下文的影响**：没有稳定改善，耗时 +47%。
- **A 全面最好且最快。**

**0.5B 对提示词措辞极其敏感，"写得更清楚"不等于"效果更好"。**
这与第 13.1 节记的接龙崩法是同一个根因：小模型会被提示词的形式带偏。

**处置**：提示词默认回到最初的 `plain`（逐字保留，不带上下文）；
`--translate-context` 默认从 2 改为 **0**；
新增 `--translate-style {plain,instruction,completion}` 供实验。
两个失败风格保留在代码里——它们记录了"什么写法会坏事"，比删掉有价值。

恢复默认后的端到端实测（同一段样本）：

| 指标 | 带上下文 | **恢复默认** |
|---|---|---|
| 原文定稿延迟 p50 | 0.97 s | **0.45 s** |
| 译文到达 p50 | 2.37 s | **0.82 s** |
| `この辺` 的译法 | 「这条路」✗ | **「这边」** ✓ |
| cpu_ratio | 0.689 | **0.19** |

**方法上的教训（比结论更重要）**：
**离线 A/B 必须用真实链路产生的输入，不能用人工整理的干净样本。**
我用 6 句干净参考文本得出"上下文更好"，与真实使用完全相反——
被评测的那份输入本身就不代表线上分布。

### 当前状态

**M0 ✓ M1 ✓ 换引擎判定 ✓ M2 ✓ M3 ✓ M4 ✓ M5 ✓（翻译＝0.5B + plain 提示词，已验证；09-29 起默认换成 Hy-MT2，见待办第 2 条）M6 进行中**
（用户已确认浏览器里能看到字幕。翻译的默认配置已回到用户实测认可的基线；
"上下文"与"新提示词"两条改进路线均被真实使用否掉，代码保留但默认关闭。）

## 2026-09-29 长句分句翻译研究（结论：收益很小，开关保留、默认关闭）

**问题**：长句要等说完才出中文（≥4s 的句子开口→中文中位 8.4s），翻译本身只占约 2s。

**1. 离线模拟（`tools/sim_partial_translate.py`，结果在 `runs/partial_study/review.md`）**
用 5 段实测会话里的 partial 模拟"说到一半"，在稳定前缀上找切点，切出来的片段用 Hy-MT2 翻译，看比整句译文早多少。
86 句定稿中有 15 句长句：
- 句中出现「。？！」，或者句末助词后跟「、」/空格（两句被 VAD 粘在一起）：只有 2 处，提前量中位 3.64s，价值基本都在这里；
- 稳定 partial 以句末助词或「？」结尾：14 处，但只能提前 0.1–0.8s；
- 从句边界（けど/から/て + 、）：1 处，而且片段译文有语义跑偏的风险；7 句长句找不到任何切点。
→ **放弃"从句切片出临时译文"**，改做两个更保守的方向，都只改定稿时机、不产生临时译文。

**2. 提前定稿 `--early-final`**：partial 里发现句中的句末边界，且边界两侧 token 时间戳间隔 ≥0.2s、
边界后 2 个字已稳定时，用时间戳把前半句的音频切出来单独定稿并翻译（`find_cut`）。
为此 `AsrResult` 新增 `tokens`（只有 SenseVoice 填，来自 sherpa-onnx 的逐 token 时间戳）。

**3. 自适应静音阈值 `--adaptive-silence S`**：最近一次 partial 以句末助词/问号/感叹号结尾时，
尾部静音达到 S 秒就定稿，不必等满 0.35s（SenseVoice 自动补的「。」不算句末）。
坑：默认 VAD 参数（min_silence 350ms）会把 0.35s 以内的停顿并进同一个语音区间，短停顿根本看不见。
因此这个开关另用一套细粒度 VAD（`FINE_VAD_OPTIONS`，100ms / pad 30ms），只看缓冲区最后 1.5s。

**台架**（`tools/compare_early.py`：对每 0.1s 的语音计算"这一刻的话多久后定稿"；5 段 × 3 轮）：

| 组 | 定稿句 | 等待中位 | p90 | 最大 | CER |
|---|---|---|---|---|---|
| 基线 | 61 | 1.76 | 3.81 | 6.53 | 0.0896 |
| 提前定稿 | 63 | 1.71 | 3.58 | 6.53 | 0.0752 |
| 自适应 0.2s | 62 | 1.68 | 3.72 | 6.47 | 0.0885 |
| 自适应 0.12s | 66 | 1.57 | 3.46 | 6.48 | 0.0976 |
| 0.2s + 提前定稿 | 66 | 1.62 | 3.55 | 6.48 | 0.0987 |

CER 的差异落在噪声范围内（每段约 ±0.02）。
- 提前定稿在全集只触发 1 次：0930「そういえば言ってたものね。」提前 1.2–1.3s 定稿。提前量有结构性上限，
  因为要等下一句开头稳定才能确认边界。0500「しまうよ。うな場面」是 ASR 错误、没有时间间隔，被正确拒绝。
- 自适应阈值触发 25–28 次，但每句只比 0.35s 早约 0.1–0.15s。代价是切出碎句：SenseVoice 常给说到一半的词补「？」，
  0.2s 时「よかったら」被切成「よかっ？」「たら？」，0.12s 时 0030 的 CER 从 0.046 升到 0.099。
  它也切不开 0930 那两句（中间停顿太短），能切开的仍是提前定稿。

**结论**：长句要等主要是因为句子本身长，定稿时机不是瓶颈。0.35s 的静音阈值已经够短，
"两句被粘在一起"在评测集里只有 1 次。两个开关保留、默认关闭，`tools/run_service.py` 可直接透传，
等用户在浏览器里实测后再决定。若要继续做自适应阈值，先收紧「？」规则（比如至少 4 个字才算）。

## 2026-09-30 扩展实验开关 + 识别上限测试

**实验开关进扩展**：popup 新增「实验选项」，里面有「粘连句提前定稿」「句末短静音定稿」两个勾选。
它们随 `start` 消息交给服务端（`Session.apply_options`），每个会话单独生效，不用重启服务；命令行参数仍可作为默认值。

**修了一个日志 bug**：popup 的"检查本地服务"也是一次连接，会话结束时会用空日志覆盖 `--log`。
用户 09-29 晚上的双开关实测日志就是这样丢的，只剩一个 0 秒 header。
现在没收到音频的会话不写日志；同一路径已存在时依次写 `_2`、`_3`……

**第 0 步上限测试（`tools/asr_ceiling.py`，结果在 `runs/asr_ceiling/review.md`）**：
把评测回放的 59 句 ASR 定稿对齐到参考原文，两边都用 Hy-MT2 翻译，再比较两份译文（chrF，1 = 相同）。
- 原文全对的 39 句：译文 chrF 中位 1.00；
- 原文有错的 20 句：chrF 中位 0.20，其中 17 句译文和参考译文差异很大（<0.5）。
→ **大约三分之一的句子，翻译质量被识别错误拖累，而且拖累得很重**，值得在识别上投入。
注意参考文本是 whisper large/small 交叉裁定的 consensus，不是人工听写。

**顺带发现的零成本改进**：SenseVoice 会在日语词之间插空格，翻译模型会把空格当断句
（「脇 が甘い 男性 が」被译成"周围变得甜美了"）。含空格的 6 句去掉空格后，有 3 句和参考译文完全一致。
现在定稿和 partial 都会去掉中日文字符之间的空格（`join_cjk`），英文空格保留。

**候选日语识别模型**（web 调研，未实测，数字来自搜索摘要）：
1. `nvidia/parakeet-tdt_ctc-0.6b-ja`：JSUT CER 约 6.4%，有 sherpa-onnx int8 官方导出，没有 Whisper 那样的固定调用开销；
2. `reazon-research/reazonspeech-k2-v2`：159M Zipformer，sherpa-onnx 原生支持，最便宜；
3. `litagin/anime-whisper`：用 galgame/动画配音数据微调，和 ASMR 的气声、情绪化语音最接近，但每次调用成本高，适合只在定稿时重识别；
4. `Qwen3-ASR-0.6B`（2026-01）：通用能力强，有 GGUF，CPU 速度待测。

**实测 1 和 2（`--engine sherpa`，`app/asr/sherpa_offline_engine.py`，5 段 × 3 轮）**：

| 模型 | CER | cpu_ratio | p95 最大 | final_p95 最大 | 译文 chrF 中位 / 均值 | chrF<0.5 |
|---|---|---|---|---|---|---|
| SenseVoice-2024（现用） | 0.0896 | 0.09 | 0.40s | 0.56s | 0.76 / 0.63 | 25/59 |
| Parakeet-ja（原始输出） | 0.1027 | 0.29 | 0.63s | 1.14s | — | — |
| Parakeet-ja（清洗后） | **0.066** | 0.30 | 1.01s | 2.03s | **0.86 / 0.69** | 22/59 |
| ReazonSpeech-k2 | 0.1836 | 0.19 | 0.64s | 1.02s | — | — |

- ReazonSpeech-k2 字错率是 SenseVoice 的两倍，且完全不出标点，放弃。
- Parakeet 听得更准（「あながち」「眉唾」「好都合」「抵抗」SenseVoice 都错了），但原始输出有三个问题，都在引擎适配层清洗（`clean_text`）：
  笑声会被转成「フフフフ」「アハ」（删掉）；句中不出标点，用空格隔短语（空格前是句末助词就换「。」，否则换「、」）；
  句末没有「。」（补上）。最后一条影响很大：识别全对的句子仅因少了句号，Hy-MT 的译文就变了
  （「そんなに驚くことかしら」→"真需要这么惊讶吗" vs 有句号时"有什么好惊讶的。"），清洗前译文 chrF 中位反而掉到 0.64。
- 代价：单次识别约是 SenseVoice 的 3 倍，final_p95 最大从 0.56s 涨到 2.03s（0230/0930 两段），这部分延迟要实机确认能否接受。
- 下载：GitHub release 直连只有几 KB/s；Parakeet 用 hf-mirror 的 `csukuangfj/` 仓库，
  ReazonSpeech-k2 在 HF 上没有官方 sherpa 导出，用的是第三方重传 `DeL-TaiseiOzaki/`（文件名与官方包一致）。

**默认写会话日志**：`启动字幕服务.bat` 现在每次会话写一份 `runs/live/live_<时间>.jsonl`，只保留最近 20 份
（`--log-dir`，`--no-log` 关闭，`--log` 指定固定文件时优先）。只存文本和时间，一小时约 1.6MB，会话结束时写一次。
`runs/live/` 不入库（含真实观看内容）。

**本地视频（扩展 v0.3.0）**：字幕脚本增加 `file:///*` 匹配，用 Chrome 直接打开本地 mp4/mp3 即可捕获翻译；
需要用户在 `chrome://extensions` 打开「允许访问文件网址」，popup 检测到没开时会提示。
这种页面全屏的是 `<video>` 本身，覆盖层无法挂进去，所以改用 popover 顶层显示，每次全屏切换后重新 `showPopover()`。
已用 headless Chrome 验证：video 全屏后 popover 显示在视频之上。

## 2026-10-01 延迟排查 + 一键启动 + 弹窗重做

**Parakeet 延迟**（`tools/live_bench.py`：真起服务、按 1x 推流、按客户端收到的墙钟算，含翻译抢 CPU）：

| 配置 | 草稿间隔 p50 | 定稿原文 p50 / p90 | 中文 p50 / p90 |
|---|---|---|---|
| SenseVoice | 0.52s | 0.47 / 0.73s | 1.22 / 1.75s |
| Parakeet（串行，默认） | 0.52s | 0.52 / 0.64s | 1.04 / 1.27s |
| Parakeet + 独立定稿线程 | 0.59s | 0.76 / 2.56s | 1.87 / 3.89s |
| 混合 + 独立定稿线程 | 0.56s | 1.33 / 1.95s | 2.49 / 3.48s |

- 独立定稿线程（`--async-finals`）反而更慢：两路 ONNX 推理并发抢同一批核。默认关闭。
- 混合模式（`--final-engine`，草稿 SenseVoice、定稿 Parakeet）没有收益：定稿仍要解码整句。保留为实验选项。
- Parakeet 从 5 线程加到 8 线程几乎没变化（10s 音频 0.70 → 0.63s）。
- 用户实测"延迟难受"的那次没有日志（服务窗口被直接关掉，走不到写日志）。现在边跑边写 `.partial`，强杀也能留下。

**一键启动（Chrome Native Messaging）**：`安装一键启动.bat` 写 HKCU 注册表 + `runs/native_host/local.live_subtitles.json`，
扩展 ID 由扩展路径算出。宿主 `tools/native_host.py` 只接受 status/start/restart/stop/log 五个指令，
后台用 pythonw 无窗口启动服务，输出写 `runs/service.log`，默认闲置 30 分钟自动退出（`--idle-exit`）。
手动用 .bat 开的服务，宿主不会替用户关或重启。只有项目文件夹搬家时需要重跑安装脚本。

**弹窗重做（扩展 v0.4.0）**：去掉"检查本地服务"按钮，状态每 2 秒自动探测；一个主按钮按状态切换
（启动服务并开始 / 开始 / 停止）；识别模型下拉框（Parakeet 默认 / SenseVoice / 混合），换模型时自动重启服务；
高级区放实验开关、闲置时长、查看服务日志（`log.html`）、停止服务。跟随系统深浅色，主题色蓝，工具栏角标显示 ON。

## 2026-10-01 英语支持

**评测集**：用户给的英语 ASMR 角色扮演视频（YouTube PJTziu8VFRE，27.5 分钟，单个女声）切 5 段 × 60 秒：
前段最密（1:00）、全片最密（8:00）、中等（12:00）、最稀疏最轻（20:00，19s 语音、-27dB）、结尾稀疏（26:00）。
参考文本 whisper large-v3 为主干，与 small、YouTube 自动字幕三方裁定（large/small 一致度 0.90–0.975）。
用户另外下了 B 站内嵌中字版（人工翻译，音轨与 YouTube 版只差 0.03s），用 Windows 自带 OCR 逐帧提取出 377 条中文字幕，
作为译文的人工参考（`tools/ocr_subtitles.py`）。

**下载**：yt-dlp 要用浏览器 cookies 过登录校验（用完即删），2026.07.04 版拿不到格式，升到 2026.08.19 + Node 做签名校验。

**英语识别（5 段 × 3 轮，WER）**：

| 模型 | WER | cpu_ratio | final_p95 最大 | 句末无标点 |
|---|---|---|---|---|
| SenseVoice-2024 | 0.102 | 0.17 | 0.66s | 0% |
| **Parakeet unified en 0.6B**（2026-04） | **0.073** | 0.36 | 1.45s | 47% |
| Parakeet tdt-0.6b-v2（2025-04） | 0.105 | 0.37 | 1.23s | 20% |

候选来自调研（Open ASR Leaderboard + sherpa-onnx 现成导出）：Qwen3-ASR、FunASR-Nano 是 LLM 解码器，每 0.5s 重解太慢；
Whisper 系有 30s 补齐成本；canary-1b、Kyutai 没有 sherpa 导出。Parakeet v2 在轻声稀疏段错得最多（0.20 / 0.18），放弃。

**整条链路译文对人工译文**（`tools/translation_vs_human.py`，整段 chrF）：SenseVoice 0.259、unified 0.264、v2 0.259。
差异不大：人工译文是意译（"Ms. Arlington" 译作"阿灵顿老师"，我们是"阿林顿小姐"），chrF 绝对值低是正常的。
逐句看我们的译文意思基本都对，主要问题是碎句被分开翻译。

**英语文本规则（都对真实输出验证过）**：语气词整句过滤（um/uh/hmm/uh-huh/haha，yeah/okay/no/oh no 保留）；
SenseVoice 标点后漏空格（"doing,huh"）补上，不碰 1,000、e.g.、U.S.；Parakeet 句末补句号
（离线 A/B 对人工译文 0.264 → 0.269，不会更差）。日语那套清洗只对日语。

**服务**：`EnginePool` 按会话语言选识别引擎，start 消息里的语言终于生效；英语默认 Parakeet unified
（`--en-engine/--en-model`，模型缺失自动退回 SenseVoice），启动后后台预加载（否则首次英语会话现场加载约 18 秒，
开头十几秒的字幕全部延后）。弹窗识别下拉框按语言列选项，日语、英语的选择分开记。扩展 v0.5.0。

## 2026-10-01 用户实测反馈修复 + 网页内浮窗

- **一键启动被误判为手动启动**：venv 的 pythonw.exe 是转发器，会再拉起真正的解释器后退出，记下的 pid 很快就没了，
  弹窗以为"端口被占但不是我们开的"。宿主改为按端口查监听进程并核对命令行（带 --idle-exit 且是本项目的 run_service.py）。
  同时加了启动锁：日志显示用户那次有两个实例同时启动、抢同一个端口。
- **"英语启动慢"其实是 bug**：扩展发完 start 立刻推音频，不等换引擎；开头的音频进了日语流水线，
  用户 15:36 那次整个英语视频都是日语那套流水线在识别（计时起点还被重置，延迟出现负数）。现在 start 处理期间先缓存音频。
  英语 Parakeet 单独加载只要 2.8s（日语 4.1s）。
- **换标签页没字幕**：捕获绑定在点"开始"的那一页，换页后还在捕获旧页。现在弹窗/浮窗在别的页上会显示
  "正在捕获另一个标签页"，主按钮变成"切换到这个标签页"，一键停旧开新。自动跟随做不到（每次捕获都需要用户操作）。
- **网页内浮窗**（扩展 v0.6.0，`extension/panel.js`）：只在字幕进行中的标签页出现（用户选定）；收起是半透明圆钮，
  展开是嵌入的弹窗页（closed shadow root 隔离页面样式）；可拖动、可调宽、记位置，全屏时挂进全屏元素，Alt+S 显示/隐藏。
  content script 读不到 storage.session，捕获状态由 background 转告。已用 Edge headless 加载真实扩展验证显示/隐藏、拖动、全屏挂载。
- **两个标签页同时翻译**：不做。两路 ONNX 并发抢核（见上方独立定稿线程的实测），人也只能看一个视频。

## 2026-10-01 英语延迟参数复测（Claude 中断后的收尾）

补齐此前未提交的修复记录：扩展以会话号隔离字幕，切换会话清空旧字幕并丢弃旧会话事件；停止捕获时主动清屏。弹窗顶部新增「浮窗」开关，隐藏后可重新打开并展开面板。

实时台架现在等待 `start` 返回 `listening` 后才开始计时，排除模型加载耗时；音频结束后等待 20 秒，并记录已定稿但尚未收到译文的句子。保存逐句事件，明确汇总口径是「各片段分位数的平均」，不是合并所有句子后再取分位数。Windows 下结束台架会清理自己启动的整个服务进程树；启动前检查评测端口，避免误连已有服务。旧台架可能混入加载耗时、截掉迟到译文，其结果不与新台架直接横比。

同机顺序跑默认 A、候选 A、候选 B、默认 B，每组使用 `en_asmr_0100` 和 `en_asmr_0800` 两段。测量时停止原来的字幕服务，只运行一组台架；其他桌面程序仍在运行，绝对延迟受负载和电源状态影响。看到两个 Python PID 不等于双开服务，Windows venv 启动器及其子解释器会显示相同命令行。

| 配置 | 中文 p50 A / B | 中文 p90 A / B | 定稿 p50 A / B |
|---|---|---|---|
| 原默认：5 线程、0.5s 草稿 | 5.350 / 6.226s | 8.598 / 8.300s | 2.039 / 2.922s |
| 候选：3 线程、0.75s 草稿 | 3.936 / 4.275s | 6.185 / 5.207s | 2.316 / 2.823s |

两轮平均中文 p50 改善约 29%，p90 改善约 33%。候选的两轮、四段共 34 个已定稿句子全部收到译文；这不代表裁切边界处未定稿的句子已完整处理。逐段最新识别文本与 consensus 参考计算的 WER 均值：原默认约 0.0533，候选约 0.0532，未显示整体退化；样本仅两个片段，不代表全量准确率。

因此英语 Parakeet 现在默认使用最多 3 个识别线程和 0.75s 草稿刷新；日语和英语 SenseVoice 保持原默认值。`--threads` / `--partial-step` 显式指定时仍覆盖所有语言，`--en-threads` / `--en-partial-step` 仅覆盖英语且优先级更高。复测结果与逐句事件见 `runs/live_bench/en_clean_*.json`。在新代码上复现原配置需显式传 `--threads 5 --partial-step 0.5`。

实际新默认入口再测两段（`en_clean_new_default.json`）：中文 p50 1.166s、p90 1.696s，定稿 p50 0.550s。此轮机器明显更快，不能把它与此前较忙时的原默认直接比较来宣称提升倍数。29 条非空定稿中 28 条收到非空译文；唯一缺失是识别碎片 `D.`，单独调用 Hy-MT2 也返回空字符串，并非等待不够。现有翻译器会抑制原样返回的输出，本次保留该行为，碎句问题仍待解决。

验证：Python 单元测试 49 项、扩展逻辑测试 39 项、真实服务探测 4 项、音频协议集成测试 9 项通过；新测试覆盖语言切换、英语参数优先级、SenseVoice 回退、台架就绪握手和译文缺失统计。完成后通过原有 Native Messaging 宿主恢复后台服务（日语和英语均选 Parakeet，闲置 30 分钟退出）。

## 2026-10-02 v0.6.1 发布整理

- 将会话隔离、浮窗找回、英语延迟优化及其评测依据一起整理发布。旧 WebSocket 的迟到回调现在先检查连接身份，避免把旧事件标为新会话，或把新连接状态改成已断开。
- 启动器补齐 README 已介绍的 `--no-preload` 透传；更新安装顺序（先装预编译翻译 wheel）、Python/Node 要求、浮窗操作、更新步骤、语言参数和双语评测集说明。
- 本轮验证：51 项 Python 单元测试、39 项扩展逻辑测试、4 项旧连接隔离检查通过。此前的真实服务探测和音频协议测试见上一节，本轮未重跑完整性能台架和全新机器安装。
- 称呼一致性仍处于设计阶段，没有实现或默认启用；当前方向是会话内自动记忆、只约束命中的称呼、优先控制额外延迟，不依赖用户管理预设。

## 2026-10-02 称呼一致性实验（v0.7.0）

完成提示词 A/B 后否决该路线：命中句短提示额外耗时 p50 173ms，且会抄出约束文字。改用默认关闭、会话内的保守局部后处理，
不改生产提示词，不增加推理调用。14 个边界样例通过，后处理 p95 0.0129ms；20 份真实会话共 1,168 条译文修正数为 0，
尚不能证明实际改善率。新增开关、清除按钮、页面/语言/会话隔离；排队和在途旧结果不能重新污染已清除的记忆。
完整范围、复现命令和指标解释见 [ADDRESS_CONSISTENCY.md](ADDRESS_CONSISTENCY.md)。

## 待办与已知缺口

1. **SenseVoice 2025-09-09 已否决（09-29，评测集 `runs/eval/sv2025`）**：它的 README 写明转自
   `ASLP-lab/WSYue-ASR/sensevoice_small_yue`，是**粤语微调版**，不是 2024 版的通用升级。
   即使指定 `language=ja`，它也输出 `<|yue|>` 和汉字（官方 ja.wav 同样如此），评测集 CER 0.82，而 2024 版为 0.091。
   默认模型保持 `sensevoice-2024`。识别错误（`短いデータと行きましょうか`、稀疏片段 0700 的 CER 0.22）仍待解决，
   评测集见 `eval/README.md`。
   - **09-29 语气词过滤（默认开启，`--keep-fillers` 可关）**：只由「う/へへ/ふふふ」这类音节组成的整句 final 会被置空，原文记入 `detail.filler`。
     A/B 每组各跑 3 轮：CER 0.093 → 0.071（`runs/eval/sv2024_keep` 对比 `sv2024_filler`），其中 0700 从 0.191 降到 0.132，0930 从 0.118 降到 0.056；延迟无变化。
   - **静音阈值不改，保持 0.35**：0.35 / 0.5 / 0.6 各跑 3 次，CER 分别在 0.071–0.078 / 0.069–0.074 / 0.066–0.075 之间，
     差异落在噪声范围内；而 0.6 的 final_p95 最大值达到 2.2s，1.0 更差（CER 0.110）。min_speech 调到 0.25–0.3 反而更差。
   - **台架噪声**：虚拟时钟把真实计算耗时也算了进去，机器忙的时候 VAD 看到的缓冲区不同，切句也跟着变，
     导致单个片段的 CER 单次波动约 ±0.02。**CER 差异小于 0.01 的结论不要看单次结果，要跑 `--repeat 3` 以上。**
   - 仍未解决：「路地→ラジ」「袋小路→ふくく足」属于模型本身听错，与切分无关。
2. **翻译：09-29 默认模型换成 Hy-MT2-1.8B（Q4_K_M GGUF，llama-cpp-python 0.3.35 预编译 CPU wheel）**。
   Qwen2.5-0.5B 保留为备选（`--translate-engine instruct`）。提示词逐字采用官方默认模板，不带上下文。
   - **"译文跟不上"诊断**（`tools/diag_display.py`，按扩展的显示规则重放会话日志）：
     用户那次长时间运行的 Qwen 服务，开口→中文中位 6.1–7.8s，服务内单句翻译 1.3–6.1s。
     2 行显示时每段有 1 句译文到达前就被挤出屏幕，改成 3 行后为 0（`extension/content.js`）。
   - **线程分配不是原因**（`tools/diag_threads.sh`）：新起的服务在 ASR 2–5 / 翻译 4–8 线程的 4 组配置下，
     翻译中位都是 0.33–0.42s。那次慢 5 倍的原因未查明，更像是长时间运行的服务状态问题。
   - **单句对比**（`tools/bench_translate.py`，22 句真实 ASR 定稿）：Qwen 中位 0.33s，Hy-MT2 中位 0.68s；
     Qwen 有 6–7 句严重错误（原样吐回日文、空输出、意思跑偏），Hy-MT2 基本通顺。
   - **用户浏览器实测**（边放视频边用，153s，27 句，`runs/live_hymt.jsonl`）：开口→中文中位 4.93s，
     服务内翻译中位 0.93s、最大 2.17s，没有一句漏看，用户主观反馈"通顺不少、出得更快、不卡"。
   - 代价：常驻内存多约 1.2GB，单句翻译算力约是 Qwen 的 2 倍。
   - **剩余延迟是结构性的**：长句（≥4s）开口→中文中位 8.4s，10s 被强制切断的句子要 14s，翻译只占约 2s，
     其余是在等这句说完。09-29 研究了"长句分句翻译"，结论见下方《长句分句翻译研究》：**收益很小，两个实验开关保留、默认关闭**。
   - 旧经验依然有效：**再动提示词或上下文之前，先用真实 ASR 输入做 A/B**。
   - 09-29 已删除冗余模型（4.7GB）：NLLB 全部 4 份、opus-mt-ja-en 两份、SenseVoice 2025。
     `--translate-engine nllb` 现在会报缺模型；需要时用 `tools/setup_models.py` 重新下载。
3. **ARCHITECTURE_REVIEW.md** 是 09-22 的诊断快照，结论仍成立，但未回填最终达成情况。
4. **英语 → 中文：10-01 完成**，见下方《英语支持》。剩余：英语碎句（Parakeet 把 "Doing / Something" 切成两句、
   各自翻译）还没解决，离线试过"短碎句并入下一句"，对人工译文没有改善（chrF 0.264 → 0.261），没上。
5. **识别仍可提升**：09-30 候选里还没测 anime-whisper（动画/配音数据微调，只适合定稿时重识别）
   和 Qwen3-ASR-0.6B；评测集参考文本仍是 consensus，未经人工听写。


