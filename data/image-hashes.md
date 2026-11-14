# 镜像尺寸与哈希

镜像本身**不入库**（版权内容 + 体积），这里只留指纹，便于日后校验自己手里的备份是否同一版本。

## 设备备份（本地保存，未上传）

| 文件 | 大小 | 算法 | 值 |
|---|---|---|---|
| `full-backup.bin`（整片 8 MB） | 8,388,608 B | MD5 | `6E69D1BC3711CB6C778BFCCA70FD6C56` |
| 同上 | | SHA-256 | `E1993D959ACFF4FC05182B2AC3E02FA93F0E51C99B050431AB9B72008D4C1AC9` |
| `merged-app.bin`（bootloader + 分区表 + app，供模拟器加载） | 3,006,464 B | SHA-256 | `123903c0affd16bed43ac6c20add2890e3a5205da5a48026a7ada31bdad3345c` |
| `full-assets.bin`（补齐素材分区的加载镜像，nvs 已抹除） | 6,725,632 B | SHA-256 | `63bc2b9c1400ddc26b218dee406b066a5cc87321f122dfae336944ee03837952` |

`verify_flash` 对整片备份返回 `digest matched`，说明读取无误。

> 两个"加载镜像"是为了喂给模拟器而重组的：`nvs` 与 `cardid` 区域被抹成 0xFF
> （模拟器有凭据保护，遇到真实凭据会 `E_SECRET_REFUSED` 拒绝加载）。

## 设备固件自身的标识

```
project_name    : trae_card
app version     : 2.1.0-xiaozhi.29
idf version     : v5.5.3
compile time    : Nov 12 2026 22:42:48
board           : ai-passport-leogie
app ELF SHA-256 : 47c6aba7……789aed   （截断，原始值见 esptool image_info 输出）
```

## 从开源基座自建的固件（ESP32-C3 / IDF 5.5.3，commit b97765a）

| 文件 | 大小 | SHA-256 |
|---|---|---|
| `FoloToy-AI-Passport.elf` | 16,065,620 B | `7c61ccf33a1229e320bdd78083cbfa51ee6a0ec2eb54b8716173e8d63fc1e257` |
| `FoloToy-AI-Passport.bin` | 1,525,248 B | 占 app 分区 18%（0x174600 / 0x7F0000） |
| `merged-binary.bin`（`idf.py merge-bin`） | 1,590,784 B | 供模拟器直接加载 |

> 这份 ELF 的 SHA-256 与模拟器 `inspect fidelity` 回执里的 `app_elf_sha256` 完全一致，
> 证明模拟器绑定的确实是它，而不是内置的官方 demo。