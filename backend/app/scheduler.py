import os
import time
import re
import yt_dlp
import yaml
import traceback
from typing import List, Dict
from nfo_builder import NfoBuilder
from downloader import BiliDownloader

class Snh48Scanner:
    def __init__(self, config_path: str, cookie_path: str, base_download_path: str):
        self.config_path = config_path
        self.cookie_path = cookie_path
        self.nfo_builder = NfoBuilder(config_path, base_download_path)
        self.downloader = BiliDownloader(cookie_path)
        self.history_file = os.path.join(os.path.dirname(config_path), "download_history.txt")
        self._ensure_history_file()
        self.active_tasks = {}

    def _ensure_history_file(self):
        if not os.path.exists(self.history_file):
            with open(self.history_file, 'w', encoding='utf-8') as f:
                f.write("")

    def _is_downloaded(self, bvid: str) -> bool:
        if not os.path.exists(self.history_file): return False
        with open(self.history_file, 'r', encoding='utf-8') as f:
            content = f.read()
            return bvid in content

    def _mark_downloaded(self, bvid: str, title: str):
        with open(self.history_file, 'a', encoding='utf-8') as f:
            f.write(f"{bvid}|{title}\n")

    def _match_team(self, title: str) -> str:
        for team_name, team_data in self.nfo_builder.roster.items():
            keywords = team_data.get("keywords", [])
            for kw in keywords:
                if kw in title or kw.replace(" ", "") in title.replace(" ", ""):
                    return team_name
        return None

    def _create_hook(self, bvid):
        def hook(d):
            if d['status'] == 'downloading':
                percent = d.get('_percent_str', '0%')
                clean_percent = re.sub(r'\x1b\[[0-9;]*m', '', percent).strip()
                if bvid in self.active_tasks:
                    self.active_tasks[bvid]['progress'] = clean_percent
                    self.active_tasks[bvid]['status'] = "视频下载中..."
            elif d['status'] == 'finished':
                if bvid in self.active_tasks:
                    self.active_tasks[bvid]['progress'] = '100%'
                    self.active_tasks[bvid]['status'] = "正在合并文件..."
        return hook

# ... 保持其他方法不变 ...

    def _process_video_info(self, bvid: str, title: str, video_info: dict, target_team: str, date_str: str, year: str, formatted_date: str):
        parts = video_info.get('entries') if 'entries' in video_info else [video_info]
        if bvid in self.active_tasks:
            self.active_tasks[bvid]['p_total'] = len(parts)

        # 🚀 提取整场公演的总简介
        overall_plot = video_info.get('description') or ''
        if not overall_plot and parts:
            overall_plot = parts[0].get('description') or ''

        # 🚀 核心改动：在循环外只调用一次，生成唯一的一个 NFO
        # 返回的 base_episode_path 类似于: /downloads/TEAM NII/Season 2026/2026-04-24 标题/S2026E0424 - 标题
        base_episode_path = self.nfo_builder.build_metadata(
            team_name=target_team, 
            year=year, 
            date_str=formatted_date,
            perf_title=title, 
            plot=overall_plot
        )

        success_count = 0
        for index, part in enumerate(parts):
            p_number = index + 1
            if bvid in self.active_tasks:
                self.active_tasks[bvid]['p_current'] = p_number

            # 🚀 核心改动：利用 Emby 的多分段命名规则，在原文件名后附加 - ptX 标识
            output_path = f"{base_episode_path} - pt{p_number}"
            
            success = self.downloader.download_part(
                bvid, p_number, output_path, 
                progress_callback=self._create_hook(bvid)
            )
            if success:
                success_count += 1

        if success_count > 0:
            self._mark_downloaded(bvid, title)
            if bvid in self.active_tasks:
                del self.active_tasks[bvid]
        else:
            if bvid in self.active_tasks:
                self.active_tasks[bvid]['status'] = "下载失败"

