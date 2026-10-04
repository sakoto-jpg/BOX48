# 设置页改造：现状梳理与方案

> 阶段 5 的数据接入已完成（五个分团的排期、花名册、剧目名单、投稿目录）。
> 本文梳理「有哪些工具/脚本/配置」，标出哪些前端改不了，并给出设置页改造方案。

## 一、工具脚本盘点（12 个）

运行环境一栏：容器 = `snh48-downloader-backend`（`./backend/app` 是挂载目录，
改代码重启即生效）；宿主机 = 必须在开发服务器上跑（依赖宿主路径或宿主服务）。

| # | 工具 | 作用 | 运行环境 | 现在的调用方式 | 前端可调用 |
|---|---|---|---|---|---|
| 1 | `sync_uploads.py` | 官方投稿目录同步（慢速可续爬） | 容器 | cron：每 6h 全量 + 每日 23:30 增量 | ❌ |
| 2 | `sync_wiki_all.py` | wiki 一键同步（花名册→任职区间→剧目名单） | 容器 | cron：每周一 04:30 | ❌ |
| 3 | `sync_roster_from_wiki.py` | 花名册同步（含 `--create-missing` 生成新队伍） | 容器 | 由 #2 串联 | ❌ |
| 4 | `sync_member_terms_from_wiki.py` | 成员任职区间同步 | 容器 | 由 #2 串联 | ❌ |
| 5 | `sync_performances_from_wiki.py` | 剧目名单生成 | 容器 | 由 #2 串联 | ❌ |
| 6 | `rewrite_episode_metadata.py` | 重建已有剧集的演职人员 | 容器 | 手工执行 | ❌ |
| 7 | `migrate_media_dirs.py` | 媒体目录规范化改名/合并 | 容器 | 手工执行（已完成） | ❌ |
| 8 | `emby_artwork.py` | Emby 素材：剧集封面 / 队伍海报 / 成员头像 | **宿主机** | 手工执行 | ❌ |
| 9 | `repair_chapters.py` | 修复 mkv 章节标题（不重编码） | 容器（自带 ffmpeg/mkvpropedit） | 手工执行 | ❌ |
| 10 | `merge_team_dirs.py` | 合并两个队伍目录 | 宿主机 | 手工执行 | ❌ |
| 11 | `migrate_history.py` | 历史 txt → SQLite（一次性，已完成） | 容器 | 已执行 | ❌ |
| 12 | `wiki_client.py` | wiki 访问与解析公共库 | — | 被 #3/#4/#5 引用 | — 非可执行 |

**结论**：除了两个 cron 任务，其余全部只能在 SSH 里手工执行。

## 二、HTTP 接口盘点

后端已暴露 15 个接口，前端实际用到 11 个：

| 接口 | 前端是否使用 |
|---|---|
| `GET/POST /api/config` | ✅ 设置页 / 分团页保存 |
| `GET /api/groups` | ✅ 标签栏与分团筛选 |
| `GET /api/uploads`、`/uploads/categories`、`/uploads/stats` | ✅ 投稿批量下载卡片 |
| `GET /api/schedule_status` | ✅ 日历卡片状态 |
| `POST /api/download_batch` | ✅ 批量下载 / 洗版 |
| `GET /api/download_queue` | ✅ 下载队列 |
| `GET /api/emby_status` | ✅ 顶部状态点 |
| `POST /api/trigger_scan` | ✅ 任务引擎按钮 |
| `GET /api/history` | ❌ 未使用（已入库状态改由 `/api/uploads` 返回） |
| `POST /api/download_specific` | ❌ 未使用（任务引擎走批量接口） |
| `POST /api/reload_metadata` | ❌ 仅内部调用（wiki 同步后热加载） |

**工具类接口为 0** —— 这是「前端无法调用工具」的直接原因。

## 三、设置项可编辑性

| 配置（roster.yaml / groups.yaml） | 前端可改 | 位置 |
|---|---|---|
| 自动扫描：开关 / 模式 / 时间 / 间隔 | ✅ | 设置页 → 自动扫描调度 |
| 自动入库下载范围 | ✅ | 同上 |
| 监听账号 `subscriptions` | ✅ | 设置页 → 监听账号 |
| 手动下载范围 | ✅ | 投稿批量下载卡片 |
| 队伍花名册：开关 / 提醒 / 关键词 / 成员 / 关注 | ✅ | 分团标签页 → 队伍编排与花名册 |
| 手动订阅单个场次 `manual_subs` | ✅ | 日历卡片的订阅按钮 |
| **推送地址 `apprise_url`** | ❌ | 只能在配置文件里改 |
| **Emby `server_url` / `api_key` / `library_name`** | ❌ | 同上 |
| **分团官方账号 `bilibili_mid`** | ❌ | 在 `groups.yaml` |
| **分团启用状态 `enabled`** | ❌ | 同上 |
| **分团投稿抓取开关 `sync_uploads`** | ❌ | 同上 |
| **分团主题色 `theme_color`** | ❌ | 同上 |
| **分团训练生规则 `trainee`** | ❌ | 同上 |
| 下载编码策略（AV1 → H.265 → H.264） | 设计上不可配置 | — |

