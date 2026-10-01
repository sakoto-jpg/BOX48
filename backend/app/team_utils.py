"""队伍名称清洗与对齐工具。

统一使用以下规范名称，供 uploads / downloads / performances 三张表对齐：
  TEAM NII / TEAM HII / TEAM SII / TEAM X / 预备生 / SNH48 GROUP

规则（以标题为准）：
- 含 SII / NII / HII / X 队关键词 → 对应队伍
- 含「预备生」或「新生」→ 预备生
- 其余（含联合生日公演等特殊场次、YBS 梦之门等）→ SNH48 GROUP

多分团（阶段 5）：
- 非 snh 分团按 groups.yaml 里各队伍的 keywords 匹配，返回队伍规范名
  （如 GNZ48 TEAM G）；匹配不上时返回分团级的「XX48 GROUP」。
- SNH 保持既有规则不变，避免影响已入库数据。
"""

import re

import group_config

CANON_NII = "TEAM NII"
CANON_HII = "TEAM HII"
CANON_SII = "TEAM SII"
CANON_X = "TEAM X"
CANON_TRAINEE = "预备生"
CANON_GROUP = "SNH48 GROUP"

# 日历服务写入的 team 代码 → 规范名
CODE_TO_CANON = {
    "n": CANON_NII,
    "h": CANON_HII,
    "s": CANON_SII,
    "x": CANON_X,
    "ybs": CANON_TRAINEE,
}

# 下载目录名 → 规范名（用于标题无法判断时的兜底）
FOLDER_TO_CANON = {
    CANON_NII: CANON_NII,
    CANON_HII: CANON_HII,
    CANON_SII: CANON_SII,
    CANON_X: CANON_X,
    "YBS": CANON_TRAINEE,
    CANON_TRAINEE: CANON_TRAINEE,
    CANON_GROUP: CANON_GROUP,
    "特殊公演": CANON_GROUP,
}


def clean_team_from_title(title: str) -> str:
    text = title or ""
    upper = text.upper()

    nii_hit = "TEAM NII" in upper or "N队" in text
    if nii_hit and ("NIII" not in upper or "NII" in upper.replace("NIII", "")):
        return CANON_NII
    if "TEAM H" in upper or "H队" in text:
        return CANON_HII
    if "TEAM S" in upper or "S队" in text:
        return CANON_SII
    if "TEAM X" in upper or "X队" in text:
        return CANON_X
    if "预备生" in text or "新生" in text:
        return CANON_TRAINEE
    return CANON_GROUP


def normalize_team(title: str, fallback: str = "") -> str:
    """标题优先；标题只能判为 SNH48 GROUP 时，用兜底值（目录名/队伍代码）。"""
    team = clean_team_from_title(title)
    if team != CANON_GROUP or not fallback:
        return team
    if fallback in FOLDER_TO_CANON:
        return FOLDER_TO_CANON[fallback]
    return CODE_TO_CANON.get(str(fallback).lower(), CANON_GROUP)


# 分团级的兜底名称：标题里认不出具体队伍时使用
GROUP_LABELS = {
    "snh": CANON_GROUP,
    "gnz": "GNZ48 GROUP",
    "bej": "BEJ48 GROUP",
    "ckg": "CKG48 GROUP",
    "cgt": "CGT48 GROUP",
}

_PATTERN_CACHE = {}


def group_label(group_code: str) -> str:
    return GROUP_LABELS.get(group_code or "snh", CANON_GROUP)


def _keyword_pattern(keyword: str):
    """关键词匹配：纯 ASCII 关键词要求前后不是字母数字，避免 TEAM G 命中 TEAM GII。"""
    pattern = _PATTERN_CACHE.get(keyword)
    if pattern is not None:
        return pattern
    escaped = re.escape(keyword)
    if re.search(r"[A-Za-z0-9]", keyword):
        pattern = re.compile(rf"(?<![A-Za-z0-9]){escaped}(?![A-Za-z0-9])", re.I)
    else:
        pattern = re.compile(escaped)
    _PATTERN_CACHE[keyword] = pattern
    return pattern


def match_group_team(title: str, group_code: str) -> str:
    """按 groups.yaml 中该分团各队伍的 keywords 匹配队伍规范名，未命中返回空串。"""
    text = title or ""
    candidates = []
    for team in group_config.teams():
        if team.get("group") != group_code:
            continue
        for keyword in team.get("keywords") or []:
            keyword = str(keyword).strip()
            if keyword:
                candidates.append((len(keyword), keyword, team.get("name", "")))
    for _, keyword, name in sorted(candidates, key=lambda item: -item[0]):
        if _keyword_pattern(keyword).search(text):
            return name
    return ""


def resolve_team(title: str, group_code: str = "snh") -> str:
    """按分团解析标题里的队伍；snh 沿用既有规则，其他分团走配置关键词。"""
    if not group_code or group_code == "snh":
        return clean_team_from_title(title)
    return match_group_team(title, group_code) or group_label(group_code)
