import os
import yaml
from fastapi import FastAPI, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from apscheduler.schedulers.background import BackgroundScheduler
from scheduler import Snh48Scanner

app = FastAPI(title="SNH48 Downloader API")

# 允许跨域请求 (关键！允许前端 8090 端口访问后端 8095)
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

scanner = Snh48Scanner(CONFIG_PATH, COOKIE_PATH, DOWNLOAD_PATH)
scheduler = BackgroundScheduler()

@app.on_event("startup")
def start_scheduler():
    # 改为调用 scanner.scan_all
    scheduler.add_job(scanner.scan_all, 'cron', hour='13,23')
    scheduler.start()
    print("✅ 定时任务已启动")

@app.on_event("shutdown")
def stop_scheduler():
    scheduler.shutdown()

@app.get("/")
def read_root():
    return {"status": "running", "message": "SNH48 自动下载服务正在运行"}

@app.post("/api/trigger_scan")
def trigger_scan_manually(background_tasks: BackgroundTasks):
    background_tasks.add_task(scanner.scan_all)
    return {"message": "已在后台触发全局扫描任务"}

# ---------- 新增供前端调用的 API ----------

@app.get("/api/config")
def get_config():
    """获取花名册与订阅配置"""
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
            return yaml.safe_load(f) or {}
    return {"subscriptions": [], "teams": []}

@app.post("/api/config")
def save_config(config: dict):
    """保存前端修改的配置并热重载"""
    with open(CONFIG_PATH, 'w', encoding='utf-8') as f:
        yaml.dump(config, f, allow_unicode=True, sort_keys=False)
    # 热重载后端的内存字典
    scanner.nfo_builder.roster = scanner.nfo_builder._load_roster(CONFIG_PATH)
    return {"message": "配置已保存并生效"}

@app.get("/api/history")
def get_history():
    """获取已下载的 BV 号历史记录"""
    history_file = scanner.history_file
    if os.path.exists(history_file):
        with open(history_file, 'r') as f:
            # 倒序排列，最新的在最上面
            lines = [line.strip() for line in f.readlines() if line.strip()]
            return {"history": lines[::-1]}
    return {"history": []}