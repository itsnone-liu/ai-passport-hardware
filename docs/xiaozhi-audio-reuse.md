# 小智（FoloToy ai-passport fork）音频路径：能否复用、值不值得复用

审阅对象：<https://github.com/FoloToy/folo-ai-passport-xiaozhi>（78/xiaozhi-esp32 的 Passport 专用 fork，
`main/CMakeLists.txt:80` 把 `BOARD_DIR` 固定成 `folotoy/ai-passport`）。
本地基线：`D:\esp\ai-passport`（ESP-IDF 5.5.3，ESP32-C3，8 MB flash，无 PSRAM）。
依据：仅**读码 + 引用上游配置 + 引用我们自己的真机日志**；本次无硬件，任何性能数字都不是实测。

标记约定：`[引用: 文件:行]` = 读到的代码/配置；`[实测]` = 我们自己在真机上跑出来的数字（出处另注）；
`[估算]` = 我按代码结构推的量级；`[推断]` = 未验证的行为判断。

## 0. 结论

**不引入 Opus，不移植上游 audio_service，保持现有 16 kHz PCM 自行实现，只借三个细节。**

| 选项 | 结论 | 依据（详见对应章节） |
|---|---|---|
| 把 Opus 编码/解码加进现有固件 | **不做** | 会话进行中实测剩余堆 28.5 KB / 最大块 18 KB，最差 5.3 KB 已崩；Opus 全链条 `[估算] 76–110 KB`（§2） |
| 移植上游 audio_service（C++） | **不做** | 它自带 I2S 通道创建，与我们的 BSP 冲突；且硬绑 Opus 队列模型（§4） |
| 只借细节 | **做** | ①启采后丢 120 ms 旧帧 ②10 ms 读粒度 ③空闲 15 s 关 codec + duplex 时保留 TX 时钟（§3） |
| 切小智云端 | 只在"整机切固件"前提下成立 | 分区表完全不同，整片覆盖，退路是整片备份（§6） |

## 1. 本地基线（读到）

| 事实 | 证据 |
|---|---|
| 采集：16 kHz/单声道/PCM16，640 B=20 ms 一帧，pre-roll 15 帧，直接阻塞读 | `main/terminal_capture.c:19-21`、`:168` |
| 播放：60 帧环 38,400 B（静态 `.bss`），预缓冲 15 帧，自走 20 ms 时钟 | `main/terminal_playback.c:22-23`、`:28` |
| I2S：全双工单通道对，`dma_desc_num=6`、`dma_frame_num=240`、`auto_clear_after_cb=true`、MCLK=256×fs | `components/bsp/src/bsp_audio.c:306-309`、`:321` |
| codec：`esp_codec_dev` 1.6.2，`es8311_codec_new` + `esp_codec_dev_open`，in-gain 30 dB | `bsp_audio.c:359-369`、`:450-457`、`:467` |
| 开关：格式变化才 close+open；同格式复用；另有 `bsp_audio_sleep/wake`（ES8311 完整 suspend） | `bsp_audio.c:436`、`:487-507`、`:536-555` |
| 静态内存：`.data`+`.bss` = 0x3fc98e00→0x3fcb2900 ≈ 105,216 B，其后才是堆 | `build/FoloToy-AI-Passport.map` 的 `_data_start/_bss_end` `[实测]` |
| 运行期堆：空闲 118,624 / 最大块 106,496；16 KB 窗口有会话 28,564/18,432；32 KB 窗口 + 真人说话 5,324 → 崩 | `docs/archive-2026-11-14-voice-m1.md:176-181`、`audio-bringup.md:55` `[实测]` |

## 2. Q2 先答（它决定了其余四问的取舍）：C3 上 Opus 的真实开销

上游配置（读到的都是"它自己写在代码里的选择"，不是我猜的）：

