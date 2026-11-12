# 第三方来源与许可

本项目运行于 Waveshare ESP32-P4-NANO（Espressif ESP32-P4 / ESP32-C6）。第三方组件保留各自许可；本文不为项目自有代码另外授予许可证。

## 官方 DSP 子集

- `firmware/components/esp-sr/`：Espressif ESP-SR 2.6.2 的 ESP32-P4 AEC/VAD 所需头文件和静态库。
- `firmware/components/esp_audio_effects/`：Espressif esp_audio_effects 1.4.5 的 ESP32-P4 采样率转换子集。
- 两者均保留 `LICENSE`，官方归档地址、逐文件大小及 SHA-256 位于各组件的 `UPSTREAM.json`。
- `scripts/prepare-speech-dsp.py` 可从官方组件归档重新提取并校验。本仓库没有包含离线识别、合成或唤醒模型。
- 这些许可限定用于 Espressif 产品，具体条款以各组件 `LICENSE` 为准。

## 摄像头示例

`camera/` 从 Waveshare [ESP32-P4-Platform](https://github.com/waveshareteam/ESP32-P4-Platform) 的 `17_simple_video_server` 示例派生，来源提交为 `6f78fa810ec505048c1941e3d09cefc9f29129bd`。
示例中的 Espressif 视频代码保留 SPDX 版权和许可证标记；完整许可见 `camera/LICENSE`。
摄像头示例前端的依赖清单和锁定文件保留在 `camera/frontend/`。

## 下载型依赖

ESP-IDF 5.5.4 和组件管理器依赖按 `firmware/dependencies.lock` / `camera/dependencies.lock` 获取；
Python 依赖按根目录 `requirements.txt` 安装。下载得到的工具、组件和运行库没有重复上传，许可随其各自发行包提供。
`sources/` 是本机保留的上游参考仓库，已排除出 Git；当前联合固件的构建不依赖它。
