# `server/dsh` — dsh 连接器

## 三条路，两条已验证

| 后端 | 状态 | 说明 |
|---|---|---|
| `mock.py` | ✅ 默认 | 确定性回复，供 mock 全链贯通与 20 轮回归 |
| `cli.py` | ✅ **已对真实 dsh 验证** | `dsh --profile headless "<task>"`，一轮一个进程 |
| `http.py` | ⚠️ 未验证 | 需要先确认 dsh 暴露的 HTTP/会话入口，不许猜 |

## 实测（2026-11-14，本机）

```
/opt/dsh-021/node_modules/.bin/dsh --profile headless "只回答四个字：链路正常"
→ 链路正常，4.83 s / 5.13 s（两次）
```

拆开看：node 启动约 2.5 s，模型往返约 2.5 s。**结论：一条语音回复光连接器就付掉 5 秒**，
做通知可以，做对话不行（目标是从"说完"到"开口" 1.5 s 以内）。

## 两个必须知道的坑

1. **PATH 上的 `dsh` 不能用**：新版启动即失败
   `credentials-local: the value for "version" in /root/.dsh/.credentials.yaml must be a string`
   —— 该文件是 `version: 1`（整数），新版要求字符串。而正在跑的飞书桥用的是
   `/opt/dsh-021`（0.2.1），它对整数宽容。所以调用要用桥那版二进制，
   **不要**为了让 CLI 跑起来去改共用的 `.credentials.yaml`（会影响桥与 web）。
2. **headless profile 需要 `DEEPSEEK_API_KEY` 在环境里**，否则
   `MISSING_CREDENTIAL: llm-deepseek ... route "deepseek-official"`。桥有它自己的账号来源，
   CLI 没有。

## 下一步（按价值排序）

1. **常驻会话**是唯一能把 5 s 压到 1 s 的办法。候选：dsh web（本机 `:3080`，桥同版本）的
   HTTP/会话接口、或 Feishu 桥的本地入口。要先**读**它的实际协议，再写适配器（接口仍是
   `Brain.astream`，上层不用动）。
2. 若接受 5 s：先做"长任务播报"场景（说完就播"已开始处理"，完成后再播结果），
   延迟被叙事吸收，不破坏对话体感。

## 边界（不要在这层做的事）

- 不实现权限提升：语音文本**不**因为「是语音」而获得更高操作权限，副作用操作走 dsh 既有确认流程。
- 不把长任务取消绑到播放打断上：用户打断播报 ≠ 取消后台任务。
- 不在日志里记录完整转写文本之外的隐私内容。