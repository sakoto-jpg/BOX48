import os
import time
import yt_dlp
from typing import List, Dict
from nfo_builder import NfoBuilder
from downloader import BiliDownloader

class Snh48Scanner:
    def __init__(self, config_path: str, cookie_path: str, base_download_path: str):
        self.config_path = config_path
        self.cookie_path = cookie_path
        # 初始化组件
        self.nfo_builder = NfoBuilder(config_path, base_download_path)
        self.downloader = BiliDownloader(cookie_path)
        
        # 历史记录文件路径，存放在宿主机挂载的 config 目录，保证重启不丢失
        self.history_file = os.path.join(os.path.dirname(config_path), "download_history.txt")
        self._ensure_history_file()

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
        """根据标题匹配花名册中的队伍"""
        for team_name, team_data in self.nfo_builder.roster.items():
            keywords = team_data.get("keywords", [])
            for kw in keywords:
                # 忽略空格匹配，例如把 "TEAM NII" 和 "TEAMNII" 都算作匹配成功
                if kw in title or kw.replace(" ", "") in title.replace(" ", ""):
                    return team_name
        return None

    def scan_and_download(self, space_url="https://space.bilibili.com/387439/video"):
        """主执行逻辑：扫描并处理"""
        print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] 开始巡视 B 站官方账号...")
        
        # 第一阶段：快速拉取前 10 个视频的元数据 (不下载视频内容)
        ydl_opts_flat = {
            'cookiefile': self.cookie_path,
            'extract_flat': True,  # 仅提取列表，不深入解析，速度极快
            'playlistend': 10,     # 每次只检查最新的 10 个视频
            'quiet': True
        }

        try:
            with yt_dlp.YoutubeDL(ydl_opts_flat) as ydl:
                info = ydl.extract_info(space_url, download=False)
                entries = info.get('entries', [])
        except Exception as e:
            print(f"❌ 扫描列表失败: {e}")
            return

        for entry in entries:
            bvid = entry.get('id')
            title = entry.get('title')
            
            # 1. 检查是否已经下载过
            if self._is_downloaded(bvid):
                continue
                
            # 2. 检查标题是否包含我们的目标队伍
            target_team = self._match_team(title)
            if not target_team:
                continue
                
            # 3. 提取日期 (这里做一个极简假设：标题结尾通常是 8 位数字日期)
            # 实际情况中你可能需要写个正则 import re; re.search(r'\d{8}', title)
            import re
            date_match = re.search(r'202\d{5}', title)
            if not date_match:
                print(f"⚠️ 无法从标题提取日期，跳过: {title}")
                continue
            date_str = date_match.group() # 形如 20260423
            year = date_str[:4]
            formatted_date = f"{year}-{date_str[4:6]}-{date_str[6:8]}"

            print(f"🎯 发现目标公演: {title} ({bvid})")
            
            # 第二阶段：对匹配中的特定 BV 号进行深度提取，获取分 P 信息
            ydl_opts_full = {'cookiefile': self.cookie_path, 'quiet': True}
            with yt_dlp.YoutubeDL(ydl_opts_full) as ydl_full:
                try:
                    video_info = ydl_full.extract_info(f"https://www.bilibili.com/video/{bvid}", download=False)
                except Exception as e:
                    print(f"❌ 提取详细信息失败 {bvid}: {e}")
                    continue
                
                # B 站多 P 视频在 yt-dlp 中会被解析为一个包含多个 entry 的播放列表
                parts = video_info.get('entries') if 'entries' in video_info else [video_info]
                
                success_count = 0
                for index, part in enumerate(parts):
                    p_number = index + 1
                    part_title = part.get('title')
                    plot = part.get('description', '')

                    # 4. 生成 NFO 并获取预期路径
                    output_path = self.nfo_builder.build_metadata(
                        team_name=target_team,
                        year=year,
                        date_str=formatted_date,
                        p_number=str(p_number),
                        part_title=part_title,
                        plot=plot
                    )
                    
                    # 5. 调用下载器开始下载
                    success = self.downloader.download_part(bvid, p_number, output_path)
                    if success:
                        success_count += 1
                
                # 如果所有 P 都处理完成，记录到历史文件
                if success_count > 0:
                    self._mark_downloaded(bvid)
                    print(f"🎉 公演入库完成: {title}")
    def scan_all(self):
        """遍历配置文件中的所有订阅源进行扫描"""
        import yaml
        if not os.path.exists(self.config_path):
            print("未找到配置文件")
            return
            
        with open(self.config_path, 'r', encoding='utf-8') as f:
            data = yaml.safe_load(f)
            
        subs = data.get('subscriptions', [])
        for sub in subs:
            space_url = sub.get('url')
            if space_url:
                print(f"正在扫描订阅源: {sub.get('name', '未知')} ({space_url})")
                self.scan_and_download(space_url)