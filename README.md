# 本地实时字幕（Local Live Subtitles）

在 Chrome 里给正在播放的日语或英语视频实时加中文字幕：识别和翻译都在本机 CPU 上跑，**音频不出本机**。
支持 YouTube 和用 Chrome 直接打开的本地视频/音频文件。

- 识别：日语用 Parakeet-TDT-CTC 0.6B 日语版，英语用 Parakeet unified 0.6B（都可换成更快的 SenseVoice），经 sherpa-onnx 推理
- 翻译：腾讯 Hy-MT2-1.8B（GGUF Q4_K_M，llama.cpp）
- 在 i5-13500H 笔记本较空闲时实测：定稿原文在说完后约 0.5 秒出现，中文约 1 秒（p50）；机器忙时可升至数秒，详见 [实测记录](docs/WORKLOG.md)

> 状态：个人项目，日语 → 中文、英语 → 中文都可日常使用。目前只支持 Windows + Chrome。

## 工作方式

```
Chrome 标签页 ──tabCapture──▶ 扩展 offscreen（16 kHz PCM）──WebSocket──▶ 本地服务 127.0.0.1:8766
                                                                          │ VAD 断句 + 开放段重解码
                                                                          │ 识别（Parakeet / SenseVoice）
                                                                          │ 定稿 → 翻译（Hy-MT2，独立线程）
页面字幕覆盖层 ◀────────── 草稿 / 定稿 / 译文事件（同一句的 revision 单调递增）◀─┘
```

- 说话过程中持续刷新草稿（日语 0.5 秒、英语 Parakeet 0.75 秒），停顿 0.35 秒后定稿，定稿才送去翻译；
- 原文先出现，中文稍后在同一行原地补上，不会闪烁或回跳；
- 服务只监听回环地址，没有鉴权，**不要把端口暴露到局域网**。

## 安装

需要 Windows、Chrome 116+，推荐 Python 3.13（已测试）；实时评测工具需要 Python 3.11+。模型约 2.5GB，运行时内存约 3.5GB。

```powershell
git clone https://github.com/qwt1011/local-live-subtitles.git
cd local-live-subtitles
python -m venv .venv                # 启动脚本依次找 .venv、..\..\.venv、PATH 里的 python
.venv\Scripts\activate

# 先安装预编译 CPU wheel，避免安装 requirements 时尝试本地编译：
python -m pip install llama-cpp-python==0.3.35 --only-binary=llama-cpp-python --extra-index-url https://abetlen.github.io/llama-cpp-python/whl/cpu
python -m pip install -r requirements.txt

# 下载模型（内置镜像列表，按速度逐个尝试）
python tools\setup_models.py --engine parakeet-ja           # 日语识别，约 625MB
python tools\setup_models.py --engine parakeet-en-unified   # 英语识别，约 500MB（不装则英语用 SenseVoice）
python tools\setup_models.py --engine sensevoice-2024       # 多语言识别（备选、更快），约 230MB
python tools\setup_models.py --engine hy-mt2                # 翻译，约 1.1GB
```

加载扩展：`chrome://extensions` → 打开「开发者模式」→「加载已解压的扩展程序」→ 选 `extension` 目录。

**一键启动（推荐）**：双击 `安装一键启动.bat`，然后在 `chrome://extensions` 里重新加载扩展。
之后在扩展弹窗里点「启动服务并开始字幕」即可，服务会在后台无窗口运行，闲置 30 分钟自动退出。
它只写当前用户的一条注册表项（`HKCU\Software\Google\Chrome\NativeMessagingHosts\local.live_subtitles`），
移动项目文件夹后需要重新运行一次；`卸载一键启动.bat` 可以清掉。

不装一键启动也可以：每次先双击 `启动字幕服务.bat`，看到 `流式字幕服务已启动` 后再用扩展。

**本地视频**：把 mp4/mp3 拖进 Chrome 就能用。第一次需要在 `chrome://extensions` → 本扩展「详细信息」里
打开「允许访问文件网址」。

## 使用

扩展弹窗里：

| 控件 | 作用 |
|---|---|
| 主按钮 | 服务没开时一键启动并开始字幕；字幕进行中时变成「停止字幕」 |
| 语言 | 日语 / 英语。切换不用重启服务：两种语言的识别模型服务启动时都会加载 |
| 识别 | 按当前语言列出可选模型：Parakeet（更准，默认）/ SenseVoice（更快）/ 混合（仅日语，实验）。换模型会自动重启服务 |
| 显示 | 原文+中文 / 原文 / 中文 |
| 浮窗 | 在字幕进行中的页面显示可拖动、可调宽的控制面板；关闭后可用此开关找回 |
| 高级 | 实验开关、闲置自动关闭时长、查看服务日志、停止服务 |

字幕可以拖动位置，滚轮调字号，全屏时也会显示。

