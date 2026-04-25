import os
import yt_dlp

class BiliDownloader:
    def __init__(self, cookie_path: str):
        self.cookie_path = cookie_path
        if not os.path.exists(self.cookie_path):
            print(f"警告: 未找到 Cookie 文件: {self.cookie_path}，下载高画质可能受限。")

    def download_part(self, bvid: str, p_number: int, output_path_without_ext: str, progress_callback=None) -> bool:
        ydl_opts = {
            'cookiefile': self.cookie_path,
            'outtmpl': f'{output_path_without_ext}.%(ext)s',
            'format': 'bestvideo+bestaudio/best',
            'merge_output_format': 'mp4',
            
            # 🚀 抛弃 Aria2，回归 yt-dlp 原生的 DASH 切片多线程并发（这就是真正的 IDM 模式）
            'concurrent_fragment_downloads': 10, # 开 10 个线程切片并发
            'socket_timeout': 15,                # 增加 15 秒超时防假死
            
            'writesubtitles': True,
            'subtitleslangs': ['all'],
            'playlist_items': str(p_number),
            'quiet': True,
            'no_warnings': True,
        }

        # 挂载前端进度条的回调钩子
        if progress_callback:
            ydl_opts['progress_hooks'] = [progress_callback]

        target_url = f"https://www.bilibili.com/video/{bvid}"
        print(f"开始并发切片下载: {bvid} - P{p_number} -> {output_path_without_ext}")
        
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                error_code = ydl.download([target_url])
                
                # 💡 物理文件兜底校验
                if os.path.exists(f"{output_path_without_ext}.mp4"):
                    return True
                return error_code == 0
                
        except Exception as e:
            # 即使发生异常抛错，只要视频文件已存在，依然算作成功
            if os.path.exists(f"{output_path_without_ext}.mp4"):
                return True
            print(f"❌ yt-dlp 发生致命异常: {e}")
            return False