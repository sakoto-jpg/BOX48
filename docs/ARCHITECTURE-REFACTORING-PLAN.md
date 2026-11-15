# BOX48 架构审计与重构方案

> 文档类型：架构审计 / 重构实施方案  
> 审查基线：仓库 `master` 分支（2026-11-15）  
> 状态：建议方案，尚未实施  
> 适用范围：Backend、Calendar、下载与媒体处理、SQLite 数据访问、任务调度

## 1. 执行摘要

BOX48 已经积累了较完整的业务能力，包括多分团投稿同步、录播自动扫描、手动补档、MC 分P筛选、编码探测与洗版、媒体合并、NFO、下载历史、Emby 入库、公演排期匹配和通知。

当前最主要的技术债不是单个文件过大，而是**业务协调职责、数据访问边界和服务间数据契约不够清晰**：

1. 下载任务入口分散在 API、APScheduler 和 Scanner 内部，互斥与任务状态没有统一协调点。
2. API 路由直接访问 Scanner 的私有方法、内部 Store、活动任务字典和下载队列。
3. 部分 API 和业务模块绕过 Store，直接连接 SQLite 并编写 SQL。
4. Backend 与 Calendar 共享 SQLite 文件，并且互相读取对方负责的数据表。
5. Scanner 将解析、业务规则、媒体处理、历史登记、通知和 Emby 刷新编排在一个流程中。
6. 部分任务参数没有完整传递到最终数据登记，例如分团身份；强制重新下载的语义也没有完整传递到失败回滚策略。

**总体建议：保留现有技术栈，按模块化单体方向渐进重构。先修复数据安全和任务协调问题，再建立稳定的服务与数据边界，最后拆分内部模块。**

不建议以本次重构为由推倒重写下载规则，也不建议当前阶段引入微服务、Redis、PostgreSQL 或 Kubernetes。

## 2. 审计范围与判断边界

本方案基于静态阅读仓库源码形成，重点检查以下文件和模块：

- `backend/app/main.py`：FastAPI 入口、批量下载队列、调度与配置接口。
- `backend/app/scheduler.py`：`Snh48Scanner`、投稿扫描、BVID 处理、媒体处理与自动洗版。
- `backend/app/uploads_api.py`：投稿列表、统计和公演排期匹配 API。
- `backend/app/history_store.py`、`upload_store.py`、`member_store.py`：SQLite Store。
- `calendar/app/main.py`、`calendar/app/schedule_store.py`：公演排期抓取和存储。
- `backend/app/downloader.py`、`merger.py`、`nfo_builder.py`：下载、合并和媒体元数据处理。
- 投稿同步及维护脚本。

本次没有在用户实际部署环境中执行下载、并发压力、磁盘故障注入或数据库迁移。因此：

- “代码中存在某调用/依赖”属于静态审查事实。
- “可能导致数据丢失、重复执行或误匹配”属于需要测试验证的风险。
- 本文不把尚未执行的运行时验证描述为已复现故障。

建议实施前重新确认目标分支和最新提交，避免基于过期代码修改。

## 3. 当前架构与主要调用链

### 3.1 手动批量下载

当前主流程可概括为：

```text
POST /api/download_batch
    |
    +-- main.py：解析 BV、去重、建立内存等待队列
    |
    +-- run_batch()：download_run_lock 串行处理批次
            |
            +-- scanner._is_downloaded()
            |
            +-- scanner.download_single()
                    |
                    +-- HistoryStore.get()
                    +-- _process_bvid()
                            |
                            +-- yt-dlp 解析视频信息
                            +-- _match_team() / 特殊队伍回退
                            +-- _process_video_info()
                                    |
                                    +-- MC 分P筛选
                                    +-- 编码探测
                                    +-- NfoBuilder 生成目录与元数据
                                    +-- BiliDownloader 下载分P
                                    +-- merger 合并与章节处理
                                    +-- HistoryStore 登记
                                    +-- Apprise 通知
                                    +-- Emby 刷新
```

主要问题：

- 队列和批次锁由 API 模块维护，业务调度职责进入 HTTP 层。
- `download_specific`、手动扫描、APScheduler 和自动洗版并未统一经过同一任务协调器。
- API 直接调用 `scanner._is_downloaded()` 等内部方法。
- Scanner 既负责业务流程，也承担外部系统和持久化的协调。

