import os
import re
import glob
import shutil
import threading
import yaml
import requests
import datetime
import group_config
import maintenance
from emby_client import EmbyClient
from fastapi import FastAPI, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from apscheduler.schedulers.background import BackgroundScheduler
from scheduler import Snh48Scanner
from team_utils import keyword_hit
from uploads_api import router as uploads_router

app = FastAPI(title="SNH48 Downloader API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(uploads_router, prefix="/api")

# 下载等待队列：只放「还没轮到的任务」，正在下载的那条由 scanner.active_tasks 展示
download_queue = []
download_queue_lock = threading.Lock()
download_run_lock = threading.Lock()   # 同一时间只处理一批，保证下载串行
download_queue_seq = 0

CONFIG_PATH = os.environ.get("CONFIG_PATH", "/config/roster.yaml")
COOKIE_PATH = os.environ.get("COOKIE_PATH", "/cookie/cookie.txt")
DOWNLOAD_PATH = os.environ.get("DOWNLOAD_PATH", "/downloads")
NOTIFIED_FILE = "/config/notified.txt"
POCKET48_NOTIFIED_FILE = "/config/pocket48_notified.txt"

scanner = Snh48Scanner(CONFIG_PATH, COOKIE_PATH, DOWNLOAD_PATH)
scheduler = BackgroundScheduler()

def get_current_config():
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
            return yaml.safe_load(f) or {}
    return {}

# 🚀 改造推送引擎，支持 type 字段和全新的规范格式
def send_apprise_msg(title: str, body: str, msg_type: str = "info"):
    config = get_current_config()
    apprise_url = config.get("apprise_url", "")
    if not apprise_url:
        return
    try:
        payload = {
            "title": title,
            "body": body,
            "type": msg_type
        }
        requests.post(apprise_url, json=payload, timeout=5)
        print(f"📱 微信推送成功: {title}")
    except Exception as e:
        print(f"❌ Apprise 推送失败: {e}")

def reschedule_scan_job():
    config = get_current_config()
    sched_cfg = config.get("scheduler", {})
    enabled = sched_cfg.get("enabled", True)
    mode = sched_cfg.get("mode", "cron")

    job = scheduler.get_job("scan_all")

    if not enabled:
        if job:
            scheduler.pause_job("scan_all")
            print("⏸️ 自动扫描调度已暂停")
        return

    if mode == "cron":
        hours = sched_cfg.get("cron_hours", [13, 23])
        if not hours:
            hours = [13]
        hour_str = ",".join(str(int(h)) for h in hours)
        if job:
            scheduler.reschedule_job("scan_all", trigger="cron", hour=hour_str)
        else:
            scheduler.add_job(scanner.scan_all, "cron", hour=hour_str, id="scan_all")
    elif mode == "interval":
        interval_h = max(1, int(sched_cfg.get("interval_hours", 6)))
        if job:
            scheduler.reschedule_job("scan_all", trigger="interval", hours=interval_h)
        else:
            scheduler.add_job(scanner.scan_all, "interval", hours=interval_h, id="scan_all")
    else:
        if job:
            scheduler.reschedule_job("scan_all", trigger="cron", hour="13,23")
        else:
            scheduler.add_job(scanner.scan_all, "cron", hour="13,23", id="scan_all")

    scheduler.resume_job("scan_all")
    print(f"✅ 扫描调度已更新: mode={mode}")

def check_reminders():
    config = get_current_config()
    apprise_url = config.get("apprise_url", "")
    if not apprise_url: return

    try:
        res = requests.get("http://192.168.1.12:8099/api/tickets", timeout=10)
        data = res.json().get("data", [])
    except Exception as e:
        print(f"⚠️ 无法连接公演排期 API: {e}")
        return

    now = datetime.datetime.now()
    
    if not os.path.exists(NOTIFIED_FILE):
        open(NOTIFIED_FILE, 'w').close()
    with open(NOTIFIED_FILE, 'r') as f:
        notified = set(f.read().splitlines())
        
    new_notified = []

    for show in data:
        show_id = show.get("id")
        title = show.get("title")
        date_str = show.get("date") 
        time_str = show.get("show_time") 
        
        try:
            month = int(date_str.split('月')[0])
            day = int(date_str.split('月')[1].replace('日', ''))
            hour, minute = map(int, time_str.split(':'))
            show_dt = datetime.datetime(now.year, month, day, hour, minute)
            
            if show_dt < now - datetime.timedelta(days=30):
                show_dt = show_dt.replace(year=now.year + 1)
        except: continue

        if now >= show_dt:
            continue

        should_notify = False
        if show_id in config.get("manual_subs", []):
            should_notify = True
        else:
            show_group = show.get("group_code") or "snh"
            for team in config.get("teams", []):
                # 🚀 排期提醒与"录播自动入库"解耦：只由队伍的通知开关(notify)控制
                if team.get("notify", False):
                    # 分团限定：GNZ 场次不会去匹配 SNH 队伍（预备生/特殊公演这类
                    # 通用关键词在多个分团重复出现）
                    team_group = (group_config.team_by_name(team.get("name", "")) or {}).get("group")
                    if team_group and team_group != show_group:
                        continue
                    # 关键词和队伍名都参与匹配：日历给 def 场次补的「预备生」标签也能命中
                    # 匹配带词边界，避免 TEAM NII 命中 TEAM NIII、TEAM G 命中 TEAM GII
                    patterns = list(team.get("keywords", [])) + [team.get("name", "")]
                    for kw in patterns:
                        clean_kw = str(kw).replace("《", "").replace("》", "").strip()
                        if clean_kw and keyword_hit(clean_kw, title):
                            should_notify = True
                            break
                if should_notify: break
                
        if not should_notify: continue

        day_before_22 = (show_dt - datetime.timedelta(days=1)).replace(hour=22, minute=0, second=0, microsecond=0)
        is_valid_day_before = day_before_22 <= now < (show_dt - datetime.timedelta(hours=2))
        
        min_30_before = show_dt - datetime.timedelta(minutes=30)
        is_valid_30min = min_30_before <= now < show_dt
        
        if is_valid_day_before and f"{show_id}_daybefore" not in notified:
            day_prefix = "明日" if now.date() < show_dt.date() else "今日"
            # 🚀 格式化：使用 info 类型发送日常提醒
            send_apprise_msg(
                title=f"[SNH48-DownLoader] {day_prefix}公演提醒",
                body=f"【{title}】\n📅 时间：{date_str} {time_str}\n🔗 票务：{show.get('link')}",
                msg_type="info"
            )
            notified.add(f"{show_id}_daybefore")
            new_notified.append(f"{show_id}_daybefore")
            
        if is_valid_30min and f"{show_id}_30min" not in notified:
            # 🚀 格式化：即将开场，使用 warning 引起注意
            send_apprise_msg(
                title="[SNH48-DownLoader] 演出即将开始！",
                body=f"【{title}】\n📅 时间：今天 {time_str} (30分钟后开场)\n🔗 票务：{show.get('link')}",
                msg_type="warning"
            )
            notified.add(f"{show_id}_30min")
            new_notified.append(f"{show_id}_30min")

    if new_notified:
        with open(NOTIFIED_FILE, 'a') as f:
            for n in new_notified:
                f.write(f"{n}\n")

def check_pocket48_live():
    config = get_current_config()
    apprise_url = config.get("apprise_url", "")
    if not apprise_url: return

    # 🚀 开播提醒与"录播自动入库"解耦：监控所有队伍的 oshi，只看名单本身
    all_oshis = set()
    for team in config.get("teams", []):
        for oshi in team.get("oshis", []):
            all_oshis.add(oshi)
                
    if not all_oshis: return

    url = "https://pocketapi.48.cn/live/api/v1/live/getLiveList"
    headers = {
        "Content-Type": "application/json;charset=utf-8",
        "User-Agent": "PocketFans201807/6.0.0 (iPhone; iOS 14.4; Scale/3.00)",
        "appInfo": '{"vendor":"apple","deviceId":"0","appVersion":"6.0.0","appBuild":"200513","osVersion":"14.4.0","osType":"ios","deviceName":"iphone","os":"ios"}',
    }
    payload = {
        "debug": True,
        "next": "0",
        "groupId": 0,
        "record": False
    }

    try:
        res = requests.post(url, json=payload, headers=headers, timeout=10)
        data = res.json()
        live_list = data.get("content", {}).get("liveList", [])
    except Exception as e:
        print(f"⚠️ 口袋48接口请求失败: {e}")
        return

    if not os.path.exists(POCKET48_NOTIFIED_FILE):
        open(POCKET48_NOTIFIED_FILE, 'w').close()
    with open(POCKET48_NOTIFIED_FILE, 'r') as f:
        notified = set(f.read().splitlines())

    new_notified = []

    for live in live_list:
        live_id = str(live.get("liveId"))
        title = live.get("title", "直播间")
        
        user_info = live.get("userInfo", {})
        nickname = user_info.get("nickname", "")
        star_name = user_info.get("starName", "")
        
        live_type = live.get("liveType", 1) 
        type_str = "📺 视频直播" if live_type == 1 else "📻 电台语音"

        is_oshi = (star_name in all_oshis) or any(oshi in nickname for oshi in all_oshis)

        if is_oshi and live_id not in notified:
            display_name = star_name if star_name else nickname
            # 🚀 格式化：使用 success 推送开播喜报
            send_apprise_msg(
                title=f"[SNH48-DownLoader] {display_name} 开播啦！",
                body=f"标题：{title}\n类型：{type_str}\n快去口袋48APP围观吧！",
                msg_type="success"
            )
            notified.add(live_id)
            new_notified.append(live_id)

    if new_notified:
        with open(POCKET48_NOTIFIED_FILE, 'a') as f:
            for n in new_notified:
                f.write(f"{n}\n")

@app.on_event("startup")
def start_scheduler():
    scheduler.add_job(check_reminders, 'interval', minutes=5, id='check_reminders')
    scheduler.add_job(check_pocket48_live, 'interval', minutes=5, id='check_pocket48_live')
    reschedule_scan_job()
    scheduler.start()
    print("✅ 定时任务及通知引擎已启动(包含口袋48监控)")

@app.on_event("shutdown")
def stop_scheduler():
    scheduler.shutdown()

@app.get("/")
def read_root():
    return {"status": "running"}

@app.post("/api/trigger_scan")
def trigger_scan_manually(background_tasks: BackgroundTasks):
    # 前端手动触发的扫描跟随「手动入库」下载范围设置（仅MC环节/整场公演）
    background_tasks.add_task(scanner.scan_all, True)
    return {"message": "已在后台触发全局扫描任务"}

@app.get("/api/config")
def get_config():
    return get_current_config()

@app.get("/api/groups")
def list_groups():
    """分团列表：前端标签页顺序、主题色与启用状态都由这里下发。"""
    groups = group_config.groups()
    teams = group_config.teams()
    result = []
    for item in groups:
        code = item.get("code", "")
        result.append({
            "code": code,
            "name": item.get("name", code),
            "theme_color": item.get("theme_color") or "#64748b",
            "enabled": bool(item.get("enabled", False)),
            "bilibili_mid": item.get("bilibili_mid"),
            "trainee": item.get("trainee") or {},
            "teams": [
                {"code": team.get("code"), "name": team.get("name")}
                for team in teams if team.get("group") == code
            ],
        })
    return result

@app.post("/api/config")
def save_config(config: dict):
    with open(CONFIG_PATH, 'w', encoding='utf-8') as f:
        yaml.dump(config, f, allow_unicode=True, sort_keys=False)
    scanner.nfo_builder.reload_config()
    scanner.reload_emby_client()
    reschedule_scan_job()
    return {"message": "配置已保存并生效"}

@app.post("/api/reload_metadata")
def reload_metadata():
    """重新加载 roster.yaml 与剧目名单（wiki 定时同步后调用，无需重启容器）。"""
    scanner.nfo_builder.reload_config()
    return {
        "message": "花名册与剧目名单已重新加载",
        "teams": len(scanner.nfo_builder.roster),
        "performances": len(scanner.nfo_builder.performances),
    }


# ── 设置页（阶段 5a）：通知 / Emby / 分团账号 ─────────────────────────
GROUPS_CONFIG_PATH = group_config.RUNTIME_PATH
GROUPS_BACKUP_DIR = os.path.join(os.path.dirname(GROUPS_CONFIG_PATH), "backups")
GROUPS_EDITABLE = ("bilibili_mid", "enabled", "sync_uploads", "theme_color")


def _backup_file(path: str, keep: int = 10):
    """按时间戳备份文件，只保留最近 keep 份。"""
    if not os.path.exists(path):
        return
    os.makedirs(GROUPS_BACKUP_DIR, exist_ok=True)
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    shutil.copy2(path, os.path.join(GROUPS_BACKUP_DIR, f"{os.path.basename(path)}.{stamp}.bak"))
    history = sorted(glob.glob(os.path.join(GROUPS_BACKUP_DIR,
                                            f"{os.path.basename(path)}.*.bak")))
    for stale in history[:-keep]:
        os.remove(stale)


def _atomic_write_yaml(path: str, data: dict):
    """原子写 YAML：先写临时文件再 rename，避免正在跑的同步任务读到半截内容。

    yaml.dump 不保留注释，这里把原文件开头的说明注释块原样带回去。
    """
    header = []
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("#") or not line.strip():
                    header.append(line.rstrip("\n"))
                else:
                    break
    except OSError:
        header = []
    while header and not header[-1].strip():
        header.pop()
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        if header:
            fh.write("\n".join(header) + "\n")
        yaml.dump(data, fh, allow_unicode=True, sort_keys=False)
    os.replace(tmp, path)


@app.post("/api/notify_test")
def notify_test(payload: dict = None):
    """发送一条测试推送，验证 Apprise 地址是否可用。"""
    url = ((payload or {}).get("apprise_url") or "").strip() \
        or (get_current_config().get("apprise_url") or "").strip()
    if not url:
        return {"ok": False, "message": "请先填写推送地址"}
    try:
        resp = requests.post(
            url,
            json={
                "title": "[SNH48-DownLoader] 测试推送",
                "body": "如果你看到这条消息，说明推送地址配置正确。\n"
                        "公演开播提醒与口袋48开播提醒都会发到这里。",
                "type": "info",
            },
            timeout=10,
        )
        if resp.status_code < 400:
            return {"ok": True, "message": f"已发送（HTTP {resp.status_code}）"}
        return {"ok": False, "message": f"HTTP {resp.status_code}: {resp.text[:120]}"}
    except Exception as exc:
        return {"ok": False, "message": str(exc)[:200]}


@app.post("/api/emby_test")
def emby_test(payload: dict = None):
    """测试 Emby 服务器地址 / API Key / 媒体库名称是否可用。"""
    data = payload or {}
    saved = get_current_config().get("emby") or {}
    url = (data.get("server_url") or saved.get("server_url") or "").strip().rstrip("/")
    key = (data.get("api_key") or saved.get("api_key") or "").strip()
    name = (data.get("library_name") or saved.get("library_name") or "").strip()
    if not url or not key:
        return {"ok": False, "message": "请先填写服务器地址与 API Key"}
    try:
        client = EmbyClient(url, key, name)
        library_id = client.library_id
    except Exception as exc:
        return {"ok": False, "message": str(exc)[:200]}
    if not library_id:
        return {"ok": False, "message": f"能连上服务器，但没找到媒体库「{name}」"}
    return {"ok": True, "message": f"已连接，媒体库 ID {library_id}"}


@app.get("/api/groups/config")
def get_groups_config():
    """分团配置中可在前端编辑的字段（api_gid / trainee / wiki 分类仍由文件维护）。"""
    config = group_config.load()
    return {
        "path": GROUPS_CONFIG_PATH,
        "groups": [
            {
                "code": group.get("code"),
                "name": group.get("name"),
                "api_gid": group.get("api_gid"),
                "theme_color": group.get("theme_color") or "#64748b",
                "bilibili_mid": group.get("bilibili_mid"),
                "enabled": bool(group.get("enabled")),
                "sync_uploads": group.get("sync_uploads", True),
            }
            for group in config.get("groups", [])
        ],
    }


@app.post("/api/groups/config")
def save_groups_config(payload: dict):
    """保存分团账号与开关：原子写 groups.yaml，写前自动备份。"""
    updates = {
        str(item.get("code")): item
        for item in (payload or {}).get("groups", [])
        if item.get("code")
    }
    if not updates:
        return {"ok": False, "message": "没有需要更新的分团"}
    if not os.path.exists(GROUPS_CONFIG_PATH):
        return {"ok": False, "message": f"配置文件不存在: {GROUPS_CONFIG_PATH}"}

    with open(GROUPS_CONFIG_PATH, encoding="utf-8") as fh:
        config = yaml.safe_load(fh) or {}

    changed = []
    for group in config.get("groups", []):
        item = updates.get(str(group.get("code")))
        if not item:
            continue
        for key in GROUPS_EDITABLE:
            if key not in item:
                continue
            value = item[key]
            if key == "bilibili_mid":
                value = int(value) if str(value).strip().isdigit() else None
            elif key in ("enabled", "sync_uploads"):
                value = bool(value)
            # 与 GET 保持同一套默认值，避免「原样回写」被误判为有变化
            default = {"sync_uploads": True, "enabled": False,
                       "theme_color": "#64748b"}.get(key)
            if group.get(key, default) != value:
                group[key] = value
                changed.append(f"{group.get('code')}.{key}")

    if not changed:
        return {"ok": True, "message": "没有变化", "changed": []}

    _backup_file(GROUPS_CONFIG_PATH)
    _atomic_write_yaml(GROUPS_CONFIG_PATH, config)
    print(f"⚙️ 分团配置已更新: {'、'.join(changed)}", flush=True)

    restart_needed = any(item.endswith(".enabled") for item in changed)
    return {
        "ok": True,
        "message": f"已更新 {len(changed)} 项",
        "changed": changed,
        "restart_calendar": restart_needed,
        "note": "改「接入」后需重启日历服务才会立即抓取排期；改「投稿抓取」在下次定时同步生效。",
    }


# ── 维护任务（阶段 5b）：把 tools/ 下的脚本做成前端按钮 ──────────────
@app.get("/api/maintenance/actions")
def maintenance_actions():
    """可用于前端的维护动作清单（含是否破坏性、支持哪些参数）。"""
    return [
        {
            "action": key,
            "label": spec["label"],
            "desc": spec["desc"],
            "destructive": spec["destructive"],
            "params": sorted(spec["params"].keys()),
        }
        for key, spec in maintenance.ACTIONS.items()
    ]


@app.post("/api/maintenance/run")
def maintenance_run(payload: dict = None):
    """启动维护任务。destructive 动作要 apply=true 才会真正写入。"""
    data = payload or {}
    action = str(data.get("action") or "")
    try:
        task_id = maintenance.start(action, data.get("params") or {},
                                    bool(data.get("apply")))
    except ValueError as exc:
        return {"ok": False, "message": str(exc)}
    except RuntimeError as exc:
        return {"ok": False, "message": str(exc)}
    task = maintenance.describe(task_id)
    return {"ok": True, "task_id": task_id, "task": task,
            "message": f"已启动：{task['label']}（{task['mode']}）"}


@app.get("/api/maintenance/tasks")
def maintenance_tasks(limit: int = 10):
    return {"busy": maintenance.is_busy(), "tasks": maintenance.recent(limit)}


@app.get("/api/maintenance/log/{task_id}")
def maintenance_log(task_id: str, tail: int = 6000):
    task = maintenance.describe(task_id)
    if not task:
        return {"ok": False, "message": "任务不存在或已过期"}
    return {"ok": True, "task": task, "log": maintenance.read_log(task_id, tail)}


@app.get("/api/history")
def get_history(group_code: str = ""):
    completed = []
    # 数据来自 SQLite（status='ok'），新→旧；返回结构保持与旧 txt 版本一致
    for row in scanner.history_store.list_completed():
        if group_code and (row.get("group_code") or "snh") != group_code:
            continue
        entry = {
            "id": row["bvid"],
            "title": row["title"] or "历史导入数据",
        }
        if row.get("codec"):
            entry["codec"] = row["codec"]
        if row.get("scope"):
            entry["scope"] = row["scope"]
        completed.append(entry)
    return {
        "completed": completed,
        "active": scanner.active_tasks
    }

@app.post("/api/download_specific")
def download_specific_video(payload: dict, background_tasks: BackgroundTasks):
    url = payload.get("url")
    if not url:
        return {"message": "未提供有效链接"}
    background_tasks.add_task(scanner.download_single, url)
    return {"message": "指令已投递，系统正在后台解析下载"}

@app.get("/api/download_queue")
def get_download_queue():
    with download_queue_lock:
        queue = list(download_queue)
    return {"queue": queue, "active": scanner.active_tasks}


@app.post("/api/download_batch")
def download_batch(payload: dict, background_tasks: BackgroundTasks):
    """批量下载 / 批量洗版：按 BV 号依次投递，顺序执行避免并发下载。

    payload:
      bvids: ["BV...", ...]
      scope: 'full' | 'mc'（不传则回退「手动入库」设置）
      force: true 表示洗版（已入库也重新下载，并清理旧范围版本）
    """
    global download_queue_seq
    items = []
    seen = set()
    for item in (payload.get("items") or []):
        match = re.match(r"^BV[0-9A-Za-z]{10}$", str(item.get("bvid", "")).strip())
        if match and match.group(0) not in seen:
            seen.add(match.group(0))
            items.append({"bvid": match.group(0), "title": str(item.get("title") or "")[:200]})
    if not items:
        for bvid in (payload.get("bvids") or []):
            match = re.match(r"^BV[0-9A-Za-z]{10}$", str(bvid).strip())
            if match and match.group(0) not in seen:
                seen.add(match.group(0))
                items.append({"bvid": match.group(0), "title": ""})
    if not items:
        return {"message": "未提供有效的 BV 号"}

    scope = payload.get("scope")
    if scope not in ("full", "mc"):
        scope = None
    force = bool(payload.get("force"))

    # 提交时整批入队，前端「排队中」才能看到后续任务
    with download_queue_lock:
        entries = []
        for item in items:
            download_queue_seq += 1
            entries.append({
                "id": download_queue_seq,
                "bvid": item["bvid"],
                "title": item["title"] or item["bvid"],
                "scope": scope or "auto",
                "force": force,
            })
        download_queue.extend(entries)

    def run_batch():
        # 串行处理：同一时间只跑一批，避免并发下载加剧风控风险
        with download_run_lock:
            for entry in entries:
                # 轮到它了：从等待列表移除（正在下载由 active_tasks 展示，避免重复出现）
                with download_queue_lock:
                    download_queue[:] = [q for q in download_queue if q["id"] != entry["id"]]
                bvid = entry["bvid"]
                if not entry["force"] and scanner._is_downloaded(bvid):
                    print(f"⏭️ 批量下载跳过已入库: {bvid}", flush=True)
                    continue
                scanner.download_single(f"https://www.bilibili.com/video/{bvid}", scope=scope)

    background_tasks.add_task(run_batch)
    label = "洗版" if force else "下载"
    return {"message": f"已投递 {len(entries)} 个{label}任务，将按顺序处理"}

@app.get("/api/emby_status")
def get_emby_status():
    """检测 Emby 连接状态"""
    config = get_current_config()
    emby_cfg = config.get("emby", {})
    server_url = emby_cfg.get("server_url", "").strip().rstrip("/")
    api_key = emby_cfg.get("api_key", "").strip()
    library_name = emby_cfg.get("library_name", "公演直播回放")

    if not server_url or not api_key:
        return {
            "configured": False,
            "status": "warning",
            "message": "Emby 未配置，请填写 server_url 和 api_key",
            "server_url": server_url,
            "library_name": library_name
        }

    headers = {"X-Emby-Token": api_key}
    result = {
        "configured": True,
        "server_url": server_url,
        "library_name": library_name,
        "status": "error",
        "message": ""
    }

    try:
        # 尝试获取 Emby 系统信息以验证 API Key 是否有效
        resp = requests.get(
            f"{server_url}/emby/System/Info",
            headers=headers,
            timeout=8
        )
        if resp.status_code == 401:
            result["status"] = "error"
            result["message"] = "API Key 无效或未授权，请在 Emby 管理后台重新生成"
        elif resp.status_code == 200:
            # API Key 有效，进一步查找目标媒体库
            try:
                lib_resp = requests.get(
                    f"{server_url}/emby/Library/VirtualFolders",
                    headers=headers,
                    timeout=8
                )
                if lib_resp.status_code == 200:
                    found = False
                    for folder in lib_resp.json():
                        if folder.get("Name") == library_name:
                            found = True
                            result["library_id"] = folder.get("ItemId")
                            break
                    if found:
                        result["status"] = "success"
                        result["message"] = f"连接成功，已找到媒体库「{library_name}」"
                    else:
                        # Fallback to Items search
                        try:
                            items_resp = requests.get(
                                f"{server_url}/emby/Items",
                                params={"IncludeItemTypes": "CollectionFolder", "Recursive": "false"},
                                headers=headers,
                                timeout=8
                            )
                            if items_resp.status_code == 200:
                                found2 = False
                                for item in items_resp.json().get("Items", []):
                                    if item.get("Name") == library_name:
                                        found2 = True
                                        result["library_id"] = item.get("Id")
                                        break
                                if found2:
                                    result["status"] = "success"
                                    result["message"] = f"连接成功，已找到媒体库「{library_name}」"
                                else:
                                    result["status"] = "warning"
                                    result["message"] = f"API Key 有效，但未找到媒体库「{library_name}」，请检查 library_name 配置"
                                    # 列出可用媒体库名供参考
                                    result["available_libraries"] = [
                                        f.get("Name", "?") for f in lib_resp.json()
                                    ]
                            else:
                                result["status"] = "warning"
                                result["message"] = f"API Key 有效，但查询媒体库列表失败 (HTTP {items_resp.status_code})"
                        except Exception:
                            result["status"] = "warning"
                            result["message"] = f"API Key 有效，但未找到媒体库「{library_name}」"
                else:
                    result["status"] = "warning"
                    result["message"] = f"API Key 有效，但获取媒体库列表失败 (HTTP {lib_resp.status_code})"
            except Exception as e:
                result["status"] = "warning"
                result["message"] = f"API Key 有效，但查询媒体库时出错: {e}"
        else:
            result["status"] = "error"
            result["message"] = f"无法连接 Emby 服务器 (HTTP {resp.status_code})"
    except requests.ConnectionError:
        result["status"] = "error"
        result["message"] = f"无法连接到 {server_url}，请检查 server_url 和网络"
    except requests.Timeout:
        result["status"] = "error"
        result["message"] = f"连接 {server_url} 超时，请检查 server_url 和网络"
    except Exception as e:
        result["status"] = "error"
        result["message"] = f"检测失败: {str(e)}"

    return result
