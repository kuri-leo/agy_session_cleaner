# Antigravity Tools

跨设备适用的 Antigravity 本地数据管理与清理工具包。

## 工具列表

### `clean_sessions.py`
清理 Antigravity 主版本、Antigravity IDE 与 Antigravity CLI 中已被删除但残留在磁盘上的孤立 Session 数据，并清理数据库中的孤立记录。

#### 功能特性
1. **三端检测**：`~/.gemini/antigravity`、`~/.gemini/antigravity-ide`、`~/.gemini/antigravity-cli`。
2. **完整依赖链保护**：以数据库中的顶层会话（`parent_conversation_id` 为空或 NULL）和 `--protect-id` 为根，递归保护全部 subagent 会话。
3. **数据库缺失/为空时中止**：该环境下没有 `conversation_summaries.db` 或其中 0 条会话时，不删除任何东西（可用 `--allow-empty-db` 覆盖）；若该环境本来就没有数据，则显示 `[SKIPPED]`。
4. **只认 UUID 命名**：`brain/`、`conversations/` 等目录中只有 UUID 命名的条目会被考虑，`tempmediaStorage` 等其他文件夹一律不动。
5. **清理范围**：孤立的 `brain/` 目录、`conversations/*.db`（含 `-wal`/`-shm`）、无主的 `-wal`/`-shm`、IDE 的 `implicit/*.pb`、`annotations/*.pbtxt`、`html_artifacts/`、`presence/*.lock`，以及数据库 `conversation_summaries` 与 `conversations_fts` 中的孤立行，最后执行 `VACUUM`（失败只告警）。
6. **CLI 的 `implicit/*.pb` 默认保留**：其文件名不是会话 ID，需显式加 `--include-cli-implicit` 才删除。
7. **永久删除**：不经过回收站，无宽限期；只读文件会先去掉只读属性再删除；被占用的文件会跳过并告警，对应的数据库记录也会保留，下次运行再重试。
8. **删除前复核**：确认后、真正删除前会重新读取数据库，扫描之后变为活动状态的会话会被保留；此时数据库读不了则该环境整体跳过。
9. **ID 匹配不区分大小写**：`--protect-id` 会去掉首尾空白并忽略大小写；磁盘上的文件名与数据库 ID 也按忽略大小写比较。

#### 使用说明
```bash
# 1. 默认预览模式（不删除任何数据，报告列出每一类待删条目）
uv run clean_sessions.py

# 2. 正式执行（会二次确认；永久删除，不可恢复）
uv run clean_sessions.py --execute
uv run clean_sessions.py --execute --yes          # 跳过确认提示

# 3. 仅针对特定环境
uv run clean_sessions.py --target ide --execute   # Antigravity IDE（别名 antigravity-ide）
uv run clean_sessions.py --target cli --execute   # Antigravity CLI（别名 antigravity-cli）
uv run clean_sessions.py --target antigravity --execute

# 4. 额外保护特定 Session（及其 subagent），可重复
uv run clean_sessions.py --protect-id <conversation_id>

# 5. 指定数据目录：单个环境目录，或包含多个环境的父目录（如 ~/.gemini）
uv run clean_sessions.py --app-data D:\backup\.gemini
```

| 参数 | 作用 |
|:---|:---|
| `--execute` | 真正执行删除（默认只预览） |
| `--yes` / `-y` | 跳过 `[y/N]` 确认 |
| `--force` | Antigravity / IDE / language server / `agy.exe` 正在运行（或无法检测进程）时仍然执行；不加则拒绝执行 |
| `--allow-empty-db` | 数据库缺失或为空时仍继续（此时除 `--protect-id` 外全部视为孤立，慎用） |
| `--include-cli-implicit` | 同时删除 CLI 的 `implicit/*.pb` |

退出码：`0` 成功；`1` 有环境报错、拒绝执行、用户取消或有删除失败；`2` 参数错误。

#### 测试
```bash
python -m unittest discover -s tests -v
```
测试只在临时目录中构造合成数据，不会触碰 `~/.gemini`。

### `path_utils.py`
路径探测通用模块，支持读取环境变量 `ANTIGRAVITY_APP_DATA`、各操作系统标准用户主目录，并自动发现所有可用的 Antigravity 及 IDE 实例。
