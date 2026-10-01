# SNH48 工具链改造路线图（多分团支持）

> 阶段 0 基线文档：记录现状、需求、目标架构与实施顺序。后续改动都在
> `refactor/multi-group` 分支上进行，按阶段推进、每阶段可独立上线。

## 零、当前进度（2026-10-01）

| 阶段 | 状态 | 说明 |
|---|---|---|
| 0 基线文档 | ✅ 完成 | 本文件 |
| 1 配置化地基 | ✅ 完成 | `groups.yaml`、三表 `group_code` 回填、日历按分团抓取 |
| 2 花名册自动同步 | ✅ 完成 | `sync_roster_from_wiki.py`；每周一 04:30（北京时间）自动同步 |
| 3 剧目名单 + 历史阵容 | ✅ 完成 | `member_terms` 表、`sync_performances_from_wiki.py`、NFO 三层匹配 |
| 4 展示层 | 🚧 进行中 | 设计见 `docs/FRONTEND-TABS.md`；已完成历史剧集元数据重建 |
| 5 全分团接入 | ⏳ 待开始 | 目前仅 `snh` 分团 `enabled: true` |

已落地的运维入口：

- `python tools/sync_wiki_all.py --apply`：花名册 → 任职区间 → 剧目名单一次跑完，
  写入前自动备份 `roster.yaml` / `performances.wiki.yaml`（保留最近 10 份），
  完成后调用 `POST /api/reload_metadata` 热加载，无需重启容器。
- 每周一 04:30（北京时间）由 cron 自动执行上述同步。
- `python tools/rewrite_episode_metadata.py --apply --refresh`：按当前匹配规则
  重建媒体库已有剧集的演职人员，原 NFO 备份到 `config/backups/nfo/<时间戳>/`。

数据现状：`member_terms` 431 条区间 / 302 名成员；自动剧目名单 56 个剧目
（覆盖 SNH48 四队 + 训练生公演）。`performances.yaml` 的 12 条人工配置仍然
优先于自动生成层。

## 一、现状

三个服务共用一套 SQLite（`config/snh48.db`）：

| 服务 | 端口 | 职责 |
|---|---|---|
| backend | 8095 | 投稿目录同步、下载/合并/洗版、历史与入库、排期匹配、提醒 |
| frontend | 8090 | 任务引擎、投稿批量下载、下载队列、日历查缺补漏、花名册配置 |
| calendar | 8099 | 官方票务接口抓取、`/api/tickets`、`/api/calendar.ics` |

数据表：`uploads`（官方投稿）、`upload_queue`、`sync_state`、`downloads`（入库
历史）、`performances`（公演排期）、`orphan_files`、`schema_meta`。

已具备能力：

- 官方账号投稿全量 + 每日增量同步（慢速、可断点续爬）
- 整场 / 仅 MC 两种入库范围，AV1 → H.265 → H.264 三级编码
- 批量下载、批量洗版（范围翻转）、同范围修复编码
- 排期 ↔ 投稿 ↔ 入库三方匹配，日历卡片直接补档
- Emby 素材：剧集封面、队伍海报/横幅/logo、成员头像、NFO 演员表
- 运维工具：历史迁移、章节修复、素材安装、队伍目录合并

## 二、需求

1. **分团扩展**：官方票务接口用 `gid` 区分分团（1=snh、2=bej、3=gnz、
   5=ckg、6=cgt），需要接入并贯穿数据模型。
2. **花名册自动化**：从 Fandom wiki 同步成员名单，减少手工维护。
3. **历史阵容准确**：老公演在 Emby 中显示当年的成员，而不是当前队伍名单。
4. **剧目名单补全**：`performances.yaml` 目前仅 12 条，需要自动生成并可人工修正。
5. **展示层一致性**：前端筛选、Emby 命名、素材映射全部支持分团。
6. **运维可持续**：同步工具默认 dry-run + 差异报告，人工只维护个性化配置。

## 三、目标架构

### 3.1 配置驱动

新增 `groups.yaml`（或扩展 `roster.yaml`），每个分团声明：

```yaml
groups:
  snh: { name: SNH48, api_gid: 1, bilibili_mid: 2832224 }
  gnz: { name: GNZ48, api_gid: 3, bilibili_mid: ... }
  bej: { name: BEJ48, api_gid: 2, bilibili_mid: ... }
  ckg: { name: CKG48, api_gid: 5, bilibili_mid: ... }
  cgt: { name: CGT48, api_gid: 6, bilibili_mid: ... }
teams:
  - { group: snh, code: n, name: TEAM NII, keywords: [...], fandom: "Team NII" }
  ...
```

### 3.2 数据模型

- `performances` / `uploads` / `downloads` 增加 `group_code`。
- 队伍规范名升级为分团内唯一，逻辑键统一为 `(group, team)`。
- 新增 `member_terms`：成员在分团/队伍的任职区间（来自 wiki）。
- 剧目名单（`performances.yaml` 或新表）：剧目 → 成员名单，标记来源（人工/自动）。

实际实现：

- `member_terms(member, group_code, team_code, start_year, end_year, concurrent,
  wiki_page, source, updated_at)`，按年记录区间，整表重建，见
  `backend/app/member_store.py`。
- 剧目名单拆成两层文件：`performances.yaml`（人工，优先）与
  `performances.wiki.yaml`（自动生成，勿手改），运行期合并，键为
  `(剧目名, 队伍)`。

### 3.3 演员表三层匹配

