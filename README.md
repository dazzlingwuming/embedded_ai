# BenchLink · ESP32-P4 本地测试台

当前开发板固件为 **0.6.0**：摄像头、真实麦克风、喇叭和网络仪器网关已经放在同一程序中。
语音路线固定为 **云 ASR → DeepSeek → 云 TTS → 开发板喇叭**，不使用直接语音问答大模型或本地语音模型。
所有工具、配置、备份、缓存、日志和测试数据都放在本目录。

## 换电脑继续开发

仓库：<https://github.com/dazzlingwuming/embedded_ai>。

```powershell
git clone git@github.com:dazzlingwuming/embedded_ai.git
cd embedded_ai
.\scripts\setup-console.ps1
```

安装脚本使用系统已安装的 64 位 Python 3.11／3.12（推荐 3.12），环境与缓存仍放在本项目内。
然后按 [换电脑继续开发](doc/换电脑继续开发.md) 配置本地 API Key、板子 IP 和启动方式。
只运行网页后台无需重装开发板固件或 ESP-IDF。当前语音上下文是通话内临时记忆，不随 Git 迁移。

Git 包含源码、网页、测试、配置模板、组件版本锁和必要 DSP 静态库；
`api_key/`、`local/`、工具缓存、固件备份、日志及采集数据保留在各电脑本地。
本文后续提到的 `data/`、`logs/`、`backups/` 是本机验证产物，克隆仓库后不会出现，需运行相应检查重新生成。
第三方来源和许可见 [THIRD_PARTY.md](THIRD_PARTY.md)。

## 打开控制台

```powershell
.\scripts\start-console.ps1 -Background
```

浏览器打开 <http://127.0.0.1:8765/>。服务已运行时直接刷新即可。
当前板子为 `http://192.168.2.38`；电脑和板子均实测连接 `现场Wi-Fi（名称已脱敏）`，同属 192.168.2.x 局域网。
页面会显示开发板实际 SSID 和 RSSI。Type-C 当前用于供电、烧录和串口日志，音视频通过局域网 Wi-Fi 传输。

## 摄像头与声音

工作台顶部自动开始真实视频预览，可以暂停、全屏或保存照片到 `data/camera-photos/`。
流畅／均衡／清晰分别使用 JPEG 画质 40／60／80，三档都是 **800×800**；更强压缩减少传输量，也减少细节。
当前预览编码上限 20fps，页面显示实际收到的帧率，不能把传感器模式的 50fps 当作网络视频帧率。

「卡顿统计」显示最近 60 秒的帧间隔 P95、最长间隔和最慢 1% 帧率，至少 100 个间隔后显示最慢 1% 数值。
它能揭示平均帧率掩盖的停顿；最慢 1% 帧率越低，长停顿通常越明显。浏览器使用一个待解码帧，优先保留最新画面。
当前仍观察到长间隔，不能宣称卡顿已经完全解决。

「开始监听」接收板载真实麦克风，默认静音；取消静音后通过电脑播放。AI 通话则从板上喇叭播放。
「开始录音」保存 48kHz、16bit、单声道原始 WAV，不需要先开始监听。停止后到「测试记录」回放或下载。
录音固定增益 12dB、ALC 关闭，不做电脑端降噪、VAD、自动增益、静音裁剪或重采样。
丢帧补静音并记录位置；过大缺口或断线时结束文件，标记中断，不自动续录。

原始录音与 AI 语音通话互斥，避免喇叭影响测量；视频和原始音频已同时测试。
切换到其他页面或隐藏页面时，当前浏览器视频预览暂停，其他浏览器的订阅可独立继续。

## 用 DeepSeek 进行语音问答