### 3.2 自动扫描和自动洗版

```text
FastAPI 生命周期 / APScheduler
    |
    +-- scanner.scan_all()
            |
            +-- 读取 subscriptions 配置
            +-- scan_and_download()
                    |
                    +-- yt-dlp 扫描 UP 主投稿
                    +-- 逐个解析 BVID
                    +-- _process_video_info()
            |
            +-- 创建 daemon thread 执行 _auto_rewash()
                    |
                    +-- HistoryStore.recent()
                    +-- 编码探测
                    +-- _process_bvid(keep_existing=True)
```

主要问题：

- 定时扫描、手动扫描、单条下载和批量下载缺少统一的任务认领与互斥机制。
- 自动洗版通过独立 daemon 线程启动，生命周期与任务状态没有统一管理。
- 解析重试的异常边界较宽，网络解析错误和后续业务/媒体处理错误可能进入相同重试逻辑。
- 内存任务状态在进程重启后不能自然恢复。

### 3.3 公演排期与录播匹配

```text
Calendar
  |
  +-- fetch_from_api()
  |     +-- 抓取官方票务排期
  |     +-- _lookup_def_label() 直接查询共享库 uploads
  |     +-- ScheduleStore 写入 performances
  |
  +-- 共享 SQLite 文件 snh48.db
          ^
          |
Backend
  |
  +-- GET /api/schedule_status
        +-- 直接查询 performances
        +-- 直接查询 uploads
        +-- 直接查询 downloads
        +-- 在 API 模块中完成日期、队伍、剧目匹配
```

主要问题：

- 两个服务共享同一个 SQLite 文件。
- Calendar 直接读取投稿表，Backend 直接读取排期表，形成双向数据结构依赖。
- 排期匹配规则与 SQL 查询、HTTP 响应组装混合在 API 函数中。
- Calendar 的预备生识别查询投稿标题时没有把当前 `group_code` 作为查询条件，存在跨分团误命中的风险。

## 4. 问题清单与优先级

优先级定义：

- **P0**：可能破坏已有媒体数据或导致业务数据错误，实施前应先验证和修复。
- **P1**：影响任务正确性、数据边界或多分团一致性，应在结构性拆分前完成。
- **P2**：明显增加维护成本、测试难度或异常恢复复杂度。
- **P3**：主要是性能、可观测性或后续演进问题。

| 编号 | 问题 | 影响 / 风险 | 优先级 |
|---|---|---|---|
| A01 | 强制下载参数没有传递为保留旧文件的失败回滚策略 | 同一目标目录已有有效文件时，新下载失败可能触发目录级清理 | P0 |
| A02 | 下载入口没有统一任务认领与 BVID 互斥 | 不同入口可能并发处理同一 BVID、文件路径或活动状态 | P0 |
| A03 | 分团身份未完整贯穿下载历史登记 | 手动批量下载传入的 `group_code` 用于特殊队伍回退，但当前 Scanner 的历史登记封装未接收该字段，Store 默认值为 `snh` | P1 |
| A04 | Backend / Calendar 共享数据库并跨域直接查询 | 表结构、迁移和部署形成隐式耦合 | P1 |
| A05 | Calendar 预备生识别查询未按分团过滤投稿 | 可能用其他分团同日投稿影响当前场次标签 | P1 |
| A06 | API 直接访问 Scanner 私有方法和内部状态 | API 与调度器实现细节绑定 | P1 |
| A07 | API 模块直接连接数据库并编写 SQL | Store 不再是稳定的数据访问边界 | P1 |
| A08 | Scanner 集中编排解析、下载、合并、历史、通知和 Emby | 核心工作流难以隔离测试和替换依赖 | P2 |
| A09 | 重试范围包含带副作用的后续处理 | 媒体处理或登记异常可能触发不必要的整段重试 | P2 |
| A10 | 自动洗版通过独立 daemon 线程启动 | 任务生命周期、互斥和异常状态不统一 | P2 |
| A11 | 排期匹配规则写在 API 路由中 | 业务规则难以独立测试与复用 | P2 |
| A12 | 队列和活动任务状态主要保存在内存 | 进程重启后无法完整恢复等待任务和运行状态 | P2 |
| A13 | 排期状态接口读取较大范围投稿和下载记录 | 数据增长后存在重复扫描与响应时间增长风险 | P3 |

### 4.1 A01：强制下载失败时的旧文件保护

