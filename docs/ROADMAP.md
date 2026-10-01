# SNH48 工具链改造路线图（多分团支持）

> 阶段 0 基线文档：记录现状、需求、目标架构与实施顺序。后续改动都在
> `refactor/multi-group` 分支上进行，按阶段推进、每阶段可独立上线。

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

### 3.3 演员表三层匹配

1. 剧目名单（`performances.yaml`，人工覆盖优先）；
2. 按公演日期 + 队伍从 `member_terms` 计算当时阵容；
3. 当前 `roster.yaml` 队伍名单（最终兜底）。

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
