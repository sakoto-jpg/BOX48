import glob
import os
import subprocess
import time
import yt_dlp


class BiliDownloader:
    def __init__(self, cookie_path: str):
        self.cookie_path = cookie_path
        if not os.path.exists(self.cookie_path):
            print(f"警告: 未找到 Cookie 文件: {self.cookie_path}，下载高画质可能受限。")

    def _get_video_start_time(self, video_path: str) -> float:
        try:
            result = subprocess.run([
                'ffprobe', '-v', 'quiet',
                '-show_entries', 'format=start_time',
                '-of', 'csv=p=0',
                video_path
            ], capture_output=True, text=True, timeout=30)
            return float(result.stdout.strip())
        except Exception:
            return 0.0

    def _merge_with_sync_fix(self, video_path: str, audio_path: str,
                             output_path: str, video_start: float) -> bool:
        temp_path = output_path + '.temp.mkv'
        try:
            if video_start > 0.1:
                cmd = [
                    'ffmpeg', '-y', '-loglevel', 'error',
                    '-i', video_path,
                    '-ss', str(video_start), '-i', audio_path,
                    '-c', 'copy',
                    '-map', '0:v:0', '-map', '1:a:0',
                    temp_path
                ]
            else:
                cmd = [
                    'ffmpeg', '-y', '-loglevel', 'error',
                    '-i', video_path,
                    '-i', audio_path,
                    '-c', 'copy',
                    '-map', '0:v:0', '-map', '1:a:0',
                    temp_path
                ]

            result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
            if result.returncode == 0 and os.path.exists(temp_path):
                os.replace(temp_path, output_path)
                return True
            else:
                if os.path.exists(temp_path):
                    os.remove(temp_path)
                return False
        except Exception:
            if os.path.exists(temp_path):
                os.remove(temp_path)
            return False

    def _find_intermediate(self, base_path: str, extensions: list) -> str | None:
        for ext in extensions:
            pattern = f'{base_path}.f*{ext}'
            candidates = sorted(glob.glob(pattern))
            if candidates:
                return candidates[0]
        for ext in extensions:
            pattern = f'{base_path}{ext}'
            candidates = sorted(glob.glob(pattern))
            if candidates:
                return candidates[0]
        return None

    def download_part(self, bvid: str, p_number: int,
                      output_path_without_ext: str, progress_callback=None) -> tuple[bool, str]:
        """下载单个分P。

        Returns:
            (success: bool, part_title: str) — 标题来自 yt-dlp 下载时获取的真实分P标题
        """
        ydl_opts = {
            'cookiefile': self.cookie_path,
            'outtmpl': f'{output_path_without_ext}.%(ext)s',
            'format': 'bestvideo[vcodec^=avc]+bestaudio/bestvideo+bestaudio/best',
            'merge_output_format': 'mkv',
            'concurrent_fragment_downloads': 10,
            'socket_timeout': 15,
            'writesubtitles': True,
            'subtitleslangs': ['all'],
            'playlist_items': str(p_number),
            'quiet': True,
            'no_warnings': True,
            'fixup': 'force',
            'keepvideo': True,

            'extractor_retries': 3,
            'retries': 5,
        }

        if progress_callback:
            ydl_opts['progress_hooks'] = [progress_callback]

        target_url = f"https://www.bilibili.com/video/{bvid}"
        max_retries = 5
        mkv_file = f"{output_path_without_ext}.mkv"
        fallback_title = f"P{p_number}"

        for attempt in range(1, max_retries + 1):
            print(f"▶ 开始并发切片下载: {bvid} - P{p_number} -> {output_path_without_ext} (尝试 {attempt}/{max_retries})")

            # 捕获真实分P标题
            part_title = fallback_title
            try:
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    info = ydl.extract_info(target_url, download=True)
                    if info:
                        # anthology 合集视频：标题在 entries[0].title 而非顶层 title
                        entries = info.get('entries', [])
                        if entries and entries[0]:
                            part_title = entries[0].get('title', fallback_title)
                        else:
                            part_title = info.get('title', fallback_title)
            except Exception as e:
                print(f"⚠️ yt-dlp 第 {attempt} 次异常: {e}")

            if not os.path.exists(mkv_file):
                if attempt < max_retries:
                    print(f"⏳ 文件未生成，等待 8 秒后重试...")
                    time.sleep(8)
                continue

            # --- 音视频同步修正 ---
            video_raw = self._find_intermediate(output_path_without_ext, ['.mp4', '.m4s'])
            audio_raw = self._find_intermediate(output_path_without_ext, ['.m4a'])

            if video_raw and audio_raw:
                video_start = self._get_video_start_time(video_raw)
                if video_start > 0.1:
                    print(f"  🔧 检测到视频首帧偏移 {video_start:.3f}s，修正音轨同步...")
                    if self._merge_with_sync_fix(video_raw, audio_raw, mkv_file, video_start):
                        print(f"  ✓ 音视频同步修正完成 (裁剪音频头部 {video_start:.3f}s)")
                else:
                    print(f"  ✓ 视频首帧偏移 {video_start:.3f}s，无需修正")
            else:
                print(f"  ⚠ 未找到原始中间文件，跳过同步检测")

            # 清理中间文件
            for f in glob.glob(f'{output_path_without_ext}.f*'):
                try:
                    os.remove(f)
                except OSError:
                    pass

            return True, part_title

        print(f"❌ yt-dlp 已耗尽 {max_retries} 次重试机会，当前分P下载彻底失败。")
        return False, fallback_title