批量接口把 `force` 用作“允许已入库视频重新处理”的开关，但调用 `scanner.download_single()` 时没有将它转换为 `keep_existing=True`。在 `_process_video_info()` 的失败分支中，普通下载会清理公演目录，而洗版模式才会仅清理本次运行产生的临时文件。

当旧文件和本次目标位于同一目录时，下载或合并失败可能影响旧文件。该风险需要通过临时目录和故障注入测试确认。

建议明确区分：

- `force`：是否跳过“已下载”判断。
- `replace_existing`：是否替换已存在的媒体文件。
- `preserve_existing_on_failure`：失败时是否必须保留旧文件。

替换必须采用“临时目录下载 → 完整性验证 → 提交替换 → 更新数据库 → 清理旧文件”的事务式工作流。失败时不得删除原有有效文件。

### 4.2 A02：统一任务协调与 BVID 互斥

目前批量下载有自己的批次锁，但单条下载、扫描、定时任务和自动洗版没有全部经过同一协调入口。仅给每个 API 分别加锁不能解决根因。

需要建立统一的 `DownloadCoordinator`：

- 所有入口只提交任务，不直接执行下载工作流。
- 以 BVID 为关键幂等键，防止同一视频被重复认领。
- 定义任务状态：queued、running、succeeded、failed、cancelled。
- 定义任务来源：manual、batch、scheduled_scan、rewash。
- 明确任务去重、优先级、取消和失败重试策略。
- 任务状态和锁的更新必须具备原子性。

第一阶段可继续使用进程内队列，但必须明确单进程限制；如未来需要多 Worker 或多实例，再引入持久化任务表或专用队列。

### 4.3 A03：分团身份完整传递

当前批量下载入口接收 `group`，传给 `download_single(..., group_code=...)`，并在队伍匹配失败时用于“特殊公演”回退。但 `_mark_downloaded()` 的参数没有 `group_code`，调用 HistoryStore 时依赖其默认值 `snh`。

这意味着“目录或匹配阶段使用了某分团信息”不等于“下载历史正确记录了该分团”。

建议引入统一的业务身份对象，或至少在各层明确传递：

- `group_code`
- `team_code`
- 规范队伍名称
- 特殊公演标识（如适用）

在 `DownloadWorkflow` 开始时解析并固定身份，后续 NFO、路径、历史记录、通知和统计都使用同一份身份信息。禁止在持久化层静默回退为 SNH48，除非业务明确判定为 SNH48。

### 4.4 A04：Backend / Calendar 数据所有权

短期不必拆库，但必须先定义表的所有者：

| 数据 | 建议所有者 | 其他模块访问方式 |
|---|---|---|
| `uploads`、`upload_queue`、`sync_state` | 投稿同步 / Upload 域 | Upload 查询接口或只读 Query Service |
| `downloads`、`orphan_files` | 下载历史 / History 域 | History Repository |
| `performances` | 公演排期 / Schedule 域 | Schedule Repository / 查询接口 |
| `member_terms` | 成员历史 / Member 域 | Member Repository |

每张表应有唯一的 Schema 创建和迁移责任方。跨域查询通过应用服务或明确的查询服务完成，不允许在任意 API 模块里直接连接数据库读取其他域表。

若 Backend 与 Calendar 继续独立部署，后续再评估内部 HTTP API 或事件同步；不要在没有边界契约的情况下同时依赖共享数据库和服务独立部署。

### 4.5 A05：预备生识别分团隔离

Calendar 的 `_lookup_def_label(api_date, bz)` 查询 `uploads` 时只按标题日期筛选，没有把当前场次的 `group_code` 纳入条件。调用端已经掌握分团代码，应将其传入，并在查询中限定分团。

增加回归测试：两个分团在同一天有相似剧目标题时，某分团的投稿不能改变另一个分团的预备生标签。

## 5. 目标架构

建议采用**模块化单体（Modular Monolith）**。保持 FastAPI、Vue、SQLite、APScheduler、FFmpeg 和 Docker Compose，不为追求分层而引入过多抽象。

### 5.1 依赖方向

```text
HTTP / API Layer
    |
    v
Application Services
    |
    v
Domain Rules / Workflows
    |
    v
Ports / Repository Interfaces
    |
    v
Infrastructure Implementations
    +-- SQLite
    +-- yt-dlp / Bilibili
    +-- FFmpeg / mkvmerge
    +-- Emby
    +-- Apprise
    +-- Filesystem
```

