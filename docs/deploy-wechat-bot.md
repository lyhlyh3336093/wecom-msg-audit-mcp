# 微信群机器人（wxbridge）部署与验收

> 前置阅读: [实施计划](../docs/plans/2026-11-24-001-feat-wechat-group-bot-migration-plan.md)（R1-R5、AE1/AE2）
> 风险声明: 个人微信自动化属灰色地带，**必须使用专用小号**，不涉及常用账号。

## 1. 前置条件

| 项 | 说明 |
|---|---|
| 专用微信号 | 新注册小号，完成实名，加入目标微信群 |
| PC 微信（4.x） | 登录专用小号；**关闭自动更新**（wxautox4 绑定客户端版本） |
| wxautox4 | `pip install wxautox4`（付费，一机一码按年；import 名以实际包为准） |
| RAG 服务 | aihub-knowledge 已运行（`http://127.0.0.1:8001`），`RAG_API_TOKEN` 有效 |
| 桌面会话 | 自动登录 + 禁止锁屏（锁屏/RDP 断开会使 UI 自动化失效）；微信窗口保持打开不被遮挡；显示器可熄屏 |

## 2. 配置（.env 追加）

```ini
# 目标群（逗号分隔群名，仅白名单群启用；新增群需人工确认）
WXBOT_LISTEN_CHATS=测试群
# 触发关键词，默认 @智能小助手
WXBOT_MENTION_KEYWORDS=@智能小助手
# 兜底文案（不读容器内 gateway.db，独立配置）
WXBOT_FAILURE_TEXT=抱歉，这条消息暂时没能处理，已经记录，稍后跟进。
# 限速/队列/熔断（默认值见 wecom_bot/wxbridge.py）
WXBOT_RATE_SECONDS=2
WXBOT_QUEUE_MAX=10
WXBOT_CIRCUIT_THRESHOLD=3
WXBOT_CIRCUIT_COOLDOWN_SECONDS=60
```

安全约束：宿主机 wxbridge 只读以上白名单键 + RAG_API_URL/TOKEN（代码强制过滤，见 `load_wxbot_config`）；完整 `.env`（含企微 Secret/私钥）仅存在于容器挂载目录，不要拷到宿主机日常环境。

## 3. 真机探针（验收第一步，必须先做）

1. 专用小号登录 PC 微信，打开目标群
2. 让群成员发一条 `测试@智能小助手`，用 wxauto 示例脚本打印提取到的 `msg.content`
3. 确认两点：**文本包含字面 `@智能小助手`**；**机器人在群内显示昵称精确为「智能小助手」**
4. 任一不满足 → 调整群昵称或改 `WXBOT_MENTION_KEYWORDS`，否则适配器永远不触发

## 4. 启动与停止

```powershell
# 前台试水（先跑通）
uv run python -m wecom_bot.wxbridge

# 常驻（计划任务，开机自启）
# 详见 scripts/wddog/register-task.ps1 同款方式注册，动作改为:
#   powershell -NoProfile -ExecutionPolicy Bypass -File <repo>\wecom_bot\wxbridge_start.ps1
```

- 停止：前台 Ctrl+C；计划任务 `Stop-ScheduledTask`
- 日志：控制台 / 计划任务重定向文件

## 5. 微信自动更新锁定

- 设置 → 关于微信 → 关闭「自动更新」；删除或改名 `WeChatUpdate` 相关计划任务
- wxautox4 绑定客户端版本，升级后 UI 结构变化会导致失效；升级前先看 wxautox4 版本公告

## 6. 验收清单

- [ ] 探针通过（第 3 节）
- [ ] **AE1**：群成员 @机器人提问（知识库有相关文档）→ 机器人在原群回复含答案文本
- [ ] **AE2**：知识库无相关内容 / RAG 停掉 → 机器人回复兜底文案，不静默、不刷屏
- [ ] 熔断观察：RAG 停掉时连续 3 次失败后，后续 @ 快速收到兜底（不等 120s 超时）
- [ ] 企微渠道回归：企微群 @智能小助手 仍正常（两渠道消息源不同，天然隔离）
- [ ] 试水期记录：有人值守时段累计 20 次问答成功且无误触发（进入第二阶段 wcferry 评估）

## 7. 与企微渠道并存

- 消息来源不同：微信渠道来自 wxauto 监听，企微渠道来自会话存档 + 智能机器人长连接，互不干扰
- 共用同一 RAG 知识库：**知识库更新同时影响两个渠道的回答**；敏感内容接入微信群前先做准入审查（普通微信群成员不受企业边界管控）
- 任一渠道异常不影响另一个（独立进程）
