# USB 枚举与芯片识别

## 主机侧看到的东西

```
USB VID:PID   303A:1001        # Espressif USB-Serial/JTAG（USB 原生，非 CH340）
设备名        USB JTAG/serial debug unit
串口          COM3（Windows）
```

枚举方法（PowerShell）：

```powershell
Get-PnpDevice -PresentOnly | Where-Object { $_.InstanceId -like 'USB\VID_303A*' } |
  Select-Object Status, Class, FriendlyName, InstanceId
```

`303A:1001` 说明固件启用了 **USB-Serial/JTAG 外设**（`CONFIG_ESP_CONSOLE_USB_SERIAL_JTAG=y`），
这也是为什么烧录只需要一根 USB 线、不需要外部 USB-UART 芯片。

> 该板的 UART0 TX 与屏幕背光共用一个 GPIO，所以厂商选了 USB-Serial/JTAG 做控制台 —— 这一点在
> 官方仓库 `sdkconfig.defaults` 里有对应的注释。

## 芯片

```
Chip type:          ESP32-C3 (QFN32) (revision v1.1)
Features:           Wi-Fi, BLE, Embedded Flash 8MB (XMC)
Crystal frequency:  40 MHz
USB mode:           USB-Serial/JTAG
MAC:                98:C3:77:XX:XX:XX   （已脱敏；真实值由 esptool read_mac 得到）
```

| 项 | 值 |
|---|---|
| 架构 | 单核 32 位 RISC-V，最高 160 MHz |
| Flash | **片内 8 MB**（XMC），无外挂 SPI flash |
| PSRAM | 无 |
| 无线 | Wi-Fi 4 (2.4G) + BLE 5.0（NimBLE，仅 peripheral） |

## 备份方法（esptool）

```powershell
# 全量 8MB 读取 + 校验
esptool.py --chip esp32c3 -p COM3 -b 460800 read_flash 0 0x800000 full-backup.bin
esptool.py --chip esp32c3 -p COM3 verify_flash 0 full-backup.bin   # -> "digest matched"
```

实测该芯片在 460800 波特率下整片读取约 2 分钟；中途掉线可重连后用
`read_flash <offset> <len> <part>` 分段续读。

## eFuse 状态

```
espefuse.py --chip esp32c3 summary
```

关键位全为 0：

| 项 | 状态 |
|---|---|
| Secure Boot | disabled |
| Flash Encryption | disabled |
| eFuse 内容 | 空白（仅出厂标定字） |

意味着固件可以被随意读出、修改、回刷，无需签名。