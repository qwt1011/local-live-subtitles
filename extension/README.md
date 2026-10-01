# 扩展（v0.6.0）

Chrome MV3 扩展：捕获当前标签页音频，推给本地服务，把识别和翻译结果叠在视频上。安装和日常使用见 [根目录 README](../README.md)。

## 组成

| 文件 | 作用 |
|---|---|
| `popup.html/css/js` | 弹窗：每 2 秒探测服务、一键启动/停止服务（Native Messaging）、识别模型、显示模式、实验开关 |
| `panel.js` | 网页内浮窗：字幕进行中的标签页里出现一个可拖动的圆钮，点开就是嵌进来的弹窗（`popup.html?embedded=1`）；拖标题栏移动、拖右下角调宽，Alt+S 显示/隐藏 |
| `log.html/js` | 服务日志页（经一键启动宿主读 `runs/service.log`） |
| `background.js` | Service worker：取 streamId、创建 offscreen、把字幕事件转给标签页、补注入字幕脚本、工具栏角标 |
| `offscreen.html/js` | 采集：`getUserMedia(tab)` → 16 kHz AudioContext → `pcm-worklet.js` → 100ms 一帧 s16le → WebSocket |
| `content.js` | 字幕覆盖层：3 行、草稿半透明、定稿后原地补中文；拖动改位置、滚轮改字号 |
| `shared.js` | 各部分共用的纯逻辑，Node 下可测（`tests/test_extension_logic.js`） |

权限：`tabCapture`、`offscreen`（采集），`scripting`（给已打开的页面补注入字幕脚本），
`nativeMessaging`（一键启动），`storage`、`activeTab`。只连 `ws://127.0.0.1:8766`。
字幕脚本匹配 `https://www.youtube.com/*` 和 `file:///*`。

## 改了代码之后怎么生效

扩展是"已解压"方式加载的，Chrome 不会自动重读文件：

1. `chrome://extensions` → 本扩展 → 点刷新图标 ↻（service worker、offscreen 才会换成新代码）；
2. 关掉再重新打开弹窗；
3. 视频页不用刷新：开始字幕时如果页面里没有字幕脚本，`background.js` 会自动注入。

**确认加载的是新版本**：扩展卡片上的版本号，或页面 F12 里的
`[本地字幕] content script 已就绪（v0.6.0）`。

## 排查：三个 Console 分别在哪

| 想看的日志 | 在哪看 |
|---|---|
| 覆盖层（渲染、收到的定稿/译文） | 视频页 F12 → Console |
| 路由、取流、补注入 | `chrome://extensions` → 本扩展卡片 → 「Service Worker」 |
| 采集、WebSocket | 同卡片 → 「检查视图」里的 `offscreen.html` |
| 弹窗 | 扩展图标右键 → 「检查弹出内容」 |
| 服务端 | 弹窗「高级 → 查看服务日志」，或手动启动时的命令行窗口 |

服务端日志能直接看出卡在哪一层：

| 服务端打印 | 说明 |
|---|---|
| 什么都没有 | 消息没到服务端，看 Service Worker 的 Console |
| `客户端已连接`，没有下一条 | 只是弹窗在探测（每 2 秒一次，正常） |
| `采集开始：language=ja` | 扩展真的在推音频了 |
| `警告：已开始采集，但 3 秒内没有收到任何音频` | 问题在 offscreen / AudioWorklet |

快捷键（`chrome://extensions/shortcuts` 可改）：Alt+Shift+S 打开弹窗（可从这里开始字幕），Alt+S 显示/隐藏网页里的浮窗。
Chrome 规定开始捕获必须由扩展自己的界面触发：浮窗里嵌的是扩展页面，所以可以直接点；但浮窗只在字幕进行中的标签页出现，
第一次开始仍要点扩展图标或按 Alt+Shift+S。

常见问题：

- **本地视频看不到字幕**：没开「允许访问文件网址」（弹窗会提示）。
- **弹窗说一键启动未安装**：运行 `安装一键启动.bat` 后重新加载扩展；项目文件夹移动过也要重跑。
- **识别模型切换不生效**：服务是手动用 .bat 启动的，扩展不会替你重启它，关掉那个窗口再从弹窗启动。

## 自动化测试

```powershell
node tests\test_extension_logic.js   # 37 项：PCM 分帧、revision 单调性、挂载点、本地媒体识别、存储兜底
node tests\test_probe_service.js     #  4 项：弹窗的服务探测（需服务在跑）
node tests\test_ws_protocol.js       #  9 项：帧格式与服务端协议对得上（需服务在跑）
```

"PCM 掉一帧""revision 回跳""全屏挂载点选错"在浏览器里只表现为偶尔怪一下，几乎无法复现，
在这里是确定的失败。

## 只能在真机上验收的

| 检查项 | 期望 |
|---|---|
| 捕获后原声 | 仍然能听到 |
| 全屏 | YouTube（F 键）和本地 mp4 全屏时字幕都在 |
| 不挡控件 | 进度条、播放按钮能正常点 |
| 草稿 → 定稿 | 文字原地定住，只变样式；中文稍后原地补上 |
| 不回跳 | 后一句不会被前一句覆盖 |
| 设置保留 | 刷新后模式、字号、位置不变 |
| 一键启动 | 服务没开时点主按钮能自动启动；闲置到时自动退出；「停止服务」能释放内存 |

## 采集协议约定（改动时别踩）

- **16 kHz / 单声道 / s16le 裸 PCM**，由 `AudioContext({sampleRate:16000})` 重采样；100 ms 一帧（1600 样点）。
- 客户端**不做任何分段决策**：句子切在哪完全由服务端 VAD 决定。
- `AudioWorkletProcessor.process()` 返回后输入缓冲区会被复用，`postMessage` 前必须 `slice()`。
- streamId 在弹窗里取（有确定的用户手势），要在任何耗时的 await 之前。