首次开始字幕仍需点扩展图标；之后可在网页浮窗操作。`Alt+S` 显示/隐藏浮窗，`Alt+Shift+S` 打开扩展弹窗。
要换视频标签页，在目标页面打开扩展弹窗，点「切换到这个标签页」。同一时间捕获一个标签页。

**更新已有安装**：拉取代码后，先停止旧服务，在 `chrome://extensions` 重新加载扩展，刷新视频页，再启动字幕。
项目路径没有变化时不需要重新安装一键启动。

**称呼一致性（实验，默认关闭）**：在「高级」中打开后，下次开始字幕生效。它会记住当前会话中少量明确称呼的译法，
只在原文和译文都能明确对齐时局部统一，不增加模型调用。旁边的回转箭头清除记忆；视频暂停保留记忆，
换页面地址、语言或重新开始字幕时清空。尚未证明真实观看中的改善率，范围和实测见 [称呼一致性实验](docs/ADDRESS_CONSISTENCY.md)。

英语 Parakeet 默认最多 3 个识别线程、0.75 秒草稿间隔，日语保持原配置。手动启动时可仅覆盖英语参数：

```powershell
python tools\run_service.py --en-threads 3 --en-partial-step 0.75
```

这些参数的复测依据和全局覆盖方式见 [开发说明](DEVELOPMENT.md#英语运行参数)。

每次会话的逐句识别、翻译和延迟会写到 `runs/live/`（只保留最近 20 份，不入库），出问题时可以用它复盘。

## 目录

```
app/                 本地服务
  server.py          WebSocket 服务：PCM 进，草稿/定稿/译文事件出
  pipelines/         流式内核（open_utterance.py：VAD 断句 + 开放段重解码）
  asr/               识别引擎：Parakeet / SenseVoice（sherpa-onnx）、Whisper（对照）
  translate/         翻译引擎：Hy-MT2（默认）、Qwen2.5-0.5B（备选）
extension/           Chrome MV3 扩展：采集、弹窗、字幕覆盖层
tools/               启动脚本、一键启动宿主、模型下载、评测与诊断工具
tests/               单元测试与协议测试
eval/                评测集清单与参考文本（音频不入库）
runs/                评测结果（文档里各项数字的出处）
docs/                架构评审、实测记录、开发日志
legacy/              早期原型脚本
```

## 开发

```powershell
python -m unittest discover -s tests -p "test_*.py"  # 不需要模型或音频的 Python 单元测试
node tests\test_extension_logic.js       # 扩展纯逻辑测试
node tests\test_offscreen_session.js     # 切换会话后的旧连接事件隔离
node tests\test_address_navigation.js    # 换页面时清除称呼记忆
node tests\test_probe_service.js         # 需要 8766 上有服务在跑
node tests\test_ws_protocol.js           # 同上，另需 sample_0230_0300.wav
```

运行 JavaScript 测试需 Node.js 22+。评测音频来自日语、英语各一个 YouTube 视频，版权原因不入库。要复现评测，先用 yt-dlp 下载 `eval/manifest.json` 里的源视频到项目根目录（文件名需与清单一致），
再运行 `python tools\build_eval.py` 切出片段，然后：

```powershell
python tools\eval_suite.py --engine sherpa --model parakeet-ja --tag my_run     # 离线回放：字错率 + 延迟
python tools\live_bench.py --tag my_live -- --asr parakeet                       # 真起服务按 1x 推流：含翻译的真实延迟
python tools\live_bench.py --tag my_en --clips en_asmr_0100 en_asmr_0800 -- --asr parakeet --en-asr parakeet
```

设计与取舍见 [DEVELOPMENT.md](DEVELOPMENT.md)，完整的实验过程见 [docs/](docs/)。

## 已知限制

- 两种语言的识别模型加上翻译，服务常驻内存约 3.5GB；只看日语可以用 `--no-preload` 让英语模型等第一次用到时再加载。
- 长句要等说完才定稿翻译，10 秒以上的句子中文会晚到（长句分句的研究结论见 `docs/WORKLOG.md`）。
- 评测集为日语、英语各 5 段，每种语言只覆盖一个视频/说话人；参考文本未经人工听写，只适合比较配置的相对好坏。
- 称呼一致性实验只覆盖少量明确呼语，不解决多角色指代、ASR 漏词或碎句。翻译仍按句进行，可能出现错误或漏译。
- 只测过 Windows 11 + Chrome 154。

## 许可证

代码以 [MIT](LICENSE) 许可证开源：可以自由使用、修改、分发，保留版权声明即可。

模型不随本仓库分发，由 `tools/setup_models.py` 从各自的发布页下载，使用时遵循各模型自己的许可证
（Parakeet 日语版：CC-BY-4.0；Parakeet unified 英语版：NVIDIA Open Model License；SenseVoice：阿里 FunAudioLLM；Hy-MT2：腾讯）。
