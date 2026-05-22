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
# 🚀 新增：口袋48开播推送记录本，防止重复轰炸
POCKET48_NOTIFIED_FILE = "/config/pocket48_notified.txt"

scanner = Snh48Scanner(CONFIG_PATH, COOKIE_PATH, DOWNLOAD_PATH)
scheduler = BackgroundScheduler()

def get_current_config():
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
            return yaml.safe_load(f) or {}
    return {}

def send_apprise_msg(title: str, body: str):
    config = get_current_config()
    apprise_url = config.get("apprise_url", "")
    if not apprise_url:
        return
    try:
        payload = {"title": title, "body": body, "format": "text"}
        requests.post(apprise_url, json=payload, timeout=5)
        print(f"📱 微信推送成功: {title}")
    except Exception as e:
        print(f"❌ Apprise 推送失败: {e}")

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

        # 🚀 拦截 1：如果现在的时间已经超过了开演时间，说明公演已结束或正在进行，直接忽略！
        if now >= show_dt:
            continue

        should_notify = False
        if show_id in config.get("manual_subs", []):
            should_notify = True
        else:
            for team in config.get("teams", []):
                if team.get("enabled") and team.get("notify", False):
                    for kw in team.get("keywords", []):
                        clean_kw = kw.replace("《", "").replace("》", "")
                        if clean_kw in title:
                            should_notify = True
                            break
                if should_notify: break
                
        if not should_notify: continue

        # ⏳ 拦截 2：精确定义触发窗口，逾期作废
        day_before_22 = (show_dt - datetime.timedelta(days=1)).replace(hour=22, minute=0, second=0, microsecond=0)
        # 有效期：前一天 22:00 开始，到开演前 2 小时结束
        is_valid_day_before = day_before_22 <= now < (show_dt - datetime.timedelta(hours=2))
        
        min_30_before = show_dt - datetime.timedelta(minutes=30)
        # 有效期：开场前 30 分钟开始，到开演结束
        is_valid_30min = min_30_before <= now < show_dt
        
        if is_valid_day_before and f"{show_id}_daybefore" not in notified:
            # 智能文案：如果是第二天早上才补发的提醒，自动把“明日”改成“今日”
            day_prefix = "明日" if now.date() < show_dt.date() else "今日"
            send_apprise_msg(f"🔔 {day_prefix}公演提醒", f"【{title}】\n📅 时间：{date_str} {time_str}\n🔗 票务：{show.get('link')}")
            notified.add(f"{show_id}_daybefore")
            new_notified.append(f"{show_id}_daybefore")
            
        if is_valid_30min and f"{show_id}_30min" not in notified:
            send_apprise_msg("🚨 演出即将开始！", f"【{title}】\n📅 时间：今天 {time_str} (30分钟后开场)\n🔗 票务：{show.get('link')}")
            notified.add(f"{show_id}_30min")
            new_notified.append(f"{show_id}_30min")

    if new_notified:
        with open(NOTIFIED_FILE, 'a') as f:
            for n in new_notified:
                f.write(f"{n}\n")

# ... 原有的 check_reminders() 函数 ...

def check_pocket48_live():
    """定时巡视口袋48，监控特别关注（oshis）是否开播"""
    config = get_current_config()
    apprise_url = config.get("apprise_url", "")
    if not apprise_url: return

    # 1. 收集所有的 oshis (特别关注)
    all_oshis = set()
    for team in config.get("teams", []):
        if team.get("enabled"):
            for oshi in team.get("oshis", []):
                all_oshis.add(oshi)
                
    if not all_oshis: return

    # 2. 请求口袋48获取当前直播列表
    url = "https://pocketapi.48.cn/live/api/v1/live/getLiveList"
    headers = {
        "Content-Type": "application/json;charset=utf-8",
        # 伪装成口袋48官方 App 的 Header
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

    # 3. 记录已推送过的直播间
    if not os.path.exists(POCKET48_NOTIFIED_FILE):
        open(POCKET48_NOTIFIED_FILE, 'w').close()
    with open(POCKET48_NOTIFIED_FILE, 'r') as f:
        notified = set(f.read().splitlines())

    new_notified = []

    for live in live_list:
        live_id = str(live.get("liveId"))
        title = live.get("title", "直播间")
        
        # 🚀 提取信息：同时拿到带前缀的昵称和纯净的艺名
        user_info = live.get("userInfo", {})
        nickname = user_info.get("nickname", "")
        star_name = user_info.get("starName", "")
        
        # 口袋48类型：1是视频直播，2是电台直播
        live_type = live.get("liveType", 1) 
        type_str = "📺 视频直播" if live_type == 1 else "📻 电台语音"

        # 🚀 核心修复：精准匹配 starName，或者模糊匹配 nickname
        is_oshi = (star_name in all_oshis) or any(oshi in nickname for oshi in all_oshis)

        # 如果匹配成功，且还没推送过
        if is_oshi and live_id not in notified:
            # 优先使用纯净的 star_name 进行推送展示
            display_name = star_name if star_name else nickname
            send_apprise_msg(
                f"🌟 你的特别关注【{display_name}】开播啦！",
                f"标题：{title}\n类型：{type_str}\n快去口袋48APP围观吧！"
            )
            notified.add(live_id)
            new_notified.append(live_id)

    # 保存新的推送记录
    if new_notified:
        with open(POCKET48_NOTIFIED_FILE, 'a') as f:
            for n in new_notified:
                f.write(f"{n}\n")

@app.on_event("startup")
def start_scheduler():
    scheduler.add_job(scanner.scan_all, 'cron', hour='13,23')
    scheduler.add_job(check_reminders, 'interval', minutes=5)
    # 🚀 新增：每 5 分钟巡视一次口袋48，保证开播提醒足够及时
    scheduler.add_job(check_pocket48_live, 'interval', minutes=5)
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
    background_tasks.add_task(scanner.scan_all)
    return {"message": "已在后台触发全局扫描任务"}

@app.get("/api/config")
def get_config():
    return get_current_config()

@app.post("/api/config")
def save_config(config: dict):
    with open(CONFIG_PATH, 'w', encoding='utf-8') as f:
        yaml.dump(config, f, allow_unicode=True, sort_keys=False)
    scanner.nfo_builder.roster = scanner.nfo_builder._load_roster(CONFIG_PATH)
    return {"message": "配置已保存并生效"}

@app.get("/api/history")
def get_history():
    history_file = scanner.history_file
    completed = []
    if os.path.exists(history_file):
        with open(history_file, 'r', encoding='utf-8') as f:
            lines = f.readlines()
            for line in lines:
                if '|' in line:
                    bvid, title = line.strip().split('|', 1)
                    completed.append({"id": bvid, "title": title})
                else:
                    completed.append({"id": line.strip(), "title": "历史导入数据"})
    return {
        "completed": completed[::-1],
        "active": scanner.active_tasks 
    }

@app.post("/api/download_specific")
def download_specific_video(payload: dict, background_tasks: BackgroundTasks):
    url = payload.get("url")
    if not url:
        return {"message": "未提供有效链接"}
    background_tasks.add_task(scanner.download_single, url)
    return {"message": "指令已投递，系统正在后台解析下载"}