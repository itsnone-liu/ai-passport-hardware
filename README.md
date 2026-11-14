# FoloToy AI Passport — 硬件调研与固件分析

对一台 FoloToy「AI Passport」（ESP32-C3 玩具挂件 / 小智语音助手硬件）做的一次完整逆向调研记录：
USB 枚举 → 8 MB flash 全量备份 → 固件与分区分析 → 安全评估 → 仿真运行 → 从开源基座源码重新构建。

> **脱敏声明**：本仓库不含任何设备凭据。MAC 地址、`DeviceKey`、`DeviceSecret`、`ProductKey`、
> MQTT 用户名口令、Wi-Fi 配置均已移除或打码；8 MB flash 镜像与 ELF 一律不入库。
> 文中保留的是**结构与方法**，不是可克隆的身份。

## TL;DR

| 项 | 结论 |
|---|---|
| SoC | **ESP32-C3**（单核 RISC-V，QFN32），硅版本 **v1.1** |
| Flash | 片内 **8 MB**（XMC），未外挂 |
| 固件 | `trae_card` **2.1.0-xiaozhi.29**，ESP-IDF **v5.5.3**，board `ai-passport-leogie` |
| 应用 | 小智（xiaozhi / Tenclass）语音助手变体，云端 `api.tenclass.net` |
| 安全 | ❌ Secure Boot 关闭　❌ Flash 加密关闭　❌ eFuse 空白 |
| 升级 | **无 OTA 分区**（只有 `factory`），只能 USB 烧录 |
| 身份存储 | `cardid` 分区里 `folotoy-key` 明文（DeviceKey/Secret/ProductKey） |
| 开源 | 官方仓库是 **BSP 开发基座 + demo**，**不是**产品固件 |

## 目录

```
docs/
  research-notes.md        完整调研笔记（10 节，含所有命令与踩坑）
data/
  partitions.csv           产品固件分区表（从 flash 解出）
  usb-identity.md          USB 枚举 / 芯片 / flash 识别
  image-hashes.md          各镜像的尺寸与哈希（镜像本身不入库）
  firmware-report-raw.txt  自动分析器原始输出（已脱敏）
scripts/
  parse_esp.py             ESP32 镜像格式解析（bootloader / app / 分区表）
  analyze_firmware.py      固件字符串与组件特征扫描
  dump_app.py              从合并镜像中切出 app 段
  make_merged.py           构造模拟器可加载的最小镜像
  make_full_assets.py      构造带素材分区的镜像
  wait_and_backup.ps1      USB 等待 + esptool 全量备份
  idf_install.ps1          安装 ESP-IDF 工具链（国内镜像）
  idf_fetch_subs.ps1       无 git 环境下用 HTTP zip 拼装 IDF 源码
  idf_build3.ps1           构建 ai-passport
sim/
  built-ui-esp32c3.png     自源码构建的固件在模拟器里的截图
  official-demo-ui.png     官方 demo 固件截图（对照）
```

## 关键发现

### 1. 分区表说明这是一台「有资源包」的产品机

产品固件有 14 个分区，除常规 `nvs` / `phy_init` / `factory` 外还有
`petart`（宠物素材）、`companion`（伙伴资源）、`imgava`（头像）、`assets`、`theme_cache`、
`cardid`（卡密）、`imgframe`/`imgstore`/`imguser`（图片槽）、`audio`（音频）、`netcfg`、`recovery`。
详见 [`data/partitions.csv`](data/partitions.csv)。

**没有 `otadata` / `ota_0` / `ota_1`** —— 这台设备不支持 OTA，只能 USB 升级。

### 2. 官方开源的是「开发基座」，不是产品固件

`FoloToy/ai-passport` 仓库（`main` @ `b97765a`）自带的分区表只有三行：

```
nvs,      data, nvs,     0x9000, 0x6000
phy_init, data, phy,     0xf000, 0x1000
factory,  app,  factory, 0x10000, 0x7f0000
```

它提供的是一套 BSP + 14 个 demo 分支（菜单 UI、俄罗斯方块、番茄钟、白兔宠物……），
用于「自己写固件」。产品里跑的小智应用**没有开源**。
换句话说：从该仓库构建得到的是 **demo 界面**，不是设备上那个界面。

### 3. 安全姿态：裸奔

- `espefuse summary`：Secure Boot / Flash Encryption 全部 disabled，eFuse 块为空
- `cardid` 分区（16 KB）里 `folotoy-key` 命名空间明文存着设备身份（阿里云 IoT 风格字段）
- NVS 里缓存着云端 MQTT 凭据（明文）
- 结果：拿到 flash 备份 = 拿到设备身份，可直接克隆；也能随意改固件刷回

### 4. 固件里有产线 AT 指令集

串口日志显示：`AT+CARDID=`（写卡密）、`AT+TEST?`、`at=config=?`、`at=command=?`、`at=reboot`。
未 provision 时会用 Wi-Fi STA MAC 自补 SN，并禁用 token 广播。

## 可复现的两条路

**A. 只做静态分析**（不需要设备）：仓库里的 python 脚本可直接解析任何 ESP32 镜像，
`parse_esp.py` 会打印分区表、段表、app 描述符。

**B. 跑起来看**：用开源 ESP32-C3 模拟器 [PassportSim](https://passportsim.bugs.cc)
加载固件。注意**只给 flash 镜像不够** —— Wi-Fi/BLE 是高级模拟，需要匹配的 ELF 才能绑定，
否则触发 `E_TRIPWIRE` 且画面全黑。正确做法是自定义 corpus 条目同时给 `bin` + `elf` + `boot_elf` + `pt`。
详见 [`docs/research-notes.md`](docs/research-notes.md) 第 9、10 节。

**C. 自己写固件**：见 [`docs/research-notes.md`](docs/research-notes.md) 第 10 节，
包含在没有 git 的环境里用 HTTP zip 手工拼装 ESP-IDF 5.5.3 的完整流程。

## 免责

本项目仅用于**自有设备**的学习与研究。文中涉及的设备身份字段已全部脱敏；
请勿将本仓库的方法用于未获授权的设备。固件镜像与 ELF 属于厂商版权内容，未包含在本仓库中。

---

## 语音链路工程状态（2026-11-14）

硬件调研之外，本仓库现在还有一条**已验证的语音链路**：Passport → Ubuntu 服务端（控制 +
音频双通道）→ ASR → dsh → TTS → 回放，`server/` 是服务端实现，`docs/voice-protocol-v1.md`
是双方签认并冻结的协议。

- 当前进度、实测数字、已修缺陷与待办：[`docs/status-2026-11-14.md`](docs/status-2026-11-14.md)
- 协议正文：[`docs/voice-protocol-v1.md`](docs/voice-protocol-v1.md)
- 服务端说明与运行方式：[`server/README.md`](server/README.md)

> 未验证项只有一条：真人对设备说话、ASR 认出字——需要有人在设备旁边。