| 项 | 上游取值 | 证据 |
|---|---|---|
| 采样率/声道/帧长 | 16 kHz、mono、**60 ms**（960 采样，1920 B PCM/帧） | `main/audio/audio_service.h:40`、`config.h` 的 `AUDIO_*_SAMPLE_RATE 16000` |
| 编码参数 | `complexity=0`、`bitrate=AUTO`、DTX 开、VBR 开、FEC 关 | `audio_service.h:67-79` |
| 重采样 | 只在 codec 采样率≠16 kHz 时创建（Passport 上 in/out 都是 16 kHz → 不创建） | `audio_service.cc:73-80`、`config.h` 注释"避免 C3 上重采样临时缓冲打碎 SRAM" |
| 编码任务栈 | `2048 * 12` = **24 KB** | `audio_service.cc:170` |
| 采集/播放任务栈 | 4 KB / 2 KB（lite 引擎路径） | `audio_service.cc:145-160` |
| 队列深度 | send 40 包、decode 20 包、encode/playback 各 2 帧 | `audio_service.h:41-44` |
| 组件 | `espressif/esp_audio_codec ~2.5.0`、`esp_audio_effects ~1.3.0` | `main/idf_component.yml` |

分项开销（**全部 `[估算]`**，上游没公布状态区大小，我也没编过这个组件）：

| 项 | 估算 | 为什么是这个量级 |
|---|---|---|
| 编码器状态 | 25–45 KB | 16k/mono/complexity 0 的定点 Opus 编码器状态；复杂性档位只影响搜索，不影响状态区主体 |
| 解码器状态 | 15–25 KB | 同族解码器，无编码侧分析状态，明显更小 |
| 编码输出缓冲 | 1–4 KB | 由 `esp_opus_enc_get_frame_size()` 返回，值不可预知（`audio_service.cc:69`） |
| 任务栈 | 24 KB | `[引用: audio_service.cc:170]`，这是**栈**不是状态，两者相加 |
| 队列+在飞 PCM | 12–16 KB | 2×1920 B 在飞帧 + 40/20 个包的 vector 头与载荷 |
| **合计新增** | **约 76–110 KB** | — |

结论：**在无 PSRAM 的 C3 上，Opus 能跑（上游就是这么发的），但它是"整机重写内存预算"级别的决定，不是加个库**。
我们自己的实测摆在这里：会话进行中只剩 28.5 KB 可用堆、最大连续块 18 KB，32 KB TCP 窗口下真人说话直接掉到 5.3 KB 并崩溃
[实测，见 §1]。76–110 KB 的新增需求在当前架构上没有落点。
CPU 侧同样不能乐观：C3 无硬件 FPU，Opus 只能走定点路径 `[推断]`；`complexity=0` 正是上游为了不把 LVGL 与链路任务
一起饿死才选的（`FoloToy` 同仓库的第三方实测记录 `docs/reference/shinku-chen/ble-voice-uplink-and-pairing.zh_CN.md:37`
原话："更高档位会把 LVGL 与链路任务也要用的核心吃满"）。单帧编码耗时我没有数据，不给数字。

对屏幕/Wi-Fi/按键的影响：上游没给量化数据，我只能说结构上它把编解码放进一个优先级 2 的独立任务
（`audio_service.cc:170`），采集 8、播放 4（`:151`、`:160`），比我们要低——这是它承认"编解码很吃 CPU"的间接证据。

## 3. Q1 不改 BSP，能否稳定 16 kHz PCM 读写；上游有没有值得借的时序/增益/启停

能——我们已经能读能写，这一问的实质是"有没有我们缺的东西"。逐项对照后，**增益与启停没有新东西，时序有三点可借**：

| 维度 | 上游做法 | 我们现状 | 判断 |
|---|---|---|---|
| 输入增益 | 固定 `input_gain_ = 30` dB（`codecs/es8311_audio_codec.cc:19`） | 也是 30 dB（`bsp_audio.c:467`） | 一样，无借鉴 |
| 输出音量 | 默认 70，百分比接口（`audio/audio_codec.h:64`） | 百分比接口（`bsp_audio.c:567`） | 一样 |
| 读粒度 | 每次只读 **160 采样 = 10 ms**，攒到 60 ms 再编码（`audio_service.cc:311`） | 一次读 20 ms（`terminal_capture.c:168`） | 粒度更细对"停得准"有帮助，可借 |
| I2S 通道参数 | desc 6 / frame 240 / MCLK 256× / stereo slot（`audio_codec.h:15-16`、`codecs/es8311_audio_codec.cc:115-175`） | 完全相同（`bsp_audio.c:306`、`:321`） | 一致，说明我们的 BSP 是同一套配置 |
| 启采时机 | `EnableVoiceProcessing(true)` → 打 `audio_input_need_warmup_`，输入任务 `vTaskDelay(120ms)` 后丢弃一次读（`audio_service.cc:276-279`、`:717`） | 无丢弃，`bsp_audio_set_format` 之后读到的第一批帧直接进 pre-roll | **该借**，见下 |
| 采→播交接 | 自动模式等播放队列排空再开麦（`application.cc:1042-1046`） | 由服务端顺序保证 | 我们的协议已覆盖，不必借 |
| 重采样 | 仅必要才建（`audio_service.cc:73`） | 不重采样 | 无借鉴 |

