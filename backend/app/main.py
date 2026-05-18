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

scanner = Snh48Scanner(CONFIG_PATH, COOKIE_PATH, DOWNLOAD_PATH)
scheduler = BackgroundScheduler()

def get_current_config():
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
            return yaml.safe_load(f) or {}
    return {}

def send_apprise_msg(title: str, body: str):
    """向 Apprise API 发送推送请求"""
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
    """定时巡视排期日历，触发订阅提醒"""
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
        date_str = show.get("date") # 例如 "5月24日"
        time_str = show.get("show_time") # 例如 "18:30"
        
        try:
            month = int(date_str.split('月')[0])
            day = int(date_str.split('月')[1].replace('日', ''))
            hour, minute = map(int, time_str.split(':'))
            show_dt = datetime.datetime(now.year, month, day, hour, minute)
            
            # 跨年处理 (12月获取1月排期)
            if show_dt < now - datetime.timedelta(days=30):
                show_dt = show_dt.replace(year=now.year + 1)
        except: continue

        # 🎯 判定是否需要提醒：手动订阅了？ 或者 命中了开启了提醒的队伍关键词？
        should_notify = False
        if show_id in config.get("manual_subs", []):
            should_notify = True
        else:
            for team in config.get("teams", []):
                if team.get("enabled") and team.get("notify", False):
                    for kw in team.get("keywords", []):
                        # 清洗特征词，兼容 B站格式 和 票务日历格式
                        clean_kw = kw.replace("《", "").replace("》", "")
                        if clean_kw in title:
                            should_notify = True
                            break
                if should_notify: break
                
        if not should_notify: continue

        # ⏳ 时间节点 1：前一天 22:00 规划日程提醒
        day_before_22 = (show_dt - datetime.timedelta(days=1)).replace(hour=22, minute=0, second=0, microsecond=0)
        # ⏳ 时间节点 2：开演前 30 分钟就绪提醒
        min_30_before = show_dt - datetime.timedelta(minutes=30)
        
        # 触发判断
        if now >= day_before_22 and f"{show_id}_daybefore" not in notified:
            send_apprise_msg("🔔 明日公演提醒", f"【{title}】\n📅 时间：明天 {time_str}\n🔗 票务：{show.get('link')}")
            notified.add(f"{show_id}_daybefore")
            new_notified.append(f"{show_id}_daybefore")
            
        if now >= min_30_before and f"{show_id}_30min" not in notified:
            send_apprise_msg("🚨 演出即将开始！", f"【{title}】\n📅 时间：今天 {time_str} (30分钟后开场)\n🔗 票务：{show.get('link')}")
            notified.add(f"{show_id}_30min")
            new_notified.append(f"{show_id}_30min")

    if new_notified:
        with open(NOTIFIED_FILE, 'a') as f:
            for n in new_notified:
                f.write(f"{n}\n")

@app.on_event("startup")
def start_scheduler():
    scheduler.add_job(scanner.scan_all, 'cron', hour='13,23')
    # 每 5 分钟巡视一次排期表，看有没有到达触发时间的公演
    scheduler.add_job(check_reminders, 'interval', minutes=5)
    scheduler.start()
    print("✅ 定时任务及通知引擎已启动")

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