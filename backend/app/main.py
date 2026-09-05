import os
import yaml
import requests
import datetime
from fastapi import FastAPI, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from apscheduler.schedulers.background import BackgroundScheduler
from scheduler import Snh48Scanner

app = FastAPI(title="SNH48 Downloader API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

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
            for team in config.get("teams", []):
                # 🚀 排期提醒与"录播自动入库"解耦：只由队伍的通知开关(notify)控制
                if team.get("notify", False):
                    for kw in team.get("keywords", []):
                        clean_kw = kw.replace("《", "").replace("》", "")
                        if clean_kw in title:
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

@app.post("/api/config")
def save_config(config: dict):
    with open(CONFIG_PATH, 'w', encoding='utf-8') as f:
        yaml.dump(config, f, allow_unicode=True, sort_keys=False)
    scanner.nfo_builder.roster = scanner.nfo_builder._load_roster(CONFIG_PATH)
    scanner.reload_emby_client()
    reschedule_scan_job()
    return {"message": "配置已保存并生效"}

@app.get("/api/history")
def get_history():
    history_file = scanner.history_file
    completed = []
    if os.path.exists(history_file):
        with open(history_file, 'r', encoding='utf-8') as f:
            lines = f.readlines()
            seen = set()
            # 新行格式: bvid|title|codec|path；旧行为 2 字段或裸 bvid。
            # 倒序(新→旧)遍历并按 bvid 去重：同一场多次入库(手动补档/洗版)只显示最新一条。
            for line in reversed(lines):
                line = line.strip()
                if not line:
                    continue
                if '|' in line:
                    parts = line.split('|', 3)
                    bvid = parts[0].strip()
                    if bvid in seen:
                        continue
                    seen.add(bvid)
                    entry = {
                        "id": bvid,
                        "title": parts[1].strip() if len(parts) > 1 else "历史导入数据",
                    }
                    if len(parts) > 2 and parts[2].strip():
                        entry["codec"] = parts[2].strip()
                    completed.append(entry)
                else:
                    if line in seen:
                        continue
                    seen.add(line)
                    completed.append({"id": line, "title": "历史导入数据"})
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