依赖规则：

1. API 层负责协议适配、参数校验和响应转换，不直接实现业务流程。
2. Application Service 负责用例编排、事务边界和任务提交。
3. Domain 负责可独立测试的业务规则，不依赖 FastAPI、SQLite 或具体外部客户端。
4. Repository / Gateway 负责数据与外部系统访问。
5. Infrastructure 实现接口，不反向依赖 API。
6. 跨业务域调用必须通过明确接口或查询服务，不直接读取对方内部对象和表结构。

### 5.2 建议目录结构

按渐进迁移，不要求一次性达到最终形态：

```text
backend/app/
├── api/
│   ├── downloads.py
│   ├── uploads.py
│   ├── history.py
│   ├── groups.py
│   └── pocket48.py
├── application/
│   ├── download_coordinator.py
│   ├── download_service.py
│   ├── upload_service.py
│   ├── schedule_status_service.py
│   ├── config_service.py
│   └── notification_service.py
├── domain/
│   ├── download_rules.py
│   ├── performance_matcher.py
│   ├── group_identity.py
│   └── task.py
├── repositories/
│   ├── upload_repository.py
│   ├── history_repository.py
│   ├── member_repository.py
│   └── schedule_repository.py
├── infrastructure/
│   ├── database.py
│   ├── bilibili/
│   ├── emby/
│   ├── notifications/
│   └── media/
├── workers/
│   ├── download_worker.py
│   └── scan_worker.py
└── app.py
```

Calendar 可以先保留独立进程，但将排期抓取、预备生识别和 ScheduleStore 封装在 Schedule 域内，并明确它与 Upload 域的接口。

## 6. 分阶段实施路线

### 阶段 0：冻结行为与建立安全网

目标：在改变调用结构之前，先保护现有业务行为和数据。

工作项：

- [ ] 记录当前 master 提交 SHA、部署配置和数据库 Schema。
- [ ] 备份 SQLite 数据库、下载目录和关键配置。
- [ ] 为现有核心流程建立 pytest 测试基线。
- [ ] 使用临时 SQLite 数据库进行 Store 测试。
- [ ] 使用 FastAPI TestClient 覆盖关键 API。
- [ ] 将外部网络、yt-dlp、FFmpeg、Emby 和 Apprise 包装为可替换依赖或测试替身。
- [ ] 建立 CI，至少执行格式/静态检查和测试。

验收标准：

- 现有核心规则有可重复的自动化测试。
- 测试不依赖真实 B 站账号、生产媒体目录或真实 Emby。
- 可以从备份恢复数据库和配置。

### 阶段 1：修复数据安全和任务协调（最高优先级）

目标：确保下载不会互相覆盖，并且失败不会破坏旧媒体。

工作项：

- [ ] 修复强制下载语义，区分跳过已下载与替换已有文件。
- [ ] 引入临时工作目录和成功后提交替换机制。
- [ ] 建立 DownloadCoordinator，统一所有下载入口。
- [ ] 实现 BVID 级任务认领和幂等保护。
- [ ] 将 APScheduler、手动扫描、单条下载、批量下载和自动洗版统一为任务提交入口。
- [ ] 将分团身份传递到最终历史登记。
- [ ] 将自动洗版纳入 Worker 管理，不再在业务流程中随意创建 daemon 线程。

验收标准：

- 同一 BVID 从多个入口同时提交时，只有一个执行实例。
- 下载或合并失败时，旧的有效媒体文件、NFO 和历史记录不被破坏。
- 不同分团下载后，目录、NFO、数据库历史和前端筛选保持一致。
- 队列状态、活动任务状态和最终状态不存在互相矛盾。

### 阶段 2：建立应用服务和数据访问边界

目标：消除 API 对 Scanner 内部实现的依赖，并使 Store 成为正式数据访问入口。

工作项：

- [ ] 从 `main.py` 提取 DownloadService / DownloadCoordinator。
- [ ] API 不再访问 Scanner 私有方法、内部 Store 和活动任务字典。
- [ ] 从 `uploads_api.py` 提取 UploadQueryService 和 ScheduleStatusService。
- [ ] 将直接 SQL 收敛到 Repository / Store。
- [ ] 将日期、队伍、剧目和分团匹配逻辑抽到可独立测试的 Domain 函数。
- [ ] 统一 SQLite 连接、WAL、busy_timeout 和 Schema 迁移入口。
- [ ] 明确各数据表的所有者、迁移负责人和跨域访问契约。

