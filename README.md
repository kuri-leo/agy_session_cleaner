# Antigravity Tools

跨设备适用的 Antigravity 本地数据管理与清理工具包。

## 工具列表

### `clean_sessions.py`
清理 Antigravity 及 Antigravity IDE 中已被删除但残留在磁盘上的孤立 Session 目录与会话数据库文件，并优化释放数据库空间。

#### 功能特性
1. **自动跨端检测双环境**：自动探测并统一管理 Antigravity 主版本（`~/.gemini/antigravity`）与 Antigravity IDE（`~/.gemini/antigravity-ide`）。
2. **完整依赖链保护**：递归保护 UI 中所有活动会话及其所有的 subagent 派生会话，避免误删。
3. **内容预览与安全 Dry-Run**：智能提取首条提问内容展示，默认安全分析统计，只有带 `--execute` 时才真正执行物理删除。
4. **数据库与上下文瘦身**：同步清理孤儿 `brain/` 目录、`conversations/` 数据库文件、`implicit/` 上下文快照，并执行 SQLite `VACUUM` 优化。

#### 使用说明
```bash
# 1. 默认预览模式（联合分析 Antigravity + Antigravity IDE，不删数据）
uv run clean_sessions.py

# 2. 正式执行一键清理（清理所有环境）
uv run clean_sessions.py --execute

# 3. 仅针对特定环境进行清理
uv run clean_sessions.py --target ide --execute       # 仅清理 Antigravity IDE
uv run clean_sessions.py --target antigravity --execute # 仅清理 Antigravity 主版本

# 4. 指定需要额外保护的特定 Session ID
uv run clean_sessions.py --protect-id <conversation_id>
```

### `path_utils.py`
路径探测通用模块，支持读取环境变量 `ANTIGRAVITY_APP_DATA`、各操作系统标准用户主目录，并自动发现所有可用的 Antigravity 及 IDE 实例。
