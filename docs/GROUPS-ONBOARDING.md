# 阶段 5：新分团接入指南

> 目标：把 GNZ48 / BEJ48 / CKG48 / CGT48 接入现有流程（排期、投稿、
> 花名册、剧目名单、下载入库、Emby 展示）。本文记录 2026-10-01 的摸底
> 结果、每个分团的接入清单、以及动手前必须补的代码缺口。

## 一、摸底结果

### 1.1 B 站官方账号（已核实）

| 分团 | mid | 昵称 | 粉丝 |
|---|---|---|---|
| SNH48 | 2832224 | SNH48官方账号 | 79.8 万 |
| GNZ48 | 27363163 | GNZ48官方账号 | 22.5 万 |
| BEJ48 | 26849519 | BEJ48 | 9.2 万 |
| CKG48 | 174377185 | CKG48官方账号 | 4.5 万 |
| CGT48 | 3493138635163905 | CGT48官方账号 | 3.9 万 |

CKG48 / CGT48 没有 B 站官方认证标识，但昵称与粉丝量可信；接入前建议人工
确认一次。

### 1.2 官方票务接口（阶段 1 已确认）

`gid`：1=snh、2=bej、3=gnz、5=ckg、6=cgt。
返回的 `s_tag` 取值：`n/h/s/x/def`（SNH）、`b/e/union`（BEJ）、
`g/niii/z/union` 与 `gnz48预备生`（GNZ）、`c/k` 与 `union`（CKG/CGT）。

注意：**只有 GNZ 的接口直接给训练生队伍代码**，SNH 靠 `def` + 关键词判定，
BEJ/CKG/CGT 目前只看到 `union`（拼盘场次），训练生场次需要靠关键词规则。

### 1.3 wiki 分类覆盖（已确认齐全）

每个分团都有 `Members / Graduates / Trainees` 三类成员分类，以及各队的
`Team X Stages` 剧目分类：

| 分团 | 成员 | 毕业 | 训练生 | 剧目页 |
|---|---|---|---|---|
| GNZ48 | 67 | 97 | 29 | G 10 / NIII 6 / Z 8 |
| BEJ48 | 40 | 75 | 11 | B 5 / E 4 / J 3 |
| CKG48 | 34 | 54 | 7 | C 6 / K 6 |
| CGT48 | 40 | 30 | 29 | CII 5 / GII 5 |

### 1.4 全分团同步试运行结果（dry-run，未写入）

```
任职区间：SNH48 431 条 / GNZ48 225 条 / BEJ48 123 条 / CKG48 73 条 / CGT48 34 条
          合计 828 条，覆盖 612 名成员
剧目名单：113 个剧目（SNH 56 + 其他分团 57），覆盖 560 名成员
```

各队累计成员（含历史）：gnz/g 46、gnz/niii 48、gnz/z 46、bej/b 50、
bej/e 53、bej/j 15、ckg/c 33、ckg/k 31、cgt/cii 22、cgt/gii 15。

也就是说**数据层已经可以覆盖全部分团**，剩下的主要是流程与展示层的接通。

### 1.5 试运行暴露的问题

1. **训练生队伍只配了 GNZ**：`BEJ48 Trainee`、`CKG48 Trainee`、`CGT48 Trainee`
   出现在未映射列表里，需要为这三个分团补训练生队伍定义。
2. **`Former Team NIII` 这种写法**没被识别：部分成员页把历史队伍写成
   `[[Former Team NIII]]`（而不是 `[[Team NIII|Former Team NIII]]`），需要在
   队伍名匹配时剥掉 `Former ` 前缀。
3. **`心的旅程` 没有解析到名单**：这套剧目的成员模板结构与其它剧目不同，
   需要单独看一下（目前它会被当成没有名单的剧目跳过）。
4. 其他未映射项（`Class 4/8`、`Team SIII`、`w:c:AKB48:*`、`Support Member`）
   属于跨团/元数据噪声，保持跳过即可。