验收标准：

- API Router 不直接执行业务 SQL。
- API 不直接访问 Scanner 内部属性或私有方法。
- 业务匹配规则可以在不启动 FastAPI、不连接真实数据库的情况下测试。
- 每张表有唯一的 Schema/迁移责任方。

### 阶段 3：拆分 Scanner 工作流

目标：把复杂下载流程拆成可测试的步骤，但保持现有业务规则不变。

建议逐步提取：

- `VideoInfoResolver`：BVID 解析、信息获取和网络重试。
- `DownloadRuleService`：分团/队伍匹配、日期、范围和 MC 分P规则。
- `CodecSelectionService`：编码探测与选择。
- `PartDownloadService`：分P下载和失败处理。
- `MediaProcessingService`：合并、章节、完整性验证。
- `MediaLibraryService`：NFO、封面、历史提交、Emby 刷新。
- `RewashService`：自动洗版策略和冷却。

重试必须按阶段划分：网络解析、单个分P下载、媒体合并和最终登记分别定义可重试条件。不得在已有副作用后无条件重跑整个工作流。

验收标准：

- Scanner 主要负责协调或作为兼容门面，不再包含所有实现细节。
- 每个阶段的输入、输出、失败类型和副作用明确。
- 核心下载行为与阶段 0 的测试基线一致。

### 阶段 4：明确 Backend / Calendar 边界

目标：消除共享数据库造成的双向隐式依赖。

建议先做：

- [ ] 确定短期继续共享 SQLite，还是将 Calendar 并入 Backend 进程。
- [ ] 为 `performances`、`uploads` 等表指定唯一所有者。
- [ ] Calendar 通过明确的 Upload 查询接口获取投稿匹配数据，或使用定义好的只读投影。
- [ ] Backend 通过 Schedule 查询接口获取排期数据。
- [ ] 如果继续共享数据库，至少禁止非所有者模块创建/迁移/直接写入对方表。
- [ ] 为跨域查询定义稳定 DTO，避免把数据库 Row 直接暴露给调用方。

不建议在这一阶段为了“服务解耦”立即改为复杂的消息队列。先建立清晰契约，再根据部署和可靠性需求决定是否采用 HTTP 或事件同步。

验收标准：

- 每个数据表只有一个写入和迁移责任方。
- Backend 和 Calendar 不再依赖对方的内部 SQL/表字段。
- 单独测试任一域时，可以通过接口替身提供另一个域的数据。

### 阶段 5：前端工程化与部署治理

前端和部署问题不应阻塞下载数据安全的修复，可在核心后端边界稳定后推进：

- [ ] 将单文件前端逐步迁移为 Vue + Vite 工程结构。
- [ ] 按页面/业务域拆组件、API Client 和状态管理。
- [ ] 统一 API 错误处理和类型定义。
- [ ] 清理容器内硬编码地址，优先使用 Compose 服务名或配置项。
- [ ] 固定所有运行依赖版本，避免 Docker 构建时无约束升级。
- [ ] 明确宿主机 crontab、Emby 目录和下载目录的挂载边界。
- [ ] 检查管理接口的认证、授权和网络暴露策略。
- [ ] 统一日志字段，包含 task_id、bvid、group_code、阶段和错误类型。

## 7. 第一批必须建立的回归测试

| 测试 | 场景 | 预期结果 |
|---|---|---|
| T01 强制下载失败保护 | 已有有效 MKV，强制下载时模拟网络/下载/合并失败 | 原 MKV、NFO 和历史记录仍然有效 |
| T02 同 BVID 并发提交 | 批量、单条、自动扫描同时提交同一 BVID | 只认领一个执行任务 |
| T03 不同 BVID 并发 | 两个不同 BVID 同时提交 | 按配置的并发策略执行，不互相覆盖路径和状态 |
| T04 分团历史 | 使用非 SNH 分团执行手动补档 | downloads.group_code 与实际分团一致 |
| T05 特殊公演回退 | 标题无法匹配常规队伍，但请求指定 group_code | 使用该分团的特殊队伍，并正确登记身份 |
| T06 跨分团预备生识别 | 两个分团同日出现相似剧目投稿 | 一个分团的投稿不影响另一个分团标签 |
| T07 排期匹配 | 相同日期、不同分团/队伍/剧目 | 匹配结果只来自正确分团 |
| T08 重试边界 | 分别模拟解析、分P下载、合并和数据库异常 | 只重试允许重试的阶段，不重复提交已完成副作用 |
| T09 任务重启 | 任务排队/执行中时重启 Backend | 按选定的持久化策略恢复或明确标记中断任务 |
| T10 数据库迁移 | 从当前生产 Schema 的备份副本升级 | 迁移可重复执行、失败可回滚或恢复 |