「AI 助手」现有「视觉观察」区块：后台持续接收摄像头，在本机筛选近期较清晰、曝光可用的画面，
每轮文字或语音提问默认最多附一张给 `deepseek-flash`。缩略图显示本轮实际选择的 JPEG 和请求阶段。
采样不调用模型；模型只在提问时收到选中图片，历史原图不无限累积。关闭摄像头参与后继续纯文字／语音对话。
画质分数是启发式指标，不代表识别正确率；完整的数据流、边界和后续问题见
[视觉上下文与问题清单](doc/视觉上下文与问题清单.md)。

进入「AI 助手」，点击「开始语音通话」。显示「等你说话」后，对板载麦克风说话，停顿等待回答。
实际流程为 **Fun-ASR-Realtime 识别 → DeepSeek 回答 → CosyVoice 合成 → 板上播放**。
已有的 DeepSeek 文字入口继续可用。默认开启自动打断：板上 AEC 消除喇叭回声，VAD 与云 ASR 共同确认插话。
说“等等”或直接提出新问题会停止旧回答并继续处理新问题，也保留手动打断和轮流说话模式。
对话历史只保留完整播出的短片段；中途取消的片段不会作为已听内容发给模型。
DeepSeek 文字流、TTS 合成和喇叭播放并行推进，首个自然短片段即可开始发声。
已移除正常回答的 30 秒音频、2000 字和整轮 90 秒截断；要求全文朗读时不会自行改为摘要。

凭据已经按用户提供的本地文件接入：

- `api_key/.env` 的 `api_key:` / `DEEPSEEK_API_KEY=`：DeepSeek。
- 同文件的 `aliyun_key:` / `ALIYUN_API_KEY=`：阿里云 ASR/TTS，兼容带点号的新 Key 格式。
- 如另有 `api_key/realtime.env`，可使用 `DASHSCOPE_API_KEY=`，优先读取该文件。
- 非秘密设置在 `local/realtime.json`。当前使用北京通用 API Host，已经通过真实调用；也可填写相应业务空间的专属 Host。

API Key 不回显到浏览器，不写入开发板。无需安装阿里云 CLI。
项目当前全部使用 Direct 直连，下载、DeepSeek 和阿里云服务不经过 7900；不改变系统代理或 Wi-Fi。
保存配置不会开始收费调用；点击通话或运行注明调用云端的验证脚本后才发起请求。
本项目播放音量可在云语音配置中调到 0～100。默认通话上限 600 秒，可设 0 为不限时；
达到设置时长后完成当前回答再结束，手动结束始终立即生效。连续 ASR 输入会产生相应服务用量。
关闭网页不会自动结束后台通话，离开前点「结束通话」。断线结束本次通话，需要手动重新开始。

细节与证据见 [ASR、DeepSeek 与 TTS 接入说明](doc/实时语音接入与自检.md)。

## 仪器通信

「启动 4 台模拟仪器」后，可选择节点读取状态／波形或修改模拟参数。命令实际通过 ESP32 转发。
这些是明确标记的软件模拟节点，尚未接入实体 USB 仪器、Hub、电机或通用 USB-over-IP。

以后可在实体笔记本运行 `simulators/instrument.py`，无需第三方依赖：

```powershell
python instrument.py --board http://192.168.2.38 --devices 4
```

这只模拟网络仪器；普通笔记本的 USB 口不能仅凭一根线变成 USB 仪器设备端。
详见 [笔记本测试工具说明](simulators/README.md)。此前未在另一台实体笔记本上实测。

## 编译、备份与停止

```powershell
.\scripts\build-gateway.ps1 -BuildOnly
.\scripts\build-gateway.ps1 -Port COM5
.\scripts\stop-console.ps1
```

停止后台服务前先停止录音；停止脚本会保护正在进行的原始录音。
Wi-Fi 私有配置在 `local/wifi.json`，修改板上保存网络需重新编译烧录。
工具使用临时 `P:` 盘作为本目录别名，兼容中文路径，不是把文件搬到其他位置。

