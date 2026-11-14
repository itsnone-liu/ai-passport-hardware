# tools/ —— 现场脚本存档

这些是 Windows 侧用于构建、刷机、观测和诊断的脚本，**不是固件的一部分**，但它们是
"当时到底怎么得出那个结论"的现场记录，所以一起存档。

## 先说最容易踩的一脚

```powershell
$env:PYTHONPATH='D:\dsh-work\tools'   # 必须设置，否则你的脚本会复位设备
```

`sitecustomize.py` 会在 Python 启动时包装 `serial.Serial.open()`，在 open 之前把
`dtr` / `rts` 都置为 `False`。不设这个环境变量，`serial.Serial("COM3", 115200)` 就会拉
DTR——而 DTR 对 ESP32-C3 就是复位信号。

这件事的代价：曾经有 13 个脚本都在复位设备，导致"连接失败""设备在崩溃"这类结论全部失真，
对面服务器还累计了 115 次误报的 hello。**修完这一层，之前那些"故障"大半自己消失了。**

## 脚本分类

| 脚本 | 用途 |
|---|---|
| `sitecustomize.py` | **系统级修复**：所有 pyserial 打开串口都不再复位设备 |
| `verify_noreset.py` | 验证上面那件事：连开 4 次串口，`boot` 应当不变 |
| `wait_ready.py` | 等认证 + 音频通道就绪，打印堆与 `rx/tx` |
| `snap.py` | 一次取全 `get_status` + `get_audio` + 相关日志行 |
| `watch_key.py` | 监听按键事件与采集状态变化（收发合在一个进程里，因为 COM3 独占） |
| `path_check.py` | 设备侧到三个锚点的 RTT |
| `crash_selftest.py` | 发 `vendor.crash` 故意 panic，验证复位取证是否正确归因 |
| `raw_log.py` | 按关键词过滤串口原始日志 |
| `play_test.py` | 发一条 JSON 命令并收应答 |
| `capture_test.py` / `preroll_test.py` / `playback_test.py` | 采集、pre-roll、播放的端到端自测 |
| `sink_server.py` | TCP 洪泛服务端，测下行吞吐 |
| `ps_ab.py` / `ps_ab2.py` | Wi-Fi 省电模式 A/B |
| `probe_sink.py` / `tcp_lab_server.py` | 连通性探针 |
| `parse_esp.py` / `parse_img.py` / `analyze_firmware.py` / `dump_app.py` | 固件镜像解析（早期硬件调研用） |
| `idf_build3.ps1` / `idf_flash.ps1` | 构建与刷机（看 `build exit=0` / `flash exit=0`） |
| `gh_push_api.ps1` | 通过 API 推送（本机 git push 不通时的替代路径） |
| `sanitize_repo.ps1` / `wait_and_backup.ps1` | 仓库清理、备份等待 |

## 判断成功与否的几个记号

- 构建/刷机：`build exit=0`、`flash exit=0`。**尾随的 `[exit code: 1]` 是噪声**。
- 串口日志是 GBK 乱码：只 grep ASCII 安全词（`auth:`、`pav1:`、`cap:`、`boot:`），
  不要匹配中文。
- 任何"设备自己重启了"的结论，先看 `get_status` 的 `boot` / `reset` / `reset_code`，
  再下判断。`reset_code=11` 是 USB 复位，也就是刷机，不是崩溃。