## 二、接入一个分团的完整清单

按顺序做，每一步都可以单独验证：

1. **配置**：`groups.yaml` 填 `bilibili_mid`，把 `enabled` 置为 `true`；
   补该分团的训练生队伍定义（`trainee.mode` / 关键词）。
   `enabled` 只决定「排期 + 花名册 + 前端标签页」；投稿抓取由
   `sync_uploads` 单独控制（`false` = 排队中，等前面分团跑完再打开），
   这样多个分团可以错峰上线。
2. **花名册**：在 `roster.yaml` 生成该分团的队伍条目（名称、关键词、
   成员、`enabled`、`notify`），执行一次 `sync_wiki_all.py --apply`。
3. **任职区间 + 剧目名单**：同上一条命令会一并同步；检查
   `member_terms_report.json` 与 `performances_report.json` 是否有异常。
4. **排期**：`enabled: true` 后日历服务会自动开始抓该 `gid` 的排期。
   注意日历服务有 24 小时内存缓存（`CACHE_TTL`），**新增分团后要重启一次
   `snh48-calendar` 容器**才会立刻抓取；否则要等缓存过期。
   重启后确认 `/api/tickets` 与 ICS 里出现新分团（GNZ48 实测 12 条）。
   另外日历服务是**镜像内代码 + 只挂载 /config**，改代码后必须
   `docker compose build snh48-calendar && docker compose up -d snh48-calendar`，
   单纯 restart 不会加载新代码。
5. **投稿目录**：投稿同步需要按分团循环（见下面的代码缺口 2），第一次
   全量抓取会很久，之后每天增量 10 条。
6. **下载入库**：确认标题 → 队伍的匹配（该分团的 `keywords`）、目录结构、
   编码策略（全分团统一 AV1 → H.265 → H.264）。
7. **Emby**：系列名、目录名、素材映射都要带分团前缀（见缺口 5）。
8. **前端**：分团标签页要能只显示本分团数据（依赖阶段 4b）。
9. **通知**：确认新分团的公演提醒要不要推、推到哪个渠道。

## 三、动手前必须补的代码缺口

| # | 缺口 | 位置 | 说明 |
|---|---|---|---|
| 1 | 投稿同步写死 SNH 账号 | `tools/sync_uploads.py` | ✅ 已完成：按分团循环，per-group 同步状态，`upload_queue.group_code`，老状态键自动迁移 |
| 2 | 花名册不能自动生成新队伍 | `tools/sync_roster_from_wiki.py` | ✅ 已完成：`--create-missing`（配合 `--all-groups`），新条目默认 `enabled: false` |
| 3 | 训练生规则只覆盖 SNH/GNZ | `groups.yaml` | ✅ 已完成：补 BEJ/CKG/CGT 训练生队伍与关键词；票务 `union` 场次靠关键词判定 |
| 4 | 历史队伍名写法 | `tools/wiki_client.py` | ✅ 已完成：`[[Former Team NIII]]` 归一为 Team NIII 并标记 former |
| 5 | Emby 命名 | `nfo_builder.py`、`tools/emby_artwork.py` | ✅ 已完成：统一「分团前缀 + 队伍名」；`TEAM_PAGES` 覆盖五个分团并保留旧名兼容；新增 `tools/migrate_media_dirs.py` 做目录迁移 |
| 6 | 个别剧目缺少成员名单 | `tools/sync_performances_from_wiki.py` | ✅ 已定位：同名《心的旅程》有三场，其中 `Team B 2nd Stage`（BEJ48）引用的 `Template:B2 Performing Members` 在 wiki 上不存在，属于 wiki 侧缺失；工具已改进为逐条打印「页面 / 剧目 / 队伍 / 模板名 / 解析到的人数」 |
| 7 | 前端分团过滤 | 阶段 4b | ⏳ 待做：接口加 `group_code`，否则标签页只能显示全局数据 |
| 8 | 日历分团去重 | `calendar/app/main.py` | ✅ 已修：合并去重键原来只有「日期+时间」，不同分团同点开演会互相覆盖标题（BEJ 的场次曾被 GNZ 覆盖），现在键里带 `group_code` |