# ... 保持其他方法不变 ...

    def scan_and_download(self, space_url=""):
        if "/video" not in space_url:
            space_url = space_url.rstrip('/') + "/video"
            
        print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] 巡视开始: {space_url}")
        
        # 🚀 增加模拟浏览器的 HTTP Headers，突破 B站风控拦截
        ydl_opts_flat = {
            'cookiefile': self.cookie_path, 
            'extract_flat': True, 
            'playlistend': 10, 
            'quiet': True,
            'http_headers': {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                'Referer': 'https://www.bilibili.com/'
            }
        }

        try:
            with yt_dlp.YoutubeDL(ydl_opts_flat) as ydl:
                info = ydl.extract_info(space_url, download=False)
                entries = info.get('entries', [])
        except Exception as e:
            print(f"❌ 列表扫描失败: {e}")
            return

        for entry in entries:
            bvid = entry.get('id')
            if not bvid or self._is_downloaded(bvid): continue

            print(f"🔎 发现新投稿 {bvid}，获取标题中...")
            ydl_opts_full = {
                'cookiefile': self.cookie_path, 
                'quiet': True,
                'http_headers': {
                    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                    'Referer': 'https://www.bilibili.com/'
                }
            }
            with yt_dlp.YoutubeDL(ydl_opts_full) as ydl_full:
                try:
                    v_info = ydl_full.extract_info(f"https://www.bilibili.com/video/{bvid}", download=False)
                    title = v_info.get('title', '')
                    target_team = self._match_team(title)
                    date_match = re.search(r'202\d{5}', title)
                    
                    if target_team and date_match:
                        date_str = date_match.group()
                        year = date_str[:4]
                        formatted_date = f"{year}-{date_str[4:6]}-{date_str[6:8]}"
                        
                        self.active_tasks[bvid] = {
                            "title": title, "status": "准备下载...", "progress": "0%", "p_current": 0, "p_total": "?"
                        }
                        self._process_video_info(bvid, title, v_info, target_team, date_str, year, formatted_date)
                    else:
                        print(f"⏭️ 忽略不匹配内容: {title}")
                except Exception as e:
                    print(f"❌ 解析详情失败 {bvid}: {e}")
                    traceback.print_exc()

    def download_single(self, video_url: str):
        bvid_match = re.search(r'(BV\w+)', video_url)
        if not bvid_match: return
        bvid = bvid_match.group(1)

        self.active_tasks[bvid] = {"title": bvid, "status": "手动解析中...", "progress": "0%", "p_current": 0, "p_total": "?"}
        
        # 🚀 增加 Headers 防止被拦截
        ydl_opts_full = {
            'cookiefile': self.cookie_path, 
            'quiet': True,
            'http_headers': {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                'Referer': 'https://www.bilibili.com/'
            }
        }
        with yt_dlp.YoutubeDL(ydl_opts_full) as ydl_full:
            try:
                v_info = ydl_full.extract_info(f"https://www.bilibili.com/video/{bvid}", download=False)
                title = v_info.get('title', '')
                self.active_tasks[bvid]['title'] = title
                target_team = self._match_team(title)
                date_match = re.search(r'202\d{5}', title)
                if target_team and date_match:
                    date_str = date_match.group()
                    self._process_video_info(bvid, title, v_info, target_team, date_str, date_str[:4], f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:8]}")
                else:
                    self.active_tasks[bvid]['status'] = "不符合入库规则"
            except Exception as e:
                # 🚀 打印出极度详尽的崩溃日志，如果还失败，我们立刻能知道是谁干的！
                print(f"❌ 手动解析视频彻底崩溃 {bvid}: {e}")
                traceback.print_exc()
                self.active_tasks[bvid]['status'] = "解析失败"

    def scan_all(self):
        with open(self.config_path, 'r', encoding='utf-8') as f:
            data = yaml.safe_load(f)
        for sub in data.get('subscriptions', []):
            self.scan_and_download(sub.get('url'))