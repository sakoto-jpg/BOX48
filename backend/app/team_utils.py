"""队伍名称清洗与对齐工具。

统一使用以下规范名称，供 uploads / downloads / performances 三张表对齐：
  TEAM NII / TEAM HII / TEAM SII / TEAM X / 预备生 / SNH48 GROUP

规则（以标题为准）：
- 含 SII / NII / HII / X 队关键词 → 对应队伍
- 含「预备生」或「新生」→ 预备生
- 其余（含联合生日公演等特殊场次、YBS 梦之门等）→ SNH48 GROUP
"""

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