`120 ms` 这个数字有物理含义：6 个描述符 × 240 帧 = 1440 帧 = 90 ms 的 DMA 深度（16 kHz 下），
所以"打开 codec 后前 120 ms 的数据"里必然混着上一轮/复位前的旧内容 `[推断，基于 DMA 深度实算]`。
我们的 BSP 与上游 DMA 深度完全一致，因此**我们同样需要这个丢弃窗口**：现在 `terminal_capture.c:151-158`
在 `bsp_audio_set_format` 成功后立刻读帧并填 pre-roll，理论上前 90 ms 可能是旧帧。
建议：`begin_turn` 前（或 `format_ok` 由 false 变 true 时）多读 6 帧直接丢弃，不计数、不进 pre-roll。

## 4. Q3 移植到 5.5.3 的依赖差异

**最小文件集**（读 `main/CMakeLists.txt:2-47` 得到的音频相关编译单元）：

| 文件 | 用途 | 移植难度 |
|---|---|---|
| `main/audio/audio_codec.h/.cc` | 抽象基类 + `OutputData/InputData` | 低 |
| `main/audio/codecs/es8311_audio_codec.h/.cc` | ES8311 + I2S 全双工 | **中：自带 `CreateDuplexChannels`，会与我们 BSP 重复建 I2S 通道** |
| `main/audio/audio_service.h/.cc` | 采集/播放/编解码三任务 + 队列 | 高（依赖 Opus + 引擎） |
| `main/audio/engines/lite_audio_engine.h/.cc` | C3 用这个（无 AEC/VAD） | 中 |
| `main/audio/fixed_queue.h`、`ogg_demuxer`、`protocols/*` | 队列 / 提示音 / 传输 | 中 |
| `main/audio/engines/afe_audio_engine.cc` | **C3 用不到**：`Kconfig.projbuild:451-458` 明确规定 `USE_AUDIO_PROCESSOR` 依赖 S3/P4/S31 + SPIRAM | 不需要 |

**IDF 6.x 才有的东西 / 版本门槛**（前两条是硬门槛，其余是我逐项查过的）：

| 项 | 事实 | 证据 |
|---|---|---|
| 组件管理器版本门 | `idf: '>=6.0.1'`，5.x 直接拒绝解析 | `main/idf_component.yml` 末段；README 第 19 行"ESP-IDF 5.x is no longer supported" |
| `i2s_port_t` 移除 | 上游用 `ESP_IDF_VERSION >= 6.0.0` 宏区分（`XIAOZHI_I2S_PORT`） | `audio/audio_codec.h:21-25` |
| `I2S_HW_VERSION_2` | **5.5.3 里不存在这个宏**（只有 `SOC_I2S_HW_VERSION_2=1`，查 `components/soc/esp32c3/include/soc/soc_caps.h`），所以 `codecs/es8311_audio_codec.cc:134/146` 两段 `#ifdef` 在 5.5.3 下是死代码 → `.ext_clk_freq_hz`、`.left_align` 等字段退化为 0。16 bit 填满 slot 时大概率无副作用 `[推断]`，但照抄等于换了一份 slot 配置 | 读码 + 查本地 IDF 头文件 |
| `esp_lcd` | `esp_lcd_panel_io_spi_config_t.trans_queue_depth`（`esp_lcd_io_spi.h:27`）与 `esp_lcd_panel_dev_config_t.rgb_ele_order`（`esp_lcd_panel_dev.h:25`）在 5.5.3 都已存在 | 查本地 IDF 头文件 |
| 新 I2C 驱动 | `driver/i2c_master.h`（`ai_passport_board.cc:13`）5.2+ 可用 | 同上 |
| 第三方组件 | 本地已有 `esp_codec_dev 1.6.2`、`lvgl 9.5`、`esp_lvgl_port 2.9.0`、`button 4.2.0`（上游 4.2.1）；**缺** `esp_audio_codec 2.5.0`、`esp_audio_effects 1.3.0`，能否装在 5.5.3 上我**没验证**（组件仓库的版本要求取不到） | `components/bsp/idf_component.yml` vs 上游 `main/idf_component.yml` |