## 四、改造方案

### 5a 设置页补齐表单（先做，风险最低）

**状态：✅ 已完成（2026-10-04）**

已落地内容与实测：

- 通知推送：`apprise_url` 输入框 + 「发送测试推送」→ 实测返回「已发送（HTTP 200）」
- Emby 媒体库：`server_url` / `library_name` / `api_key`（密码框 + 显示切换）
  + 「测试连接」→ 实测返回「已连接，媒体库 ID 32475」
- 分团官方账号与开关：五个分团各一行，可改 B站 mid、接入、投稿抓取、主题色；
  「保存分团配置」原子写 `groups.yaml`（保留文件头注释 + 自动备份，保留最近 10 份），
  改「接入」时提示需重启日历服务
- 新增接口：`POST /api/notify_test`、`POST /api/emby_test`、
  `GET/POST /api/groups/config`

设置页新增三块：

1. **通知**：`apprise_url` 输入框 + 「发送测试推送」按钮
   （新增 `POST /api/notify_test`，直接用现有 `send_apprise_msg`）
2. **Emby**：`server_url` / `api_key` / `library_name` + 「测试连接」
   （复用 `EmbyClient._get_library_id()` 判断媒体库是否可达）
   - API Key 用密码框显示，留空表示不修改（避免误清空）
3. **分团账号**：每行一个分团，可改 `bilibili_mid`、`enabled`、`sync_uploads`、
   `theme_color`
   - 需要新增 `GET/POST /api/groups/config`（读写 `groups.yaml`，写前备份）
   - 保存后自动调用 `POST /api/reload_metadata`，并提示「日历服务需重启才生效」

### 5b 维护工具面板

新增 `POST /api/maintenance/{action}`，action 白名单：

| action | 对应工具 | 是否破坏性 |
|---|---|---|
| `sync_uploads` | `sync_uploads.py --group X --mode full/incremental` | 否（只读网络 + 写目录表） |
| `sync_wiki` | `sync_wiki_all.py --apply` | 覆盖 roster/任职区间/剧目名单，有护栏 |
| `rewrite_metadata` | `rewrite_episode_metadata.py --apply` | 重写 NFO（自动备份） |
| `repair_chapters` | `repair_chapters.py --apply` | 改 mkv 章节（可回滚） |
| `emby_artwork` | `emby_artwork.py --apply` | 需要宿主机，暂用「生成命令提示」 |

设计要点：

- 一律后台执行（`subprocess.Popen`），接口立即返回任务 id
- 新增 `GET /api/maintenance/log/{task_id}` 轮询日志尾部
- 破坏性操作前端默认 dry-run 预览，二次确认后才允许 `--apply`
- 同时只允许一个维护任务运行（复用现有 `sync_uploads.lock` 思路）

### 5c 定时任务可视化

设置页显示四条 cron 的：状态、下次执行时间、最近一次日志尾部；
「启用/停用」通过重写 crontab 实现（写前备份 `crontab -l`）。

## 五、风险与约束

1. **破坏性工具**（目录迁移、重建元数据、合并目录）必须在 UI 上强制
   dry-run 预览 + 二次确认，且默认不可跳过。
2. **长任务**（投稿全量 ≈ 数小时）必须后台跑 + 轮询日志，不能阻塞 HTTP 请求。
3. **`emby_artwork.py` 依赖宿主机路径**（`/root/emby-config/metadata` 没挂进容器）。
   两条路：给后端容器加挂载（改 compose），或者在设置页只提供「复制命令」。
   **✅ 2026-10-04 已加挂载**：compose 增加 `- /root/emby-config:/emby-config`，
   并注入 `EMBY_METADATA_PATH` / `EMBY_PATH_ROOT`；素材工具改为从环境变量取路径，
   同一份代码在宿主机与容器里都能直接跑（容器里实测：剧集封面 84/84 匹配、
   队伍素材、人物头像目录均可读写）。
4. **改 `groups.yaml` 要原子写**（临时文件 + rename），否则正在跑的同步任务
   会读到半截内容（GNZ 首轮抓取就是这么中断过一次）。
5. **crontab 改动**要备份并在 UI 上明确显示改了什么。
