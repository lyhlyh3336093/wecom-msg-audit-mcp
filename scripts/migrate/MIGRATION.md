# 迁移操作单 — 当前机器 → 新 Windows 主机

> **⚠ 迁移包含全部凭据与已解密聊天记录。归档遗失即重大泄露。**
> 解压密码经独立渠道（口头/密码管理器）传递，绝不与归档同介质存放。

## 一、当前机器：导出

1. 确认 Docker 引擎正常（`docker info`；若僵死按下文「Docker 引擎僵死恢复」处理）
2. 执行导出：
   ```powershell
   powershell -ExecutionPolicy Bypass -File scripts\migrate\export.ps1 -OutputDir D:\migration_pack
   ```
   - 脚本会停本项目与 aihub 容器 → 导出镜像/数据库/数据卷 → 重启容器 → 生成加密归档
   - **导出期间机器人短暂停机（约 5-15 分钟，取决于磁盘速度）**
3. 记录解压密码 → 存入密码管理器
4. 用移动硬盘/局域网把 `migration_pack.7z` 拷到新机器（明文 work 目录不要拷）

## 二、新机器：初始化

1. 安装 Docker Desktop（WSL2 后端），启动并确认 `docker info` 正常
2. 安装 7-Zip（`winget install 7zip.7zip`）
3. 安装 Python 3.12 + uv（供本项目 `uv run` 使用）
4. 拷贝本仓库到新机器（`git clone` 或拷贝目录）
5. PC 微信：登录**专用小号**，关闭微信自动更新；确认群聊窗口可打开
6. wxautox4：按官方流程激活（一机一码；换绑政策购买时已确认）
7. Windows 自动登录 + 禁止锁屏：`netplwiz` 取消勾选「要求用户输入用户名和密码」；设置 → 电源 → 屏幕永不关闭睡眠（显示器可熄屏）；组策略或注册表禁用锁屏（`HKLM\SOFTWARE\Policies\Microsoft\Windows\Personalization` → `NoLockScreen=1`）
8. **真机探针（先于一切验收）**：人工确认 wxauto 提取的消息文本包含字面 `@智能小助手`，机器人在群内显示昵称精确为「智能小助手」

## 三、新机器：导入恢复

```powershell
powershell -ExecutionPolicy Bypass -File scripts\migrate\import.ps1 -Pack D:\migration_pack.7z -ProjectDir D:\wecom-restored
```

- 脚本自动完成：解压 → manifest 校验 → docker load → Postgres/MinIO/BGE 卷恢复 → KTD8 容器内 RAG 地址转换 → 启动 → health 与容器内 RAG 连通检查
- 演练（不打扰现有部署）: 加 `-Rehearsal`（override 剥离容器名、端口偏移 1xxxx）
- 目标目录已存在时默认中止，确认覆盖加 `-Force`

## 四、人工验证单

- [ ] 本项目 `/health` 200（脚本已检查）
- [ ] 容器内 RAG 连通（脚本已检查）
- [ ] 拉取消息：`POST http://127.0.0.1:8331/api/pull` 返回 seq 推进
- [ ] 企微群问答：群内 `……？@智能小助手` 收到回复
- [ ] 仪表盘：`http://127.0.0.1:8331/` 可查历史消息
- [ ] wxbridge 启动并完成一次微信群 @ 测试（按 docs/deploy-wechat-bot.md）
- [ ] 演练模式验证后清理演练栈

## 五、退役清机（勿跳过）

验证全部通过后，在**两端机器**上执行：

- [ ] 删除迁移包（`migration_pack.7z`）与解压目录，清空回收站
- [ ] 旧机器删除 `.env`、`secrets/`、`data/` 目录，清空回收站
- [ ] 退出 PC 微信登录、注销 wxautox4 授权
- [ ] 若旧机器将离开你的完全控制（转手/报废/送人）：重装系统
- [ ] 企微管理后台轮换 `MSGAUDIT_SECRET` / `EXTERNAL_CONTACT_SECRET`，并更新新机器 `.env` 后重建容器

## 六、常见故障

| 故障 | 处理 |
|---|---|
| Docker 引擎僵死（API 500 / 管道消失） | 见下节「Docker 引擎僵死恢复」；或安装看门狗（scripts/wddog）自动恢复 |
| `pg_restore` 失败 | 现场已保留（卷 aihub-knowledge_pg_data），修正后重跑 import.ps1（幂等：`-Force`） |
| manifest 校验不过 | 传输损坏或被篡改——重新拷贝归档，勿强行导入 |
| 8001/8331 端口冲突 | 检查旧实例是否残留：`docker ps`；演练用 `-Rehearsal` |
| RAG 连通检查失败 | 查 aihub 容器日志 `docker logs aihub-knowledge-app`；确认 KTD8 env 转换生效 |

## 七、Docker 引擎僵死恢复（本机已验证序列）

```powershell
wsl --shutdown
Stop-Process -Name "Docker Desktop","com.docker.backend","com.docker.build","com.docker.service" -Force -ErrorAction SilentlyContinue
Start-Process "C:\Program Files\Docker\Docker\Docker Desktop.exe"
# 轮询 docker info 直到就绪（约 1-3 分钟）
```

建议安装看门狗自动执行：`scripts\wddog\register-task.ps1`（每 5 分钟探活，恢复期 15 分钟宽限防死循环）。
