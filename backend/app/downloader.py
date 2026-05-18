import os
import time
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
            'merge_output_format': 'mkv',
            'concurrent_fragment_downloads': 10,
            'socket_timeout': 15,
            'writesubtitles': True,
            'subtitleslangs': ['all'],
            'playlist_items': str(p_number),
            'quiet': True,
            'no_warnings': True,
            
            # 🚀 第一重保险：让 yt-dlp 遇到 HTTP 或解析错误时，自身先重试 3 次
            'extractor_retries': 3,
            'retries': 5,
        }

        if progress_callback:
            ydl_opts['progress_hooks'] = [progress_callback]

        target_url = f"https://www.bilibili.com/video/{bvid}"
        max_retries = 5  # 🚀 第二重保险：外部大循环重试 5 次
        
        for attempt in range(1, max_retries + 1):
            print(f"▶ 开始并发切片下载: {bvid} - P{p_number} -> {output_path_without_ext} (尝试 {attempt}/{max_retries})")
            
            try:
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    error_code = ydl.download([target_url])
                    
                    # 只要物理文件成功生成了，我们就认为它下载成功了
                    if os.path.exists(f"{output_path_without_ext}.mkv"):
                        return True
                    
                    if error_code == 0:
                        return True
                        
            except Exception as e:
                # 即使抛出异常，如果文件已经合成，也算成功
                if os.path.exists(f"{output_path_without_ext}.mkv"):
                    return True
                print(f"⚠️ yt-dlp 第 {attempt} 次解析被 B 站拦截或发生异常: {e}")
            
            # 如果不是最后一次尝试，则进行冷却休眠后重试
            if attempt < max_retries:
                print(f"⏳ 触发防风控冷却，等待 8 秒后进行下一次重试...")
                time.sleep(8)
                
        print(f"❌ yt-dlp 已耗尽 {max_retries} 次重试机会，当前分P下载彻底失败。")
        return False