1. 剧目名单（`performances.yaml`，人工覆盖优先）；
2. 按公演日期 + 队伍从 `member_terms` 计算当时阵容；
3. 当前 `roster.yaml` 队伍名单（最终兜底）。

实际实现（`backend/app/nfo_builder.py::_members_for`）：

0. `cast_mode=roster` 的队伍（拼盘/特别公演）直接用 `roster.yaml` 配置；
1. 剧目名单本队命中（同名剧目靠队伍区分）；
2. **按公演日期反推当期阵容**（见下）；
3. `member_terms` 按公演年份回溯（精度较粗，兜底）；
4. 剧目名单跨队兜底（用于队名判错或特殊公演）；
5. 当前花名册（含未开启自动入库的队伍）。

### 3.3.1 按日期反推当期阵容

wiki 的任职区间只精确到「年」，年中转会无法区分，因此生日公演这类
「全队上场但没有独立名单」的场次会多列已离队成员。解决办法是用剧目日期当
锚点：每套剧目的首演/收官阵容都有准确的日期，

    NII 8th 首演 2023-01-07 的阵容 → 那天谁在 NII
    NII 9th 首演 2024-xx-xx 的阵容 → 那天谁在 NII
    ...

于是「某天的队伍阵容」= 当天在演的那套剧目的阵容：

- 该剧已收官 → 首演阵容 ∪ 收官阵容；
- 该剧仍在上演（该队最新一套）→ 直接用当前花名册：首演阵容里可能有人只在
  MV/宣传物中出现，或已经退团，并进来反而不准；
- 早于该队第一套已收录剧目 → 返回空，交给下一步兜底。

实测效果：青钰雯 2026-09-12 生日公演由按年的 29 人收敛到 16 人（当前
花名册），既去掉了 2026 年内已离队的成员，也不会把只在 MV 出场的卢天惠
算成登台演员。

`tools/rewrite_episode_metadata.py` 只在**生日公演**上直接套用这一层
（其他专场/拼盘名单无从推断，仍需 `--include-fallbacks` 手工确认）。

### 3.4 Emby 组织

- 保持单媒体库，系列名前加分团前缀（如 `GNZ48 TEAM G`），避免不同分团
  的 `TEAM G` 撞名。
- 素材映射与成员照片消歧按分团配置。

## 四、实施阶段

| 阶段 | 内容 | 交付物 |
|---|---|---|
| 0 | 冻结基线（本文档） | 本文件 |
| 1 | 配置化地基：`groups.yaml`；数据表加 `group_code`；键改 `(group, team)`；日历按配置循环抓 gid | 迁移脚本 + 校验报告 |
| 2 | 花名册自动同步：wiki 分类 + Infobox → 更新 roster 的 `members`（保留人工字段） | `sync_roster_from_wiki.py`（dry-run + diff） |
| 3 | 剧目名单与历史阵容：wiki Stage 页面 → 生成 `performances.yaml`；`member_terms` + 三层匹配 | `sync_performances_from_wiki.py`、`sync_member_terms_from_wiki.py` |
| 4 | 展示层：前端筛选按配置生成；Emby 系列/目录加分团前缀；素材映射按分团 | 前端与素材工具改造 |
| 5 | 全分团接入：各分团 B 站账号订阅、排期、下载、通知全流程 | 新分团上线验收 |

## 五、已确认决策

1. 按本文档推荐顺序推进：阶段 0 → 1 → 2 → 3 → 4 → 5。
2. Emby 采用单库 + 分团前缀的系列命名。
3. 数据权威性：官方票务接口 = 排期；B站官方投稿 = 录播；Fandom wiki =
   花名册与剧目阵容；人工配置优先级最高。
4. 花名册同步采用 wiki 覆盖 `members`，保留 `oshis / notify / keywords /
   enabled / audio_offset` 等人工字段，每次同步输出差异报告。
5. `performances.yaml` 定位为「自动生成 + 人工修正」的覆盖层。
6. 成员以中文名为主，跨分团重名时用分团后缀消歧。

## 六、风险与注意事项

- 队伍代码不跨分团唯一（`g` 同时出现在 gnz 与 cgt），必须使用 `(group, team)`。
- 巡演场次的「档期所属分团」与「队伍所属分团」不同，需要分开存储。
- wiki 任职时间通常精确到年或年月，跨年中转会存在几个月误差，可用
  `performances.yaml` 人工修正。
- Emby 目录/系列改名会触发重新扫描，建议一次性确定命名规则。
- 改造分支为 `refactor/multi-group`，禁止直接在 `master` 上修改。

## 七、已踩过的坑（实现记录）

- **紧凑写法 Infobox**：部分成员页把整个 Infobox 写在一行，逐行解析会越界
  读到后续参数（甚至把 `generation` 当成队伍），必须按 `|参数=` 截断。
- **未标注日期的队伍字段**：如 `[[Team SII]]` 没有年份区间，早期规则要求出现
  `present` 才视为在册，导致芦馨怡这类成员被误剔除；现在改为「有区间则要求
  present/至今，无区间视为在册」，并额外排除 `Former ...` 与 `inactive`。
- **同名剧目**：如「十八个闪耀瞬间」四个队都有，关键词匹配必须带队伍维度，
  否则会串台。
- **新生公演**：SNH48 的「NEW 心的旅程」「NEW 命运 X 号」等属于
  `Category:SNH48 New Members Stages`，与同名正场剧目重名，需要按队伍区分，
  并额外生成去掉 `NEW ` 前缀的关键词。
