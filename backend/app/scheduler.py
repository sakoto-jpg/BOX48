import os
import time
import re
import yt_dlp
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
            with open(self.history_file, 'w') as f:
                f.write("")

    def _is_downloaded(self, bvid: str) -> bool:
        with open(self.history_file, 'r') as f:
            return bvid in f.read()

    def _mark_downloaded(self, bvid: str):
        with open(self.history_file, 'a') as f:
            f.write(f"{bvid}\n")

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
                    self.active_tasks[bvid]['status'] = "视频分片下载中..."
            elif d['status'] == 'finished':
                if bvid in self.active_tasks:
                    self.active_tasks[bvid]['progress'] = '100%'
                    self.active_tasks[bvid]['status'] = "合并音视频流..."
        return hook

    def _process_video_info(self, bvid: str, title: str, video_info: dict, target_team: str, date_str: str, year: str, formatted_date: str):
        parts = video_info.get('entries') if 'entries' in video_info else [video_info]
        if bvid in self.active_tasks:
            self.active_tasks[bvid]['p_total'] = len(parts)

        success_count = 0
        for index, part in enumerate(parts):
            p_number = index + 1
            if bvid in self.active_tasks:
                self.active_tasks[bvid]['p_current'] = p_number

            part_title = part.get('title')
            plot = part.get('description', '')

            # 这里使用了 B 站返回的真实标题 (perf_title=title) 去建立文件夹
            output_path = self.nfo_builder.build_metadata(
                team_name=target_team, year=year, date_str=formatted_date,
                p_number=str(p_number), part_title=part_title, plot=plot,
                perf_title=title
            )
            
            success = self.downloader.download_part(bvid, p_number, output_path, progress_callback=self._create_hook(bvid))
            if success:
                success_count += 1

        # 核心：只有真正下载成功了，才把它写进历史记录！
        if success_count > 0:
            self._mark_downloaded(bvid)
            print(f"🎉 公演入库完成: {title}")
            if bvid in self.active_tasks:
                del self.active_tasks[bvid]
        else:
            if bvid in self.active_tasks:
                self.active_tasks[bvid]['status'] = "下载失败/中断"
                self.active_tasks[bvid]['progress'] = "ERROR"

    def scan_and_download(self, space_url="https://space.bilibili.com/2832224/video"):
        if not space_url.endswith("/video"):
            space_match = re.search(r'space\.bilibili\.com/(\d+)', space_url)
            if space_match:
                space_url = f"https://space.bilibili.com/{space_match.group(1)}/video"
                
        print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] 开始巡视 B 站账号: {space_url}")
        
        # 1. 快速拿到最近的 10 个投稿信息 (此时拿不到标题也没关系)
        ydl_opts_flat = {'cookiefile': self.cookie_path, 'extract_flat': True, 'playlistend': 10, 'quiet': True}

        try:
            with yt_dlp.YoutubeDL(ydl_opts_flat) as ydl:
                info = ydl.extract_info(space_url, download=False)
                entries = list(info.get('entries', []))
        except Exception as e:
            print(f"❌ 扫描主页列表失败: {e}")
            return

        for entry in entries:
            # 2. 提取 BV 号
            bvid = entry.get('id', '')
            if not str(bvid).startswith('BV'):
                url = entry.get('url', '')
                bv_match = re.search(r'(BV\w+)', url)
                if bv_match:
                    bvid = bv_match.group(1)

            if not bvid: continue
            
            # 3. 过滤掉已经成功入库的视频
            if self._is_downloaded(bvid): continue

            # 4. 🚀 对于新视频，直接“模拟手动下载”去获取真实的详细信息！
            print(f"⏳ 发现新视频 {bvid}，正在获取详细元数据...")
            ydl_opts_full = {'cookiefile': self.cookie_path, 'quiet': True}
            
            with yt_dlp.YoutubeDL(ydl_opts_full) as ydl_full:
                try:
                    video_info = ydl_full.extract_info(f"https://www.bilibili.com/video/{bvid}", download=False)
                except Exception as e:
                    print(f"❌ 解析新视频失败 {bvid}: {e}")
                    continue
                    
            # 5. 拿到真实的标题，进行严格判定
            title = video_info.get('title', '')
            if not title: continue
            
            target_team = self._match_team(title)
            date_match = re.search(r'202\d{5}', title)
            
            # 🚀 如果不是目标队伍或者不是公演，直接跳过！不写历史记录！
            if not target_team or not date_match: 
                print(f"⏭️ [忽略] {title}")
                continue
            
            # 6. 一切条件符合，投入下载池
            date_str = date_match.group()
            year = date_str[:4]
            formatted_date = f"{year}-{date_str[4:6]}-{date_str[6:8]}"

            print(f"🎯 命中目标公演，准备下载: {title} ({bvid})")
            self.active_tasks[bvid] = {
                "title": title, "status": "解析视频分P...", "progress": "0%", "p_current": 0, "p_total": "?"
            }

            self._process_video_info(bvid, title, video_info, target_team, date_str, year, formatted_date)

    def download_single(self, video_url: str):
        # 手动下载的逻辑保持不变，因为它是绝对精准的
        print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] 手动触发下载: {video_url}")
        bvid_match = re.search(r'(BV\w+)', video_url)
        if not bvid_match: return
        bvid = bvid_match.group(1)

        self.active_tasks[bvid] = {
            "title": f"手动解析: {bvid}", "status": "获取网络请求...", "progress": "0%", "p_current": 0, "p_total": "?"
        }

        ydl_opts_full = {'cookiefile': self.cookie_path, 'quiet': True}
        with yt_dlp.YoutubeDL(ydl_opts_full) as ydl_full:
            try:
                video_info = ydl_full.extract_info(f"https://www.bilibili.com/video/{bvid}", download=False)
            except Exception as e:
                self.active_tasks[bvid]['status'] = "解析源视频失败"
                return

            title = video_info.get('title', '')
            self.active_tasks[bvid]['title'] = title
            
            target_team = self._match_team(title)
            date_match = re.search(r'202\d{5}', title)
            
            if not target_team or not date_match:
                self.active_tasks[bvid]['status'] = "标题不符合入库规则"
                return
            
            date_str = date_match.group()
            year = date_str[:4]
            formatted_date = f"{year}-{date_str[4:6]}-{date_str[6:8]}"
            
            self._process_video_info(bvid, title, video_info, target_team, date_str, year, formatted_date)

    def scan_all(self):
        import yaml
        if not os.path.exists(self.config_path): return
        with open(self.config_path, 'r', encoding='utf-8') as f:
            data = yaml.safe_load(f)
        for sub in data.get('subscriptions', []):
            if sub.get('url'):
                self.scan_and_download(sub.get('url'))