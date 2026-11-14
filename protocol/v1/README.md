# `protocol/v1` — PAV1 测试向量

本目录是 [`docs/voice-protocol-v1.md`](../../docs/voice-protocol-v1.md) 的可执行部分：
协议里每一句「怎么组帧、怎么切包、错了怎么办」都对应这里一个字节序列。

```
make_vectors.py  生成器（纯标准库，无依赖）
vectors.json     生成的黄金向量（提交入库，禁止手改）
```

## 用法

```bash
python3 make_vectors.py            # 重新生成
python3 make_vectors.py --check    # 校验入库文件是否与生成器一致（CI/提交前跑）
```

## 两端怎么用同一份向量

- **Python（Ubuntu 侧）**：直接读 `vectors.json`，对每个 `streams[]` 元素按 `segments_hex`
  顺序喂给解析器，断言产出的帧序列与 `expect.frames` 完全一致、`expect.error` 一致。
- **C（Windows 侧）**：两种做法任选其一——① 构建时用脚本把 `vectors.json` 转成
  `static const uint8_t[]` + 期望表；② 把 `make_vectors.py` 的组帧逻辑照抄成 C 的
  `pav1_test_vectors.c`，再断言两侧的 SHA-256 相同。**不允许**只挑几个用例手测。

## 字段说明

| 字段 | 含义 |
|---|---|
| `frames[]` | 单帧：`wire_hex` 是完整字节，`header` 是解出的头字段 |
| `streams[]` | 一条连接上的字节流：`segments_hex` 是 `recv()` 的切分（TCP 可以任意切/合） |
| `streams[].expect.frames` | 解析器**应当**依次产出的帧 |
| `streams[].expect.error` | 应当报出的错误码（`null` = 无错误） |
| `control[]` | 控制通道的请求/应答样例（JSONL） |
| `auth_vectors` | 固定 secret + nonce 的 HMAC 期望值，用于双向校验实现 |

## 注意

- `frames[]` 与 `streams[]` 里出现的 `wire_hex` 都是**线上真实字节**，可直接 `xxd -r -p` 落盘。
- `auth_vectors.test_secret` 是测试专用常量，真实设备密钥永不进仓库。
- `duplicate_end` 用例里解析器会合法地产出两个 END 帧——**幂等是会话层的责任**，不是解析器的。
  同理 `turn_closed` / `turn_mismatch` / `not_authed` / `auth_failed` 需要会话状态才能判定。
- 向量一旦冻结，改动必须走协议变更流程（见协议 §13）。