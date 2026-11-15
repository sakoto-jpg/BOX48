# BOX48 重构遗留问题跟踪

> 记录日期：2026-11-15  
> 审查基线：`refactor/a01-a05-fixes`（`bad1a80`）  
> 关联文档：`docs/ARCHITECTURE-REFACTORING-PLAN.md`

本文只记录 A01/A02/A03/A05 第一轮修复后**仍然存在或需要后续阶段处理**的问题。
已关闭项不再展开实现细节，保留结论和验证结果即可。

## 一、已关闭

| 编号 | 问题 | 结果 |
|---|---|---|
| A01 | 强制下载/洗版失败时可能清理旧媒体目录 | 已改为 `preserve_existing` + 开跑前旧媒体检测；合并使用 `.temp.mkv` 后原子替换 |
| A02 | 下载入口分散、同一 BVID 可能被并发处理 | 已引入 `DownloadCoordinator`：BVID 幂等认领 + 单 worker 串行 |
| A03 | 分团身份未贯穿下载历史与队伍匹配 | `group_code` 必传、Store 拒绝空值回退；`_match_team` 已按分团过滤 |
| A05 | 预备生识别跨分团误命中 | 日历关键词按分团缓存，投稿查询增加 `group_code` 条件 |

测试基线：`pytest` 共 13 passed、1 skipped（2026-11-15，容器内执行）。

## 二、待处理问题

### F01 测试未隔离 `GROUPS_PATH`

- 优先级：P2
- 现状：`tests/conftest.py` 设置了 `CONFIG_PATH`、`ROSTER_PATH`、`SCHEDULE_DB`，但没有设置
  `GROUPS_PATH`。`group_config.RUNTIME_PATH` 仍默认 `/config/groups.yaml`。
- 影响：在开发服务器上跑测试会读取生产 `groups.yaml`；CI 没有该文件时回退
  `backend/app/defaults/groups.yaml`。同一套测试可能因运行环境不同而行为不同。
- 建议：在 `conftest.py` 中把 `GROUPS_PATH` 指向临时副本，或复制 `defaults/groups.yaml`
  到测试临时目录。
- 验收：测试在有无 `/config/groups.yaml` 的机器上结果一致。

### F02 `/api/download_specific` 缺少分团参数

- 优先级：P2
- 现状：接口只接收 `url`，`DownloadCommand.group_code` 为空，只能按标题匹配队伍后反查分团。
- 影响：手动补档一个匹配不到常规队伍的「特殊公演」时，无法按入口分团回退到该分团的
  特殊公演队伍。前端当前未使用该接口，但 API 语义不完整。
- 建议：增加可选 `group` 字段，校验 `group_config.group_by_code()` 后传入
  `DownloadCommand`；未知分团仍回退为按队伍反查。
- 验收：调用 `download_specific` 时传 `group=gnz`，无常规队伍匹配也能落到 GNZ 特殊公演。

### F03 `DownloadCoordinator.describe()` 的保留窗口

- 优先级：P2
- 现状：只保留最近 100 个已结束任务，超过后 `describe(task_id)` 返回 `None`。
- 影响：前端目前使用 `queue_snapshot()` 与 `active_snapshot()`，暂不受影响；后续如果做
  任务历史页或异步查询任务结果，会出现「刚查过一段时间就查不到」。
- 建议：明确保留策略（数量/时间），或在阶段 2 用持久化任务表替代进程内字典。
- 验收：API 文档明确 `describe` 的生命周期；或任务状态可持久查询。

### F04 队列与任务状态仍然只存在内存

- 优先级：P2（阶段 2 处理）
- 现状：`DownloadCoordinator` 的 `_claimed`、`_tasks`、`_pending` 都是进程内结构。
- 影响：Backend 重启后，排队任务和正在执行状态丢失，无法恢复或明确标记中断。
- 说明：当前单进程 uvicorn 部署下属于已知限制，文档已注明；不要求本轮修复。
- 建议：阶段 2 引入 SQLite 任务表，至少记录 queued/running/终态和 BVID 幂等键。
- 验收：重启后能恢复排队任务，或把未完成任务明确标记为 interrupted。

### F05 API 仍直接访问 Scanner 内部状态

- 优先级：P2（阶段 2 处理）
- 现状：`main.py` 仍调用 `scanner.active_snapshot()`、`scanner.is_downloaded()` 等方法，
  并把 `scanner.active_tasks` 作为下载队列的「正在处理」数据源。
- 影响：API 与 Scanner 实现细节仍有耦合；后续拆分 Scanner 工作流时需要同步改 API。
- 建议：阶段 2 提取 `DownloadService` / `TaskQueryService`，API 只依赖服务接口。
- 验收：`main.py` 不再直接访问 Scanner 的属性或私有方法。

### F06 CI 依赖未锁定

- 优先级：P3
- 现状：`requirements-dev.txt` 使用 `pytest>=7.4`，`yt-dlp` 未固定版本。
- 影响：CI 和本地测试结果可能随依赖升级漂移；yt-dlp 行为变化会直接影响下载解析测试。
- 建议：固定版本，或引入 `pip-tools` / `uv lock` 生成锁定文件。
- 验收：CI 安装的依赖版本可复现。

### F07 测试临时目录不清理

- 优先级：P3
- 现状：`tests/conftest.py` 使用 `tempfile.mkdtemp(prefix="box48-tests-")`，测试结束后不删除。
- 影响：长期运行会产生 `/tmp/box48-tests-*` 残留。
- 建议：改用 pytest 的 `tmp_path_factory`，或在 session fixture 结束时清理。
- 验收：连续运行测试不再累积临时目录。

### F08 旧媒体检测依赖文件 mtime

- 优先级：P3
- 现状：`_is_pre_existing_media()` 通过 `mtime < run_start` 判断目录中的文件是否为本轮之前产生。
- 影响：NAS 时钟偏差、文件被外部工具重写 mtime 时可能误判「旧文件」，进而影响失败回滚策略。
- 建议：结合数据库历史记录中的 path/size 或内容校验辅助判断；至少在异常情况下记录日志。
- 验收：时钟偏差场景下回滚仍不会误删旧媒体。

## 三、后续阶段

以下事项不在第一轮 A01/A02/A03/A05 范围内，按
`docs/ARCHITECTURE-REFACTORING-PLAN.md` 的顺序推进：

1. 阶段 2：提取 `DownloadService` / Repository，API 去除直接 SQL 和 Scanner 内部访问。
2. 阶段 3：拆分 Scanner 的解析、规则、编码探测、分P下载、合并、媒体库与洗版职责。
3. 阶段 4：明确 Backend / Calendar 的表所有权与查询契约。
4. 阶段 5：前端迁移到 Vue + Vite 工程结构、统一错误处理和 API Client。

## 四、维护规则

- 新发现的问题使用 `Fxx` 编号追加到本文，记录优先级、影响、建议和验收标准。
- 问题关闭时在「已关闭」表追加一行，并标注修复提交 SHA。
- 本文不替代 `ARCHITECTURE-REFACTORING-PLAN.md` 的阶段路线；长期架构问题仍以该文档为准。
