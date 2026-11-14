# FoloToy AI Passport —— 身份 / 源码 / 使用 / 开发 资料存档

> 整理：2026-11-14　设备 MAC `98:C3:77:XX:XX:XX`（USB `303A:1001`，COM3）
> **设备身份已确认：FoloToy AI Passport（乐鑫 ESP32-C3）**
> ⚠️ 更正：我此前判断"厂商未开源、搜不到源码"是**错的**。官方仓库就在 GitHub：`FoloToy/ai-passport`，
> 而且固件里直接写着 `ai-passport.folotoy.cn`。以本文为准。

---

## 1. 官方资源清单

| 用途 | 地址 |
|---|---|
| **固件源码（主仓库，MIT）** | https://github.com/FoloToy/ai-passport |
| 仓库开发规范（必读入口） | 仓库根 `AGENTS.md` / `AGENTS.zh_CN.md` |
| 官方固件二进制（预编译） | https://github.com/FoloToy/folotoy-bin |
| 官方文档 | https://github.com/FoloToy/folotoy-doc （站点 docs.folotoy.com） |
| 自建服务端 | https://github.com/FoloToy/folotoy-server-self-hosting |
| 产品销售/激活页 | https://ai-passport.folotoy.cn （二维码形如 `?s=<sn>&k=<key>`） |
| AI 服务端（对话/OTA） | `api.tenclass.net`（虾哥 Tenclass 的 xiaozhi 服务） |
| 上游对话框架 | https://github.com/78/xiaozhi-esp32 （AI 部分的上游） |
| 社区固件替代（第三方） | `kunlun322/Qoder-Passport`（ESP32-C3/8MB/**IDF 5.5.3**，与官方同构） |
| 无设备调试用模拟器 | `BlackHole1/passportsim`（ESP32-C3 模拟器 + MCP，Windows/macOS） |
| 第三方模拟器 | `VOID001/FoloToy-Passport-Simulator`（JavaScript） |

---

## 2. 硬件事实（官方 `components/bsp/include/bsp_pins.h`，单一事实来源）

**屏幕**：ST7789P3 240×320，4 线 SPI（`SPI2_HOST`），PCLK 80MHz，SPI mode 0，出厂需反色（INVON）

| 信号 | GPIO |
|---|---|
| LCD MOSI / SCLK / CS / DC | 9 / 8 / 1 / 20 |
| LCD RST | `-1`（未接 MCU，硬接 3.3V，走 SWRESET 软复位） |
| 背光 BL | 21（LEDC，5kHz，10bit） |

**按键**：三个键**共用一个 ADC 引脚** GPIO0（ADC1_CH0），靠分压电阻区分（外部 10k 上拉）

| 键 | 分压 | 电压 |
|---|---|---|
| 上 | 0Ω | ≈0 mV |
| 下 | 1k | ≈300 mV |
| 确定 | 2.2k | ≈595 mV |
| 松开 | — | ≈3300 mV |

阈值表 `{0–150}, {150–447}, {447–1900}`；短按 180ms、长按 500ms；`deep sleep` 用低电平唤醒。
⚠️ 官方明确：**不能用内部上拉**（约 45kΩ，三档会挤到 0–154mV 并随温漂重叠）。

**音频**：ES8311 codec，I2S 全双工

| 信号 | GPIO |
|---|---|
| MCLK / BCLK / WS | 6 / 5 / 3 |
| DOUT（MCU→codec）/ DIN（codec→MCU） | 2 / 4 |
| PA 功放使能 | `-1`（常通，未接 MCU） |

**I2C0**：SDA 10 / SCL 7；ES8311 `0x18`、**CW2017 电量计** `0x63`

**其他**：ESP32-C3，8MB Flash，**无 PSRAM**

---

## 3. 你这台设备的实测档案

| 项目 | 值 |
|---|---|
| 芯片 | ESP32-C3 (QFN32) rev **v1.1**，封装内 8MB XMC flash，40MHz 晶振 |
| USB | `303A:1001` 芯片内置 USB Serial/JTAG（CDC → COM3，MI_02 → WinUSB） |
| 固件 | 项目名 `trae_card`，版本 **2.1.0-xiaozhi.29**，IDF **v5.5.3**，构建 `Nov 12 2026 22:42:48` |
| 板型标识 | **`ai-passport-leogie`**（上报 `name: ai-passport`） |
| 客户端 UUID | `2d7fcfd7-3286-4dcf-9d82-2a0573191a62` |
| 卡片 ID | `cardid` 分区 `folotoy-key/DeviceKey` = `TUyXXxxXXxxXXxx`（由 MAC 派生） |
| AI 服务 | MQTT `api.tenclass.net`，`client_id = GID_test@@@<mac>@@@<uuid>`，`publish_topic=device-server` |
| OTA | `https://api.tenclass.net/xiaozhi/ota/`（xiaozhi 标准激活/配置接口） |
| 安全 | **Secure Boot 关 / Flash 加密关**，eFuse 全空，固件可任意读改 |
| 全片备份 | `D:\dsh-work\flash\full-backup.bin`（8MB，MD5 `6E69D1BC3711CB6C778BFCCA70FD6C56`，回读校验通过） |

分区（产品固件在上游模板基础上扩展了素材/私有分区）：

| label | type/sub | offset | size | 内容 |
|---|---|---|---|---|
| nvs | data/nvs | 0x009000 | 24K | `phy`、`trae_cfg`、`nimble_bond`、`nvs.net80211`、`xiaozhi` |
| phy_init | data/phy | 0x00F000 | 4K | 空 |
| **factory** | app/factory | 0x010000 | 2.8M | 主固件（熵 7.43，字体/图内嵌） |
| petart | data/0x40 | 0x2DE000 | 884K | 压缩素材（熵 7.59） |
| companion | data/0x41 | 0x3BB000 | 1.6M | 压缩素材（熵 7.83） |
| imgava | data/spiffs | 0x54D000 | 128K | 头像 `avatar-01..07` |
| assets | data/spiffs | 0x56D000 | 920K | **空** |
| theme_cache | data/0x42 | 0x653000 | 92K | 空 |
| cardid | data/nvs | 0x66A000 | 16K | `folotoy-key`：DeviceKey / DeviceSecret / ProductKey / HardwareVersion |
| imgframe / imgstore / imguser / audio | spiffs | 0x66E000… | 152K/32K/32K/80K | 全空 |
| netcfg | data/nvs | 0x6BC000 | 16K | 空（未配网） |
| recovery | app/test | 0x6C0000 | 1.2M | 空（全 0xFF） |

NVS 里的关键命名空间：`xiaozhi`（`wifi_profiles`、`uuid`、`mqtt_cache_v1`、`avatar`）、`trae_cfg`（`profile` blob）、
`nimble_bond`（BLE 配对 IRK）、`phy`（射频校准）。

**注意**：`api.tenclass.net` 的 MQTT 凭据（username/password）被缓存在 NVS 里，明文，我已脱敏未输出。

---

## 4. 使用方法（用户视角）

1. **上电 → 配网**：设备开热点（`192.168.4.1` 配置页，`xiaozhi.mqtt_cache_v1` 等走 xiaozhi 体系），手机连上填 Wi-Fi；也支持 BLE 配网（`nimble_bond`）。
2. **激活绑定**：设备显示/生成二维码，指向 `https://ai-passport.folotoy.cn?s=<sn>&k=<key>`；扫码到 FoloToy 平台绑定卡片。
3. **AI 对话**：绑定后由 `api.tenclass.net` 下发 MQTT 配置，设备连上去做语音对话（Opus 流）。
4. **升级**：本设备**无 OTA 分区**，只能 USB 刷写（官方 `folotoy-bin` 或自编译）。

---

## 5. 开发方法（官方流程）

### 环境：**必须是 ESP-IDF v5.5.3**

```bash
# 已有激活环境且 idf.py --version 输出 ESP-IDF v5.5.3 才可复用；其它版本安装到并行目录，不覆盖
git clone --branch v5.5.3 --recursive https://github.com/espressif/esp-idf.git ~/esp/esp-idf-v5.5.3
~/esp/esp-idf-v5.5.3/install.sh esp32c3
. ~/esp/esp-idf-v5.5.3/export.sh
```

国内线路（乐鑫官方，不改全局 git/pip）：`git.espressif.com.cn` 镜像 clone esp-idf（子模块自动跟随）、
工具归档 `dl.espressif.cn/github_assets`、组件 `components-file.espressif.cn`。
⚠️ 老镜像脚本会在全局 git 里写 `insteadOf` 把子模块劫持到 404 源，子模块拉不动时先查 `git config --global --get-regexp 'url\..*jihulab'`。

**Windows**：用乐鑫官方 ESP-IDF Tools Installer 选 v5.5.3，之后在它生成的 **ESP-IDF PowerShell** 里执行（不要把 POSIX 的 `export.sh` 机械翻成 PowerShell）。

### 构建与门禁

```bash
./tools/validate.sh --static     # 仓库检查 + host tests
./tools/validate.sh --firmware   # ESP-IDF 构建 + 合并镜像验证
./tools/validate.sh              # 完整门禁
idf.py -p COM3 flash monitor     # 烧录 + 串口（烧录前需用户同意）
```

### 代码边界（官方硬规则）

- **BSP（板级）** → `components/bsp`，硬件参数只有 `bsp_pins.h` 一个来源；换板只改它
- **应用（页面/状态机/动画/任务）** → `main`
- **二次开发必须重新设计 UI**：禁止沿用 demo 的测试菜单/页面外壳；只改名、换色、加功能不算重做（BSP API 与非 UI 逻辑可复用）
- **LVGL 非线程安全**：LVGL 任务外访问对象必须 `bsp_lvgl_lock()`
- **中文字体**：默认 Montserrat 无中文字形，编译通过 ≠ 能显示，须查字形覆盖 + 真机验收
- 按键回调不得阻塞；慢操作（音频/存储/网络）丢进工作任务
- 可测逻辑（状态机/协议/计时/布局）与 ESP-IDF、LVGL 解耦，由 host tests 覆盖

### 仓库自带的 5 个 AI 技能

`skills/` 下：`passport-develop`、`passport-setup`、`passport-build`、`passport-device-test`、`passport-debug`。
仓库设计为"AI agent 可直接开发"，AGENTS.md 里有按任务类型加载文档的路由表。

### 分支模型

每个应用一个分支，`main` 的 README 只介绍托管在各分支上的应用（不在 main 分支时只介绍本分支）。

### 三种"验证"别混为一谈

| 方式 | 是什么 | 能验证什么 | 不能验证什么 |
|---|---|---|---|
| **host tests**（官方 `tests/`，`validate.sh --static`） | 在 PC 上编译并运行的单元测试，逻辑与 ESP-IDF/LVGL 解耦 | 状态机、协议解析、计时、布局计算 | 任何硬件行为 |
| **PassportSim 模拟器**（第三方，见下） | Rust 写的 ESP32-C3(RISC-V) 模拟器，跑**未经修改**的 ESP-IDF 镜像 | UI 与 LVGL 布局、按键事件、串口日志、启动流程、崩溃定位 | 真实音频、真实 Wi-Fi/MQTT 连通性、Flash 实速、外设电气特性 |
| **真机烧录** | USB 刷写后上板运行 | 一切 | —— |

**PassportSim**（`BlackHole1/passportsim`，MIT，Rust）：

- 在线版 https://passportsim.bugs.cc （纯浏览器 WASM，**固件不上传**，本地跑）；桌面版支持 **macOS apple-silicon / Windows x64**，**不支持 Linux**
- 可喂任何 merged `.bin` / `idf.py` 构建目录 / `.pebundle`；**理论上我们的 `full-backup.bin`（从 0x0 起的整片镜像）也能直接跑起来看界面**
- 功能：屏幕、按键、声音、串口、UI 树、暂停/单步/存档、截图；单元卡有 电量/USB/音频/NFC/Wi-Fi/蓝牙
- 带 **MCP server**（`passportsim mcp`）+ agent 技能，可让 AI 自己启动、按键、等日志、读 UI 树、截图
- 安装：Windows `irm https://passportsim.bugs.cc/install.ps1 | iex`；macOS `curl -fsSL .../install.sh | sh`
- 模拟范围与保真度的权威说明在仓库 `docs/overview.md`（我未逐页精读，重要结论应回查该文）

---

## 6. 安全 / 隐私结论

1. **固件完全无保护**：Secure Boot 与 Flash 加密均关，eFuse 空 → 谁拿到都能读出固件、配网信息、卡片密钥。
2. **卡片身份明文**：`cardid` 分区 `DeviceKey`（`TUyXXxxXXxxXXxx`）与 `DeviceSecret`/`ProductKey`（阿里云 IoT 风格字段）都是明文，可直接伪造克隆。
3. **云端凭据缓存**：`nvs:xiaozhi/mqtt_cache_v1` 里有 MQTT username/password 明文。
4. **我脚本的"6 处 PEM 私钥"告警是误报**：那 6 条是 mbedTLS 的**解析器常量字符串**
   （`BEGIN RSA PRIVATE KEY` / `BEGIN EC PRIVATE KEY` / `BEGIN PRIVATE KEY` / `BEGIN ENCRYPTED PRIVATE KEY` /
   `BEGIN RSA PUBLIC KEY` / `BEGIN PUBLIC KEY`），不是真实内嵌密钥。真实内嵌只有 **1 张 TLS 证书**。
5. 官方规则也要求：不得提交凭证、设备二维码秘密、私钥、个人数据或未脱敏日志。

---

## 7. 本次产物

| 文件 | 说明 |
|---|---|
| `D:\dsh-work\flash\full-backup.bin` | 8MB 全片备份（回读校验通过），刷坏可整片还原 |
| `D:\dsh-work\analysis\firmware-report.txt` | 分区/端点/关键字/NVS 分析报告 |
| `D:\dsh-work\analyze_firmware.py` | 分析脚本（可重复跑） |
| `D:\dsh-work\dump_app.py`、`parse_esp.py` | app 描述符 / 分区表解析 |
| `D:\dsh-work\tools` | esptool 5.6.0 隔离安装（`PYTHONPATH` 指向它） |
| `D:\dsh-work\ai-passport-notes.md` | 本文件 |

还原命令（真出事时用）：

```powershell
$py='%LOCALAPPDATA%\hermes\hermes-agent\venv\Scripts\python.exe'
$env:PYTHONPATH='D:\dsh-work\tools'
& $py -m esptool --port COM3 --baud 460800 write-flash 0x0 'D:\dsh-work\flash\full-backup.bin'
& $py -m esptool --port COM3 --baud 460800 verify-flash 0x0 'D:\dsh-work\flash\full-backup.bin'
```

---

## 8. 下一步可选路线

1. **clone 官方仓库并编译一次**（需装 ESP-IDF 5.5.3，约 3–5GB）：验证 `./tools/validate.sh --firmware` 通过，产出 `idf.py merge-bin` 整包。
2. **刷官方固件上机**：先 `full-backup.bin` 在手再动手；官方说明"刷写前不强制备份、也不授权全片擦除"。
3. **写自己的应用**：在 `main/` 里按官方规则做独立 UI，BSP 直接用 `bsp_pins.h`（引脚已全知道）。
4. **无设备开发**：用 `BlackHole1/passportsim` 模拟器 + MCP 调试，省去反复烧录。
5. 想看它跟云端说了什么：抓 `api.tenclass.net` 的 MQTT 流量（凭据在 NVS 里，明文可取）。

---

## 9. 模拟器（PassportSim）实测结论 —— 2026-11-14

安装位置 `D:\passportsim\passportsim-1.0.3-windows-x64\`（官方 release v1.0.3 windows-x64，
SHA256 `4255db83…9289c` 已校验，23,194,519 字节）。exe 45MB，自带 official `.pebundle`（24MB）。

### 9.1 官方 demo：完整可用

```
passportsim start official --label demo
passportsim clock take --owner agent
passportsim run --for 3s --timeout 30s
passportsim screenshot --save-as demo-ui     # 产物在 %LOCALAPPDATA%\passportsim\data\artifacts\...
```

结果：boot `matched until_ui_settled`，串口打印
`bsp_i2c: 发现设备 @ 0x18 <- ES8311` / `@ 0x63 <- CW2017 电量计`、
`bsp_batt: 优特利 520mAh profile 已匹配`（与 `bsp_pins.h` 完全吻合）。
截图 `D:\dsh-work\sim\demo-ui.png`：蓝底、标题 **FoloToy**、菜单
Display / Button / Audio / Battery / Wi-Fi / BLE / Low Power，底部像素机器人站在草绿地面上。

### 9.2 我们的 flash 备份：能启动，但看不到 UI

| 尝试 | 结果 |
|---|---|
| 直接 `start full-backup.bin` | ❌ `E_SECRET_REFUSED: its cardid window is not erased`（凭据保护） |
| 抹掉 nvs + cardid 的 8MB 副本 | ❌ 仍报 cardid（模拟器检查的窗口是文档里的 `0x356000..0x359FFF`，位于我们 petart 内部） |
| 只含 bootloader+分区表+app 的合并镜像（3,006,464B，nvs 抹除） | ✅ 加载成功，但 `E_TRIPWIRE` |
| 补齐素材分区的镜像（6,725,632B，切到 0x66A000 为止） | ✅ 加载，日志更完整，仍在 579ms `E_TRIPWIRE` |

**卡点原因（`inspect fidelity` 的 receipt 里看得一清二楚）**：

```json
"radio": "unbound (no ELF)",
"hle": { "bound": null,
         "tripwires_hit": ["radio_mmio:radio_ble 0x60031204: not one of the boot rtc_sleep_pu accesses"] }
```

Wi-Fi/BLE 是**高级模拟（HLE）**，需要**匹配的 ELF** 才能在驱动层拦截；只有 flash 镜像时
radio 无法绑定，guest 一碰 BLE 射频寄存器就触发 tripwire。`skills/passportsim/SKILL.md` 也写明：
"CLI 只为内置 demo 和带 `elf` 条目的 corpus 查找 app ELF"。
面板始终 `panel off` → 截图全黑（880 字节 PNG）。

**结论**：flash-only 备份在模拟器里只能验证「启动早期 + 串口日志」，**看不了 UI**。
要跑完整 UI 必须提供 ELF —— 要么自源码 `idf.py build` 后的构建目录，要么官方 `.pebundle`。

### 9.3 意外收获：串口日志暴露的固件行为

即使 UI 出不来，日志已经很有信息量：

```
I (221) app: 时间未同步,置默认起点 2026-01-01 00:00(BLE 对时后覆盖)
E (413) companion: Missing/mismatched resource pack
I (543) pet_art: Mapped 16063 petart bytes from 0x2de000
W (576) identity: SN 缺失,已按 WIFI_STA MAC 自补: 020000784d20
I (576) identity: 身份载入: SN=020000784d20 hw=- provisioned=0
W (576) app: 未写入 Key/ProductKey,小程序绑定与 token 广播不可用。请用 AT+CARDID= 指令写入或烧录 cardid 分区
I (578) prov: 产线指令就绪: AT+CARDID=/? / AT+TEST? / at+config=? / at+command=? / at+reboot
```

- **产线 AT 指令集**：`AT+CARDID=`（写卡密）、`AT+TEST?`、`at+config=?`、`at+command=?`、`at+reboot`
- 未 provision 时用 **Wi-Fi STA MAC 自补 SN**，并**禁用 token 广播**
- `petart` / `companion` 是**带版本校验的资源包**（"Missing/mismatched resource pack"）

### 9.4 使用备忘

```powershell
$exe='D:\passportsim\passportsim-1.0.3-windows-x64\passportsim.exe'
& $exe doctor                                   # 自检
& $exe start <bin|构建目录|pebundle> --label x
& $exe clock take --owner agent                 # 拿时钟租约才能推进
& $exe run --for 3s --timeout 30s
& $exe screenshot --save-as name                # 只能给名字，产物在 artifacts 目录
& $exe --output json <cmd>                      # 看结构化结果/错误原因
& $exe stop ; & $exe serve --stop               # 收尾（不停止会留 localhost:8765 守护进程）
```

配置文件在 `%APPDATA%\passportsim`，artifacts 在 `%LOCALAPPDATA%\passportsim\data\artifacts`。

---

## 10. 从源码构建并在模拟器里跑通（2026-11-14 完成）

目标：D 盘装好 ESP-IDF v5.5.3 → 构建 `FoloToy/ai-passport` → 产物喂给 PassportSim → 截图验证 UI。
**全部达成**，模拟器绑定的是我们自己编译的 ELF（SHA256 完全一致）。

### 10.1 最终成果

| 项 | 值 |
|---|---|
| ESP-IDF | `D:\esp\esp-idf-v5.5.3`（342 MB，`idf.py --version` = **ESP-IDF v5.5.3**） |
| 工具链 | `D:\esp\espressif`（3,414 MB：riscv32-esp-elf 14.2.0、cmake、ninja、openocd、ccache、esp-rom-elfs、python_env） |
| 工程 | `D:\esp\ai-passport`（`git clone` 成功，commit `b97765a`） |
| ELF | `build\FoloToy-AI-Passport.elf`，16,065,620 B，SHA256 `7c61ccf33a1229e320bdd78083cbfa51ee6a0ec2eb54b8716173e8d63fc1e257` |
| app bin | `build\FoloToy-AI-Passport.bin`，1,525,248 B（0x174600，占 8 MB 分区的 18%） |
| 合并镜像 | `build\merged-binary.bin`，1,590,784 B（`idf.py merge-bin`） |
| 截图 | `D:\dsh-work\sim\built-ui.png`，240×320，6,115 B |

### 10.2 关键认知：公开仓库 ≠ 产品固件

仓库自带的 `partitions.csv` 只有三行：

```
nvs,      data, nvs,     0x9000, 0x6000
phy_init, data, phy,     0xf000, 0x1000
factory,  app,  factory, 0x10000, 0x7f0000
```

而设备实际分区表有 petart / companion / imgava / assets / theme_cache / cardid / imgframe /
imgstore / imguser / audio / netcfg / recovery（十几个）。**公开仓库是 BSP 开发基座 + demo，
产品的小智语音应用（`trae_card 2.1.0-xiaozhi.29`）没有开源。**

所以 A 方案的产出是「能自己写固件 + 能仿真运行」，不是复现设备原界面。

### 10.3 网络：git 协议在这台机器上不可用

| 通道 | 结果 |
|---|---|
| `git clone`（GitHub） | ❌ POST 阶段 0 字节永久挂起（`ls-remote` GET 正常） |
| `git clone`（git.espressif.com.cn） | ❌ 同样挂起；该站也没有 archive/GitLab API 端点（全 404） |
| `https://dl.espressif.cn/dl/esp-idf/esp-idf-v5.5.3.zip` | ❌ 404（镜像不提供源码包） |
| codeload zip（单连接） | ✅ 但只 40–240 KB/s，69 MB 的 IDF 主包下了 **24.6 分钟** |
| **多连接并发** | ✅ 线性叠加，5–6 路并发实测 ~160–300 KB/s |
| `dl.espressif.cn`（工具链） | ✅ **871 KB/s**（`IDF_GITHUB_ASSETS=dl.espressif.cn/github_assets` 走这条） |

**codeload 不支持 Range 请求**（带 `-r` 仍返回 200 全量），所以大包无法分段，只能单连接慢慢拉；
但**不同文件之间**可以并发，子模块正是这么加速的。

### 10.4 手工拼装 ESP-IDF 的完整步骤

源码包不含子模块，也不含 `version.txt`（后者由 git 生成）。流程：

1. `codeload.github.com/espressif/esp-idf/zip/refs/tags/v5.5.3` → 69.3 MB → 展开到 `D:\esp\esp-idf-v5.5.3`
2. 用 GitHub API 查每个子模块 pin 的 commit：
   `api.github.com/repos/espressif/esp-idf/contents/<父目录>?ref=v5.5.3` → 取同名条目的 `sha`
   （**PS 5.1 必须先 `[Net.ServicePointManager]::SecurityProtocol = 'Tls12'`，否则 API 静默失败退化成 HEAD，
   拿到不兼容的新版源码**）
3. `codeload.github.com/<owner>/<repo>/zip/<sha>` 下载并展开到对应路径（16 个，共 28 MB）
4. **`version.txt` 必须手工补**：写入 `v5.5.3`，否则 `idf_component.yml` 的 `idf: ">=5.5.3,<5.6.0"` 校验不过
5. 删掉之前 clone 残留的 `.git`（避免 `git describe` 给出错误版本）

脚本：`D:\dsh-work\idf_fetch_subs.ps1`（子模块）、`D:\dsh-work\idf_build3.ps1`（构建）。

### 10.5 三个必踩的坑

| 坑 | 现象 | 解法 |
|---|---|---|
| **venv 里不能建 venv** | `install.ps1` 走到 "Setting up Python environment" 报 `This script was called from a virtual environment` | 用**非 venv 的 base python** 跑：`%APPDATA%\uv\python\cpython-3.11-windows-x86_64-none\python.exe tools\idf_tools.py install-python-env` |
| **中文用户名 + export.ps1** | `export.ps1` 生成的 activate 脚本路径里用户名乱码 → `. : The term 'C:\Users\????\...' is not recognized` | 先把 `$env:TEMP` / `$env:TMP` 指到纯 ASCII 目录（`D:\esp\tmp`）再 source `export.ps1` |
| **CMock 缺失** | cmake 报 `Include directory '.../components/cmock/CMock/src' is not a directory` | 补 `ThrowTheSwitch/CMock@eeecc49c` 子模块（IDF 会注册 cmock 组件，即使不跑单测） |

工具链安装本身没失败，`install.ps1` 退出码 1 只是卡在 Python 环境那一步；`install-python-env` 单独补跑即可。

### 10.6 让模拟器跑自己的固件：自定义 corpus 条目

**这是 9.2 那个「看不到 UI」问题的正解**（比 `start <构建目录>` 更好，因为构建目录不给 ELF）：

`%APPDATA%\passportsim\corpus.toml`：

```toml
[psim]
bin      = "D:/esp/ai-passport/build/merged-binary.bin"
elf      = "D:/esp/ai-passport/build/FoloToy-AI-Passport.elf"
boot_elf = "D:/esp/ai-passport/build/bootloader/bootloader.elf"
pt       = "D:/esp/ai-passport/build/partition_table/partition-table.bin"
```

只有 `bin` 是必需的；给了 `elf` → radio 能绑定、`ui`/`inspect` 能走 DWARF；给了 `pt` → 分区表已知。
路径可以写绝对路径（Windows 反斜杠也行）。

运行：

```powershell
$exe='D:\passportsim\passportsim-1.0.3-windows-x64\passportsim.exe'
& $exe doctor                                  # 应显示 "psim  found  4 file(s)"
& $exe start psim
& $exe screenshot --save-as build-ui
& $exe inspect fidelity --output json          # 看 receipt
```

实测 receipt（**tripwire 全消**）：

```json
"binding": { "app_elf_sha256": "7c61ccf3…e257",
             "features": { "ble": "bound", "wifi": "bound" },
             "idf_ver": "v5.5.3", "log_lines": { "wifi": "verified" } },
"hle": { "bound": "idf-5.5.3/ble,idf-5.5.3/wifi", "tripwires_hit": [] }
```

`boot: matched until_ui_settled`，`insns` 2706 万，`frame_gen=16`，截图
`built-ui.png` 显示蓝底 + FoloToy 标题 + Display(高亮)/Button/Audio/Battery/Wi‑Fi/BLE/Low Power
+ 底部草地上的像素机器人 —— 与我们编出来的源码一致（也和 9.1 的官方 demo 截图一致）。

> 小提示：`screenshot` 的产物在 `artifacts\run-*\p1\screens\`，不是 artifacts 根目录。

### 10.7 后续可做

- 改 `main/ui_pixel.c` / `demo_*.c` 就能改 UI，重新 `idf.py build` + `merge-bin` + 换 corpus 里的 bin/elf 即可仿真验证
- 真要上设备：本项目**没有 OTA 分区**（只有 factory），只能用 USB 烧；且产品固件与 demo 分区表不同，
  **不要**把这个 demo 镜像刷进设备（会破坏 petart/companion/cardid 等分区）
- 若要让仿真更接近真机，可把设备备份里的 assets/petart 分区内容按 demo 分区表重新摆放