结论：`esp_lcd`/I2C 不是障碍，真正的门槛是**它的音频服务与 Opus 组件、以及 codec 类里重复建 I2S 的结构**。
"能编过"和"能在我们 BSP 上跑"是两件事——上游 codec 类在构造路径里就 `i2s_new_channel`
（`codecs/es8311_audio_codec.cc:115-127`），与 `bsp_audio_init` 会撞通道。

## 5. Q4 停止后如何释放 I2S DMA、清旧帧、避免爆音

上游的做法（**它没有直接 `i2s_channel_disable`**，全靠 codec 生命周期）：

| 目标 | 上游实现 | 证据 |
|---|---|---|
| 停采/停放 | 不打 I2S，只 `EnableInput(false)`/`EnableOutput(false)`；两者都 false 时 `esp_codec_dev_close` + `esp_codec_dev_delete`（内部会 disable I2S 通道），PA 脚拉到无效电平 | `codecs/es8311_audio_codec.cc:84-113`、`:181-205` |
| 何时停 | 每 1 s 检查，输入/输出各自空闲超 **15 s** 才关；关完两者都停则停掉定时器 | `audio_service.cc:833-853`、`audio_service.h:48` |
| 双工约束 | 注释原文："Keep TX clock when duplex RX is active; otherwise RX may stall on some boards"——即单 I2S 全双工板上**采着就不能停 TX** | `audio_service.cc:846` |
| 旧帧 | 开麦时 `ResetDecoder()` + 重采样器 `reset` + 120 ms 丢弃（见 §3） | `audio_service.cc:716-723`、`:276-279` |
| 打断 | `ResetDecoder()` 清 decode/playback/testing 三队列 + `esp_opus_dec_reset` + `playback_generation_` 递增，在飞任务凭 generation 判失效 | `audio_service.cc:798-818` |
| 爆音 | **没有淡入淡出**；Passport 的 `AUDIO_CODEC_PA_PIN = GPIO_NUM_NC`（`config.h`），没有功放静音脚可用，所以切歌/停播只能靠 codec 关闭时的静音，边界处就是硬切 | `config.h`、`audio_service.cc:844-849` |

对我们的启示（三条，按性价比排序）：
1. **我们现在是持续运行 I2S、从不停**，这本身没错，但缺少"旧帧窗口"——这是唯一会造成"用户听到自己上一句话"的真问题，务必补（§3）。
2. 空闲关 codec 我们已经有更彻底的工具：`bsp_audio_sleep()`（完整 ES8311 suspend 序列 + 显式停 I2S TX/RX，`bsp_audio.c:487-507`），
   上游那套 15 s 定时器对我们**没有增量价值**，不要抄。
3. 播放停止时我们的 `auto_clear_after_cb=true` 会让 DMA 自动补零（`bsp_audio.c:308`），
   这在"下一帧数据不再到来"时表现为突降到静音=咔哒声 `[推断]`。若要消爆音，正确做法是写 PCM 时对首尾 5–10 ms 做线性斜坡
   （纯软件，零额外内存），上游没做，这属于我们可以自己加的改进。

## 6. Q5 用小智云端：要不要整机换固件、刷写范围与恢复风险

**要。** 它不是"加个协议"，是另一个完整固件（README：本仓库只启用 Passport 一个板型，输出 `build/merged-binary.bin` 给 `0x0`）。

分区表对比（这是最直接的冲突证据）：