## 8. 建议的模块接口草图

以下是目标接口示意，不代表要求立即按此代码实现。

### 8.1 下载任务协调

```python
class DownloadCoordinator:
    def submit(self, command: DownloadCommand) -> Task:
        """提交任务并执行幂等认领。"""

    def get_task(self, task_id: str) -> Task | None:
        """查询任务状态。"""

    def cancel(self, task_id: str) -> bool:
        """取消尚未进入不可中断阶段的任务。"""
```

API 只负责将请求转换为 Command：

```python
@router.post("/download/batch")
def submit_batch(payload: BatchRequest):
    return download_coordinator.submit_batch(payload)
```

### 8.2 下载工作流

```python
class DownloadService:
    def execute(self, command: DownloadCommand) -> DownloadResult:
        info = video_info_resolver.resolve(command.bvid)
        identity = group_identity_resolver.resolve(info, command.group_code)
        plan = download_planner.create(info, identity, command.scope)
        artifact = media_workflow.build(plan)
        return download_committer.commit(
            artifact,
            preserve_existing=command.preserve_existing,
        )
```

### 8.3 排期状态查询

```python
class ScheduleStatusService:
    def get_status(self, date: str, group_code: str) -> ScheduleStatus:
        performances = schedule_repository.list_by_date(date, group_code)
        uploads = upload_repository.find_candidates(date, group_code)
        downloads = history_repository.find_by_bvids(
            [item.bvid for item in uploads]
        )
        return schedule_matcher.build_status(
            performances, uploads, downloads
        )
```

## 9. 不建议在本轮做的事情

为控制风险和避免过度设计，以下事项不作为当前重构目标：

- 不整体重写 Bilibili 下载、MC 分P筛选、编码探测和媒体合并规则。
- 不因为文件较大就机械拆分为大量小文件。
- 不为了形式上的服务独立而立即把 SQLite 替换为 PostgreSQL。
- 不引入 Redis、Kafka、Kubernetes 等当前没有明确必要性的基础设施。
- 不同时进行数据库迁移、目录重排、前端重写和下载算法改写。
- 不在没有回归测试的情况下改变目录命名、NFO 格式、Emby 入库行为或历史记录语义。

## 10. 完成标准

当以下条件满足时，可以认为第一轮架构重构达到目标：

1. 所有下载入口经过统一协调器，具备 BVID 幂等认领和明确的任务状态。
2. 强制替换采用安全提交流程，失败不会损坏已有有效媒体。
3. 分团身份从入口贯穿到路径、NFO、历史记录和查询结果。
4. API 层不再访问 Scanner 私有方法、内部 Store 或内部任务字典。
5. 业务 API 不直接执行跨域 SQL；数据访问经 Repository / Query Service。
6. Backend 与 Calendar 的表所有权、迁移责任和跨域契约明确。
7. 核心下载、洗版、排期匹配流程具备自动化回归测试。
8. 部署和数据库变更有备份、恢复和迁移验证记录。

## 11. 实施顺序建议

实际执行顺序建议如下：

1. **先验证并修复 A01、A02、A03、A05**：保护媒体文件、避免重复任务、修正分团数据和预备生识别。
2. 建立测试基线和 CI，保证后续重构可以持续验证。
3. 提取 DownloadCoordinator 和 DownloadService。
4. 将 API 的直接 SQL 收敛到 Repository / Query Service。
5. 拆分 Scanner 工作流，明确重试和提交边界。
6. 明确 Backend / Calendar 数据所有权和接口契约。
7. 最后推进前端工程化、部署治理和更广泛的代码整理。

**最终原则：先稳定行为，再改变依赖；先保护数据，再拆分结构；每一步重构都应可测试、可回滚、可独立验收。**
