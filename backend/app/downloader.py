import os
import yt_dlp

class BiliDownloader:
    def __init__(self, cookie_path: str):
        self.cookie_path = cookie_path
        if not os.path.exists(self.cookie_path):
            print(f"警告: 未找到 Cookie 文件: {self.cookie_path}，下载高画质可能受限。")

    def download_part(self, bvid: str, p_number: int, output_path_without_ext: str) -> bool:
        """
        下载 B 站稿件的指定分P
        :param bvid: B站视频的 BV 号 (如 BV11KoMB6E58)
        :param p_number: 视频的分P序号 (如 1, 2, 3...)
        :param output_path_without_ext: 预期输出的文件路径（不带 .mp4 后缀，由 nfo_builder 提供）
        """
        
        # yt-dlp 的核心配置字典
        ydl_opts = {
            # 身份认证
            'cookiefile': self.cookie_path,
            
            # 输出路径与命名模板 (yt-dlp 会自动补充 .mp4 和 .xml 等后缀)
            'outtmpl': f'{output_path_without_ext}.%(ext)s',
            
            # 画质选择：最高画质视频 + 最高画质音频，最后合并为 mp4
            'format': 'bestvideo+bestaudio/best',
            'merge_output_format': 'mp4',
            
            # 🚀 性能优化：开启多线程分片并发下载 (这里设为 8 线程，视你的网络情况可调)
            'concurrent_fragment_downloads': 8,
            
            # 弹幕下载配置 (B 站弹幕会被 yt-dlp 识别为 subtitle)
            'writesubtitles': True,
            'subtitleslangs': ['all'], # 下载所有语言的弹幕/字幕，B 站默认会下 xml
            
            # 指定下载具体的某一个分P (比如 P1 或 P2)
            'playlist_items': str(p_number),
            
            # 终端日志控制
            'quiet': False,
            'no_warnings': True,
        }

        # B 站的完整播放页 URL
        target_url = f"https://www.bilibili.com/video/{bvid}"
        
        print(f"开始并发下载: {bvid} - P{p_number} -> {output_path_without_ext}")
        
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                # 提取信息并下载
                error_code = ydl.download([target_url])
                
                if error_code == 0:
                    print(f"✅ 下载完成: {output_path_without_ext}")
                    return True
                else:
                    print(f"❌ 下载失败 (错误码 {error_code}): {bvid}")
                    return False
        except yt_dlp.utils.DownloadError as e:
            print(f"❌ yt-dlp 发生异常: {e}")
            return False
        except Exception as e:
            print(f"❌ 发生未知错误: {e}")
            return False