当前依赖锁定 ESP-IDF 5.5.4、ESP-Hosted 1.4.6、esp_wifi_remote 0.14.5、esp_video 2.6.0。
摄像头和 ES8311 共用 GPIO7/8 的 I2C 总线。控制端口 80，原始音频 81，喇叭 WebSocket 82，视频 83，AEC 语音流 84。
原厂完整 Flash 在 `backups/factory-esp32p4-16mb.bin`；旧摄像头独立固件在 `backups/camera-verified/`，
0.4.0 音频固件在 `backups/gateway-0.4.0/`。当前 0.6.0 备份在 `backups/gateway-0.6.0/`，上一版在 `backups/gateway-0.5.1/`。它们属于历史独立程序，分别烧录会替换当前应用。

## 验证

```powershell
# 软件检查，不调用收费服务
.\.tools\python\Scripts\python.exe -m unittest discover -s tests -p 'test_*.py' -v
# 实板播放失败语义检查：静音 PCM，无 API
.\.tools\python\Scripts\python.exe scripts\check-speaker.py
# 实板收音和短提示音回传，无 API
.\.tools\python\Scripts\python.exe scripts\check-voice.py
# 视觉测试工具的离线自检；加 --run-cloud 才调用真实模型
.\.tools\python\Scripts\python.exe scripts\check-vision-context.py
# 三分钟真实音视频 + 四台软件仪器，会创建 WAV，无云 API
.\.tools\python\Scripts\python.exe tests\verify_hardware.py --seconds 180 --video
```

需顺序运行硬件检查，期间不要同时手动操作录音、语音或模拟仪器。
真实云 API 检查脚本及各项边界见语音接入说明。
2026-11-13 的视觉接入版本通过 **193 项软件检查**。真实云测试正确识别两张合成图的形状、颜色和随机数字，
也通过生产聊天入口核对了摄像头选图、缩略图与发给模型的图片字节一致。
用户重新对准电脑屏幕后，模型描述的屏幕、“现场声音”标题和深色波形区与人工查看相符；
另有模糊画面样本，模型明确说明看不清。这些是有限样本，不代表复杂场景或真人语音＋视觉的全面验收。
后台视觉采集开启时，实板麦克风上行和喇叭短提示音收发检查通过，未调用 ASR/TTS。
记录见本机 `data/vision-verification-summary.json` 和 `logs/vision-unit-tests.log`。

随后完成同批实拍 A/B：47 个固定时点、每策略两遍、188 次云请求。主体粗分类为最新帧 91/94、当前算法 90/94；
杯子晃动有获益个例，但当前算法增加选图等待（中位数 250 ms），两边输入均为 740 Token／次。
这批数据尚不能证明总体更优，没有据此修改阈值。完整方法、限制和本机报告位置见
[实拍对比记录](doc/视觉上下文与问题清单.md#2026-11-13-同批实拍选帧对比)。新增实验工具后，软件检查为 202 项通过。

此前 0.6.0 已通过 128 项软件检查和 15 项实板联合检查；四节点稳定查询 30.84 秒、17 轮，
包括故障注入在内录音 62.24 秒、1058 张 JPEG，WAV 无缺帧、无中断。
长朗读样本《滕王阁序》717 字全部进入 TTS 并获实板排空确认，播放 270.548 秒；
第一段非零 PCM 在 1.171 秒提交，板上 DAC 参考在 1.578 秒出现。长读中断停止确认 15ms。
长朗读输入为文字任务；真人自动插话另由用户确认成功，不把文字样本当作麦克风识别验收。
证据见 `data/duplex-verification-summary.json`、`data/long-voice-check.json` 和 `logs/final-unit-tests.log`。
历史 0.5.0 的三分钟联测保存在 `data/hardware-verification-v0.5.0.json`。
视频帧间隔仍有长尾，尚未通过长时间低延迟或生产环境稳定性验收。

目录：`firmware/` 联合固件、`camera/` 原独立摄像头工程、`pc_backend/` 本机服务、
`pc_ui/dist/` 页面、`simulators/` 测试节点、`tests/` 和 `scripts/` 验证工具、`doc/` 需求和记录。