| | 上游 `partitions/v2/8m.csv`（`config.json` 指定） | 我们 `partitions.csv` |
|---|---|---|
| nvs | 0x9000，**0x4000** | 0x9000，**0x6000** |
| otadata | 0xD000，0x2000 | **无** |
| phy_init | 0xF000 | 0xF000 |
| app | `ota_0` 0x20000/0x2f0000 + `ota_1` 0x2f0000 | 单个 `factory` 0x10000/0x7f0000 |
| 资源 | `assets` spiffs 2 MB @0x600000 | 无（真机实际还有 petart/companion/imgava/theme_cache/cardid/imgframe/imgstore/imguser/audio/netcfg/recovery 等厂商数据分区，早前读机记录，仓库 csv 只有三行） |

由此得出三个硬结论：
1. **上游的 otadata(0xD000–0xF000) 正好压在我们 nvs 的后半段**。刷上游镜像等于连带抹掉我们的 nvs（Wi-Fi 凭据、设备配置）。
2. 上游 `ota_0` 从 0x20000 开始，我们仓库 csv 的 `factory` 从 0x10000 开始、名义 0x7f0000（真机被截成 2.8 MB 让位给厂商数据分区）——刷写必然覆盖我们整个应用，并越界覆盖厂商资源/密钥分区
   （cardid 里的 DeviceKey 一旦丢失，原厂云激活与身份恢复都不可逆）。
3. 上游 README 自己承认这条路的风险（第 62 行）："A raw merged image overwrites the gaps between segments and may overwrite
   NVS configuration; … This project does not back up existing firmware before flashing." 它要求按 `build/flash_args` 分段写并保留身份/配置分区，
   但我们这块板的身份分区（cardid）与它的分区表**没有对应关系**，分段写也保留不了。

刷写范围与恢复：整片 8 MB 覆盖（0x0 起）。唯一可靠退路是我们已有的整片备份
`D:\dsh-work\flash\full-backup.bin`（MD5 6E69D1BC3711CB6C778BFCCA70FD6C56，已校验）→ 恢复即 `esptool write_flash 0x0`。
恢复风险清单：①设备无 OTA 分区，变砖只能 USB Serial/JTAG 恢复，而 USB 口与按键共用 GPIO0 的 ADC 链路，需确认下载模式可复现；
②厂商资源包带版本校验（早前读到"Missing/mismatched resource pack"），部分恢复=启动即报错；③cardid 分区若被覆盖，原厂激活链路失效（即使刷回旧 app）。

## 7. 一句话落地建议

保持现状：16 kHz PCM + 20 ms 帧 + 自己的 capture/playback；**只做两件事**——启采后丢弃 120 ms 旧帧（§3），
以及播放首尾 5–10 ms 线性斜坡（§5 第 3 条）。Opus 与小智云端都归到"整机换固件"这一类动作，
在整片备份可恢复、且明确接受"厂商素材/身份分区全部丢失"之前，不动。

## 8. 我没读到 / 没验证的东西（诚实清单）

| 项 | 状态 |
|---|---|
| `main/boards/common/board.cc`、`wifi_board.cc` | 未读到：我先猜的 `main/board.cc` 是 404，没有再翻目录定位正确路径 |
| `main/audio/fixed_queue.h`、`ogg_demuxer.*`、`wake_words/esp_wake_word.cc` | 已知存在但未逐个读 |
| `main/idf_component.yml` 全文 | 已读；其中组件的 IDF 版本要求未展开 |
| `esp_audio_codec` 源码 / `esp_opus_enc` 状态区大小 | **没有**：只读了它的 README，没有任何内存数字 → §4 的表全部是估算 |
| `esp_audio_codec`、`esp_audio_effects` 能否装到 5.5.3 | **未验证**（registry 版本约束取不到，本机也没试装） |
| 78/xiaozhi-esp32 主项目本体 | 未读，本报告全部结论基于 FoloToy fork |
| 上游 8 MB merged 镜像的实际分区表内容 | 未下载构建产物验证，仅按 `8m.csv` + `config.json` 推断 |
| 任何 CPU 占用 / 欠载率 / 帧时延 | **无实测**；无硬件，全部标注为估算或引用 |
| 我这边"改完就更好"的两条建议（120 ms 丢弃、PCM 斜坡） | 逻辑推导，**未在真机验证** |