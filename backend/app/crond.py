"""管理系统 crontab 里「由本工具维护」的定时任务块。

宿主机把 /var/spool/cron/crontabs 挂进容器（见 docker-compose.yml），
这里直接读写其中的 root 文件：
  - 原地覆盖写（不 rename），保持 root:crontab 600 权限
  - cron 每分钟检查文件 mtime，写完下一分钟自动生效，无需重启服务
  - 只替换 BEGIN/END 标记之间的内容，其他条目原样保留

配置来自 roster.yaml 的 refresh 段：
    refresh:
      uploads:
        enabled: true
        mode: cron          # cron=每日定时 | interval=间隔轮询
        cron_hours: [7]     # 北京时间整点
        cron_minute: 30
        interval_hours: 6
        latest: 10
        full_interval_hours: 6
"""

import os
import shutil
import time

CRONTAB_PATH = os.environ.get("CRONTAB_PATH", "/host-crontabs/root")
BACKUP_DIR = os.environ.get("CRONTAB_BACKUP_DIR", "/config/backups")

MANAGED_BEGIN = "# >>> snh48-downloader managed >>>"
MANAGED_END = "# <<< snh48-downloader managed <<<"

CONTAINER = "snh48-downloader-backend"
UPLOAD_LOG = "/var/log/snh48_uploads.log"

DEFAULT_UPLOADS = {
    "enabled": True,
    "mode": "cron",
    "cron_hours": [7],
    "cron_minute": 30,
    "interval_hours": 6,
    "latest": 10,
    "full_interval_hours": 6,
}


def _uploads_conf(refresh: dict) -> dict:
    conf = dict(DEFAULT_UPLOADS)
    conf.update((refresh or {}).get("uploads") or {})
    return conf


def _to_utc(hour_beijing: int, minute: int) -> tuple:
    """crontab 按 UTC 执行，配置里填的是北京时间。"""
    return (int(hour_beijing) - 8) % 24, int(minute)


def _upload_cmd(args: str) -> str:
    return (f"/usr/bin/docker exec {CONTAINER} python /app/tools/sync_uploads.py "
            f"{args} >> {UPLOAD_LOG} 2>&1")


def build_lines(refresh: dict) -> list:
    """按配置生成受管块的行（不含 BEGIN/END 标记）。"""
    conf = _uploads_conf(refresh)
    lines = []
    latest = int(conf.get("latest", 10))
    incremental_cmd = _upload_cmd(f"--mode incremental --latest {latest}")
    if conf.get("enabled", True):
        mode = conf.get("mode", "cron")
        if mode == "interval":
            hours = max(1, min(23, int(conf.get("interval_hours", 6))))
            lines.append(f"# 投稿目录：每 {hours} 小时增量抓取最近 {latest} 条")
            lines.append(f"0 */{hours} * * * {incremental_cmd}")
        else:
            hours = conf.get("cron_hours") or [7]
            minute = int(conf.get("cron_minute", 30))
            for hour in hours:
                utc_hour, utc_minute = _to_utc(hour, minute)
                lines.append(
                    f"# 投稿目录：北京时间 {int(hour):02d}:{minute:02d} 抓取最近 {latest} 条"
                    f"（UTC {utc_hour:02d}:{utc_minute:02d}）")
                lines.append(f"{utc_minute} {utc_hour} * * * {incremental_cmd}")
        full_hours = max(1, min(23, int(conf.get("full_interval_hours", 6))))
        lines.append(f"# 投稿目录：每 {full_hours} 小时续抓未完成的详情（全量模式会断点续爬）")
        lines.append(f"0 */{full_hours} * * * {_upload_cmd('--mode full')}")
    else:
        lines.append("# 投稿目录自动抓取：已停用")
    return lines


def read() -> str:
    try:
        with open(CRONTAB_PATH, encoding="utf-8") as fh:
            return fh.read()
    except OSError:
        return ""


def _strip_managed(text: str) -> str:
    """移除旧的受管块与历史遗留的同步行，返回剩余内容。"""
    out, skipping = [], False
    for line in text.splitlines():
        if line.strip() == MANAGED_BEGIN:
            skipping = True
            continue
        if line.strip() == MANAGED_END:
            skipping = False
            continue
        if skipping:
            continue
        # 首次接管前留下的两行（不在受管块里）
        if "tools/sync_uploads.py" in line:
            continue
        if line.strip() == "# SNH48 官方投稿目录同步":
            continue
        out.append(line)
    while out and not out[-1].strip():
        out.pop()
    return "\n".join(out)


def apply(refresh: dict) -> dict:
    """按配置重写 crontab 的受管块，返回改动信息。"""
    current = read()
    if not current:
        return {"ok": False, "message": f"读取不到 crontab: {CRONTAB_PATH}"}
    body = _strip_managed(current)
    lines = build_lines(refresh)
    new_text = body + "\n\n" + "\n".join([MANAGED_BEGIN, *lines, MANAGED_END]) + "\n"
    if new_text == current:
        return {"ok": True, "changed": False, "message": "定时任务无变化"}

    os.makedirs(BACKUP_DIR, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup = os.path.join(BACKUP_DIR, f"crontab.{stamp}.bak")
    try:
        shutil.copy2(CRONTAB_PATH, backup)
    except OSError:
        backup = ""
    # 原地覆盖写：保住 root:crontab 600 的属主与权限
    with open(CRONTAB_PATH, "w", encoding="utf-8") as fh:
        fh.write(new_text)
    return {"ok": True, "changed": True, "message": "定时任务已更新",
            "backup": backup, "lines": lines}


def describe(refresh: dict) -> dict:
    """给前端展示：受管块内容 + 配置解析结果。"""
    conf = _uploads_conf(refresh)
    return {
        "path": CRONTAB_PATH,
        "readable": bool(read()),
        "uploads": conf,
        "lines": build_lines(refresh),
        "managed_begin": MANAGED_BEGIN,
        "managed_end": MANAGED_END,
    }