另外补了一个写入护栏：wiki 抓取出现失败（或任职区间抓到 0 条）时，三个写入型
工具会拒绝覆盖，需要 `--allow-partial` 才强行写入——避免一次网络抖动把花名册/
任职区间/剧目名单写成残缺版本。

## 三点五、自动生成新分团队伍条目的实测结果

在 roster.yaml 副本上跑 `--create-missing --all-groups`，生成了 14 个新条目
（默认 `enabled: false`，要在接入时手动打开）：

| 队伍 | 成员数 | 备注 |
|---|---|---|
| GNZ48 TEAM G / NIII / Z | 13 / 12 / 12 | 与 wiki 分类规模一致 |
| GNZ48 预备生 | 29 | 与 `Category:GNZ48 Trainees` 一致 |
| BEJ48 TEAM B / E / J | 13 / 16 / 0 | TEAM J 分类只有 2 个页面且都已离队，0 人符合现状 |
| BEJ48 预备生 | 11 | |
| CKG48 TEAM C / K | 12 / 11 | |
| CKG48 预备生 | 7 | |
| CGT48 TEAM CII / GII | 5 / 0 | GII 分类只有 1 个页面，尚未成队 |
| CGT48 预备生 | 29 | |

接入时要核对一处：`GNZ48 TEAM G` 花名册给 13 人，而 wiki 分类里在册是 14 人，
差 1 人疑似被 `theme` 校验过滤（兼任成员的分团字段可能不同），接入时逐个核对。

### 三点六、同名剧目与缺失模板（2026-10-01 修正结论）

「心的旅程」在 wiki 上有三场，不要混淆：

| 页面 | 归属 | 名单 |
|---|---|---|
| `SNH48 6th Special Stage` | 训练生（NEW 心的旅程，2026-05-01 首演） | ✅ 16 人 |
| `Team SII 6th Stage` | SNH48 TEAM SII（2016-05-20 首演） | ✅ 18 人 |
| `Team B 2nd Stage` | BEJ48 TEAM B（2016-10-02 首演，SII 版的复刻） | ❌ 页面引用的 `Template:B2 Performing Members` 不存在 |

只有第三场拿不到名单，属于 wiki 侧缺模板；接入 BEJ48 时该剧目会退回
`member_terms`（按年任职区间）或花名册。

## 四、需要你确认的决策

1. **先接哪个分团**：建议 GNZ48（账号最活跃、wiki 覆盖最全、粉丝最多），
   跑通一遍后再批量复制到其余三个。
2. **Emby 目录/系列命名**：✅ 已定案并落地——统一用「分团前缀 + 队伍名」，
   例如 `SNH48 TEAM NII`、`SNH48 预备生`、`SNH48 特殊公演`、`GNZ48 TEAM G`。
   旧的 `TEAM NII` / `预备生` / `特殊公演` / `SNH48 GROUP` 已由
   `tools/migrate_media_dirs.py` 改名（`SNH48 GROUP` 合并进 `SNH48 特殊公演`），
   downloads / orphan_files 的路径同步更新，Emby 已触发整库重扫。
3. **存储容量**：新增四个分团的录播，量级大约是现在的 2～4 倍，需要确认
   媒体库空间。
4. **下载优先级**：多分团同时更新时是否要限制并发（当前是全局串行的单队列，
   同一时间只下一场，理论上不用改）。

## 五、建议的推进顺序

1. 先补代码缺口 1～4（配置与数据层，不影响现有 SNH 流程）。
2. 补缺口 7（前端 4b），让标签页真正按分团工作。
3. 开启 GNZ48 做端到端验收（排期 → 投稿 → 下载 → 入库 → Emby → 前端）。
4. 验收通过后依次开启 BEJ48 / CKG48 / CGT48，每个分团重复第二节清单。
