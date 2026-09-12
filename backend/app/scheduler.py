import os
import time
import re
import shutil
import glob
import threading
import yt_dlp
import yaml
import traceback
from typing import List, Dict
from concurrent.futures import ThreadPoolExecutor, as_completed
from nfo_builder import NfoBuilder
from downloader import BiliDownloader
from merger import merge_parts, is_song_part, _get_video_codec as get_video_codec
from emby_client import EmbyClient
from history_store import HistoryStore

MC_TITLE_SUFFIX = "MC合集"  # MC模式入库标题/目录后缀；_auto_rewash 靠 path 中该串推断范围，勿改


def _select_mc_parts(parts):
    """过滤歌曲分P，返回 [(原始索引, part), ...]。

    必须保留原始索引：下载分P号(p_number=index+1)必须等于B站原始P号，
    原地过滤列表会让 enumerate 重编号，导致 playlist_items 下载错误的分P。
    flat 标题缺失(None)的分P一律保留，下载后由真实标题二次校验。
    """
    return [(i, p) for i, p in enumerate(parts) if not is_song_part(p.get('title'))]

class Snh48Scanner:
    def __init__(self, config_path: str, cookie_path: str, base_download_path: str):
        self.config_path = config_path
        self.cookie_path = cookie_path
        self.nfo_builder = NfoBuilder(config_path, base_download_path)
        self.downloader = BiliDownloader(cookie_path)
        # 旧 download_history.txt 仅作只读备份；读写全部走 SQLite
        self.history_file = os.path.join(os.path.dirname(config_path), "download_history.txt")
        self.history_store = HistoryStore(os.path.join(os.path.dirname(config_path), "snh48.db"))
        self.active_tasks = {}
        self._progress_lock = threading.Lock()
        self.emby_client = None
        self._wash_failed = {}   # bvid -> 失败时间戳，洗版失败 24h 内不重试
        self._init_emby_client()

    def _init_emby_client(self):
        """从配置文件加载 Emby 设置并初始化客户端"""
        try:
            with open(self.config_path, 'r', encoding='utf-8') as f:
                config = yaml.safe_load(f) or {}
            emby_cfg = config.get("emby", {})
            server_url = emby_cfg.get("server_url", "")
            api_key = emby_cfg.get("api_key", "")
            library_name = emby_cfg.get("library_name", "公演直播回放")
            if server_url and api_key:
                self.emby_client = EmbyClient(server_url, api_key, library_name)
                print(f"✅ [Emby] 客户端已初始化: {library_name} @ {server_url}", flush=True)
            else:
                self.emby_client = None
        except Exception as e:
            print(f"⚠️ [Emby] 初始化失败: {e}", flush=True)
            self.emby_client = None

    def reload_emby_client(self):
        """配置变更后重新加载 Emby 客户端"""
        self._init_emby_client()

    def _is_downloaded(self, bvid: str) -> bool:
        return self.history_store.is_downloaded(bvid)

    def _mark_downloaded(self, bvid: str, title: str, codec: str = '',
                         path: str = '', status: str = 'ok'):
        self.history_store.mark_downloaded(bvid, title, codec=codec, path=path, status=status)

    def _match_team(self, title: str, include_disabled: bool = False) -> str:
        """匹配标题所属队伍。
        include_disabled=True 时也匹配未开启自动入库的队伍（手动入库场景），
        False 时仅匹配已启用队伍（自动入库/自动洗版场景）。"""
        roster = self.nfo_builder.roster
        if include_disabled:
            roster = self.nfo_builder._load_roster(self.config_path, enabled_only=False)
        for team_name, team_data in roster.items():
            keywords = team_data.get("keywords", [])
            for kw in keywords:
                if kw.upper() == "NII" and "NIII" in title.upper():
                    if "NII" not in title.upper().replace("NIII", ""):
                        continue 
                if kw in title or kw.replace(" ", "") in title.replace(" ", ""):
                    return team_name
        return None

    def _get_preferred_codec(self) -> str:
        """读取 preferred_codec 配置。显式 'avc' → 强制 H.264；其余(含缺失、'av1')→ 优先 AV1。"""
        try:
            with open(self.config_path, 'r', encoding='utf-8') as f:
                config = yaml.safe_load(f) or {}
            return 'avc' if config.get('preferred_codec') == 'avc' else 'av01'
        except Exception as e:
            print(f"⚠️ 读取 preferred_codec 配置失败，回退 AVC: {e}", flush=True)
            return 'avc'

    def _get_download_scope(self, kind: str = 'auto') -> str:
        """读取下载范围配置。kind: 'auto'|'manual'；取值 'full'|'mc'，缺失/非法/异常回退 'full'。"""
        key = 'auto_download_scope' if kind == 'auto' else 'manual_download_scope'
        try:
            with open(self.config_path, 'r', encoding='utf-8') as f:
                config = yaml.safe_load(f) or {}
            scope = config.get(key, 'full')
            return scope if scope in ('full', 'mc') else 'full'
        except Exception as e:
            print(f"⚠️ 读取下载范围配置({key})失败，回退整场: {e}", flush=True)
            return 'full'

    def _rewash_flat_opts(self) -> dict:
        """洗版解析 MC 分P 用的浅层扫描选项(与 scan_and_download 同款)。"""
        return {
            'cookiefile': self.cookie_path,
            'quiet': True,
            'extract_flat': True,
            'extractor_retries': 3,
            'http_headers': {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                'Referer': 'https://www.bilibili.com/'
            }
        }

    def _probe_av1_availability(self, bvid: str, p_numbers: List[int] = None) -> bool:
        """探测指定分P是否均提供 AV1 源，全部提供才返回 True，否则(含探测失败)返回 False。

        p_numbers: 1-based 分P号列表，None 表示探测全部。MC 模式只合并选中的
        分P，编码一致性只要求在选中集合内，故仅探测选中分P。
        分P合并要求编码一致，混编会导致合并失败。
        """
        if p_numbers is not None and not p_numbers:
            return False
        ydl_opts = {
            'cookiefile': self.cookie_path,
            'quiet': True,
            'no_warnings': True,
            'extractor_retries': 3,
            'http_headers': {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                'Referer': 'https://www.bilibili.com/'
            }
        }
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(f"https://www.bilibili.com/video/{bvid}", download=False)
            entries = info.get('entries') if info else None
            if not entries:
                return False
            missing_parts = []
            for i, entry in enumerate(entries):
                if p_numbers is not None and (i + 1) not in p_numbers:
                    continue
                if not entry or not any((f.get('vcodec') or '').startswith('av01')
                                        for f in entry.get('formats') or []):
                    missing_parts.append(i + 1)
            if missing_parts:
                print(f"⚠️ 整场 AV1 探测：分P {', '.join(map(str, missing_parts))} 无 AV1 源", flush=True)
                return False
            return True
        except Exception as e:
            print(f"⚠️ AV1 整场探测失败，本场回退 AVC: {e}", flush=True)
            return False

    def _create_hook(self, bvid, p_number=None):
        lock = self._progress_lock
        def hook(d):
            with lock:
                if bvid not in self.active_tasks:
                    return
                task = self.active_tasks[bvid]
                if 'part_progress' not in task:
                    task['part_progress'] = {}

                if d['status'] == 'downloading':
                    percent = d.get('_percent_str', '0%')
                    clean_percent = re.sub(r'\x1b\[[0-9;]*m', '', percent).strip()
                    if p_number is not None:
                        task['part_progress'][p_number] = clean_percent
                    else:
                        task['progress'] = clean_percent
                elif d['status'] == 'finished':
                    if p_number is not None:
                        task['part_progress'][p_number] = '100%'
                        task['p_done'] = task.get('p_done', 0) + 1

                if task.get('part_progress'):
                    parts = task['part_progress']
                    total_pct = sum(float(str(p).replace('%', '')) for p in parts.values())
                    task['progress'] = f"{total_pct / len(parts):.1f}%"

                task['status'] = "视频下载中..."
        return hook

    def _process_video_info(self, bvid: str, title: str, video_info: dict, target_team: str, date_str: str, year: str, formatted_date: str,
                            keep_existing: bool = False, scope: str = 'full'):
        """下载公演并入库。scope: 'full' 整场公演 | 'mc' 仅MC环节(过滤歌曲分P)。
        keep_existing=True(洗版)时失败回滚不删除原有合并文件。"""
        run_start = time.time()
        parts = video_info.get('entries') if 'entries' in video_info else [video_info]
        is_mc = (scope == 'mc')
        print(f"🎯 {bvid} 入库范围: {'仅MC环节' if is_mc else '整场公演'}，共 {len(parts)} 个分P", flush=True)

        # MC 范围：预过滤歌曲分P(flat 标题缺失的分P保留，下载后用真实标题二次校验)。
        # 过滤必须保留原始索引：下载分P号须等于B站原始P号，否则会下载错分P。
        if is_mc:
            selected = _select_mc_parts(parts)
            if not selected:
                print(f"⏭️ {bvid} 无MC环节，跳过入库: {title}", flush=True)
                if bvid in self.active_tasks:
                    self.active_tasks[bvid]['p_total'] = 0
                    self.active_tasks[bvid]['status'] = "无MC环节，跳过入库"
                return
        else:
            selected = list(enumerate(parts))

        if bvid in self.active_tasks:
            self.active_tasks[bvid]['p_total'] = len(selected)
            self.active_tasks[bvid]['p_done'] = 0
            self.active_tasks[bvid]['part_progress'] = {}

        # 编码偏好：AV1 优先时先探测(MC 模式仅探测选中分P)，确保参与合并的分P编码一致
        prefer_codec = self._get_preferred_codec()
        if prefer_codec == 'av01' and not self._probe_av1_availability(bvid, [i + 1 for i, _ in selected]):
            print(f"⚠️ {bvid} 部分分P无 AV1 源，本场回退 H.264(AVC) 编码下载", flush=True)
            prefer_codec = 'avc'

        overall_plot = video_info.get('description') or ''
        if not overall_plot and parts:
            overall_plot = parts[0].get('description') or ''

        # MC 模式在标题/目录后追加「MC合集」，便于 Emby 与历史记录辨识
        perf_title = f"{title} {MC_TITLE_SUFFIX}" if is_mc else title
        base_episode_path = self.nfo_builder.build_metadata(
            team_name=target_team,
            year=year,
            date_str=formatted_date,
            perf_title=perf_title,
            plot=overall_plot
        )

        perf_dir = os.path.dirname(base_episode_path)

        success_count = 0
        part_files = []   # 已下载的分P文件路径
        part_titles = []  # 已下载的分P标题

        stop_flag = threading.Event()

        def _download_one(index: int, part: dict):
            if stop_flag.is_set():
                return None
            p_number = index + 1
            output_path = f"{base_episode_path} - pt{p_number}"
            success, real_title = self.downloader.download_part(
                bvid, p_number, output_path,
                progress_callback=self._create_hook(bvid, p_number),
                prefer_codec=prefer_codec
            )
            if not success:
                stop_flag.set()
            return p_number, success, real_title, output_path

        max_workers = 3
        raw_results = []
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = []
            for index, part in selected:
                if stop_flag.is_set():
                    break
                if len(futures) > 0:
                    time.sleep(0.8)
                futures.append(executor.submit(_download_one, index, part))

            for future in as_completed(futures):
                result = future.result()
                if result is not None:
                    raw_results.append(result)

        raw_results.sort(key=lambda x: x[0])

        for p_number, success, real_title, output_path in raw_results:
            if success:
                success_count += 1
                mkv_file = f"{output_path}.mkv"
                if is_mc and is_song_part(real_title):
                    # 二次校验：flat 标题缺失/误判的歌曲分P在此剔除，不参与合并
                    print(f"🧹 MC范围过滤: P{p_number} 为歌曲环节，删除", flush=True)
                    for f in (mkv_file, f"{output_path}.danmaku.xml", f"{output_path}.nfo"):
                        try:
                            os.remove(f)
                        except OSError:
                            pass
                    continue
                if os.path.exists(mkv_file):
                    part_files.append(mkv_file)
                    part_titles.append(real_title)
            else:
                print(f"❌ P{p_number} 死磕失败，触发数据完整性保护，中断后续分P下载...", flush=True)
                break

        if success_count == len(selected) and part_files:
            # --- 多分P合并为完整视频 + 写入章节 ---
            if len(part_files) >= 2:
                if bvid in self.active_tasks:
                    self.active_tasks[bvid]['status'] = "正在合并分P与生成章节..."
                print(f"🔗 开始合并 {len(part_files)} 个分P → {base_episode_path}.mkv")
                merge_ok = merge_parts(part_files, part_titles, base_episode_path)
                if not merge_ok:
                    print(f"⚠️ 合并失败，保留独立分P文件")
            elif len(part_files) == 1:
                # 单分P：重命名为不带 pt 后缀的标准名称
                single_file = part_files[0]
                target_name = f"{base_episode_path}.mkv"
                if single_file != target_name:
                    try:
                        os.replace(single_file, target_name)
                        # 同时迁移 danmaku 和 nfo
                        for suffix in ('.danmaku.xml', '.nfo'):
                            src = os.path.splitext(single_file)[0] + suffix
                            dst = base_episode_path + suffix if suffix != '.nfo' else f"{base_episode_path}.nfo"
                            # nfo 已由 build_metadata 生成，只需处理 danmaku
                            if suffix == '.danmaku.xml' and os.path.exists(src):
                                os.replace(src, f"{base_episode_path}{suffix}")
                    except OSError as e:
                        print(f"⚠️ 单文件重命名失败: {e}")

            # 记录入库编码与最终文件路径（供前端展示与自动洗版使用）
            final_path = f"{base_episode_path}.mkv"
            if not os.path.exists(final_path):
                # 合并失败/最终文件缺失时不写成功历史，避免产生“有记录无文件”的幽灵条目
                print(f"⚠️ 最终文件不存在，取消入库登记: {final_path}", flush=True)
                if bvid in self.active_tasks:
                    self.active_tasks[bvid]['status'] = "洗版失败，保留原文件" if keep_existing else "合并失败，未入库"
                return
            codec = get_video_codec(final_path)
            self._mark_downloaded(bvid, perf_title, codec, final_path)
            try:
                import requests as req_lib
                with open(self.config_path, 'r', encoding='utf-8') as f:
                    apprise_url = (yaml.safe_load(f) or {}).get("apprise_url", "")
                if apprise_url:
                    if keep_existing:
                        codec_label = {'av1': 'AV1', 'h264': 'H.264', 'hevc': 'HEVC'}.get(codec, codec.upper())
                        payload = {
                            "title": "[SNH48-DownLoader] 洗版完成",
                            "body": f"【{perf_title}】\n已重新下载并替换为 {codec_label} 编码，前往 Emby 观看！",
                            "type": "success"
                        }
                    else:
                        payload = {
                            "title": "[SNH48-DownLoader] MC合集入库完成" if is_mc else "[SNH48-DownLoader] 录播入库完成",
                            "body": f"【{perf_title}】\n已成功下载{'仅MC环节' if is_mc else '完整录播'}并刮削，前往 Emby 观看！",
                            "type": "success"
                        }
                    req_lib.post(apprise_url, json=payload, timeout=5)
            except Exception:
                pass

            # 自动刷新 Emby 媒体库，无需手动扫描
            if self.emby_client:
                try:
                    self.emby_client.refresh_library()
                except Exception:
                    pass

            if bvid in self.active_tasks:
                del self.active_tasks[bvid]
        else:
            # MC模式二次校验后一个分P都不剩：下载本身成功，但全部被识别为歌曲环节
            mc_skip = is_mc and success_count == len(selected) and not part_files
            if mc_skip:
                print(f"⏭️ {bvid} 下载完成但均为歌曲环节，无MC环节，跳过入库: {perf_title}", flush=True)
            if keep_existing:
                # 洗版失败：仅清理本次运行产生的分P/临时文件，保留原有合并文件与字幕
                print(f"🧹 洗版失败回滚：仅清理本次分P文件，保留原有文件", flush=True)
                for pattern in (f"{base_episode_path} - pt*", f"{base_episode_path}.temp.mkv"):
                    for f in glob.glob(pattern):
                        try:
                            if os.path.getmtime(f) >= run_start:
                                os.remove(f)
                        except OSError:
                            pass
                if bvid in self.active_tasks:
                    self.active_tasks[bvid]['status'] = "无MC环节，跳过入库" if mc_skip else "洗版失败，保留原文件"
            else:
                print(f"🗑️ 触发物理回滚，清理不完整的公演数据目录: {perf_dir}", flush=True)
                try:
                    if os.path.exists(perf_dir):
                        shutil.rmtree(perf_dir)
                except Exception as e:
                    print(f"⚠️ 清理脏数据失败: {e}", flush=True)

                if bvid in self.active_tasks:
                    self.active_tasks[bvid]['status'] = "无MC环节，跳过入库" if mc_skip else "残缺失败，已回滚清理"

    def scan_and_download(self, space_url="", scope: str = 'full', include_disabled: bool = False):
        """扫描某 UP 主新投稿并入库。
        include_disabled=True 表示手动扫描：允许匹配未开启自动入库的队伍；
        False 表示自动扫描：只匹配已启用队伍。"""
        if "/video" not in space_url:
            space_url = space_url.rstrip('/') + "/video"
            
        print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] 巡视开始: {space_url}", flush=True)
        
        ydl_opts_flat = {
            'cookiefile': self.cookie_path, 
            'extract_flat': True, 
            'playlistend': 10, 
            'quiet': True,
            'extractor_retries': 3,
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
            print(f"❌ 列表扫描失败: {e}", flush=True)
            return

        for entry in entries:
            bvid = entry.get('id')
            if not bvid or self._is_downloaded(bvid): continue

            print(f"🔎 发现新投稿 {bvid}，获取标题中...", flush=True)
            ydl_opts_full = {
                'cookiefile': self.cookie_path, 
                'quiet': True,
                'extract_flat': True, # 🚀 浅层扫视：不再挨个访问分P网页，拿完总目录清单就跑！
                'extractor_retries': 3,
                'http_headers': {
                    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                    'Referer': 'https://www.bilibili.com/'
                }
            }
            
            max_retries = 8
            for attempt in range(1, max_retries + 1):
                try:
                    with yt_dlp.YoutubeDL(ydl_opts_full) as ydl_full:
                        v_info = ydl_full.extract_info(f"https://www.bilibili.com/video/{bvid}", download=False)
                        title = v_info.get('title', '')
                        target_team = self._match_team(title, include_disabled=include_disabled)
                        date_match = re.search(r'202\d{5}', title)
                        if date_match:
                            date_str = date_match.group()
                        else:
                            date_str = v_info.get('upload_date', '')

                        if target_team and date_str and re.match(r'^\d{8}$', date_str):
                            year = date_str[:4]
                            formatted_date = f"{year}-{date_str[4:6]}-{date_str[6:8]}"

                            self.active_tasks[bvid] = {
                                "title": title, "status": "准备下载...", "progress": "0%", "p_current": 0, "p_total": "?"
                            }
                            self._process_video_info(bvid, title, v_info, target_team, date_str, year, formatted_date, scope=scope)
                        else:
                            print(f"⏭️ 忽略不匹配内容: {title}", flush=True)
                        break 
                except Exception as e:
                    print(f"⚠️ [自动扫描] 第 {attempt} 次解析失败 {bvid}: {e}", flush=True)
                    if attempt < max_retries:
                        sleep_time = attempt * 10 
                        time.sleep(sleep_time)
                    else:
                        print(f"❌ 解析详情彻底失败 {bvid}", flush=True)

    def _process_bvid(self, bvid: str, keep_existing: bool = False, scope: str = 'full', include_disabled: bool = False) -> bool:
        """解析 bvid → 匹配团队/日期 → 下载入库。手动补档与自动洗版共用。

        调用前需保证 active_tasks[bvid] 已存在(用于进度展示)。
        """
        ydl_opts_full = {
            'cookiefile': self.cookie_path,
            'quiet': True,
            'extract_flat': True, # 🚀 浅层扫视：拿完目录就撤退，降低前期报错率
            'extractor_retries': 3,
            'http_headers': {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                'Referer': 'https://www.bilibili.com/'
            }
        }

        max_retries = 8
        for attempt in range(1, max_retries + 1):
            try:
                with yt_dlp.YoutubeDL(ydl_opts_full) as ydl_full:
                    v_info = ydl_full.extract_info(f"https://www.bilibili.com/video/{bvid}", download=False)
                    title = v_info.get('title', '')
                    self.active_tasks[bvid]['title'] = title
                    target_team = self._match_team(title, include_disabled=include_disabled)
                    date_match = re.search(r'202\d{5}', title)
                    if date_match:
                        date_str = date_match.group()
                    else:
                        date_str = v_info.get('upload_date', '')

                    if target_team and date_str and re.match(r'^\d{8}$', date_str):
                        self._process_video_info(bvid, title, v_info, target_team, date_str,
                                                 date_str[:4], f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:8]}",
                                                 keep_existing=keep_existing, scope=scope)
                    else:
                        self.active_tasks[bvid]['status'] = "不符合入库规则"
                    return True
            except Exception as e:
                print(f"⚠️ [解析信息] 第 {attempt} 次解析失败 {bvid}: {e}", flush=True)
                if attempt < max_retries:
                    sleep_time = attempt * 10
                    self.active_tasks[bvid]['status'] = f"解析受阻，冷却{sleep_time}s({attempt}/{max_retries})"
                    time.sleep(sleep_time)
                else:
                    print(f"❌ 解析视频彻底崩溃 {bvid}: {e}", flush=True)
                    traceback.print_exc()
                    self.active_tasks[bvid]['status'] = "解析彻底失败"
        return False

    def download_single(self, video_url: str):
        bvid_match = re.search(r'(BV\w+)', video_url)
        if not bvid_match: return
        bvid = bvid_match.group(1)

        self.active_tasks[bvid] = {"title": bvid, "status": "获取视频信息中...", "progress": "0%", "p_current": 0, "p_total": "?"}
        scope = self._get_download_scope('manual')   # 手动补档跟随「手动入库」下载范围设置
        # 精准补档属于手动入库：允许匹配未开启自动入库的队伍
        self._process_bvid(bvid, scope=scope, include_disabled=True)

    def _auto_rewash(self):
        """自动洗版：检查最近入库的 2 场公演(按 bvid 去重)，
        实际文件编码与下载策略所选编码不符、且 B 站已提供目标编码源流时，自动重新下载替换。"""
        preferred = self._get_preferred_codec()          # 'av01' / 'avc'
        target_codec = 'av1' if preferred == 'av01' else 'h264'

        # 读数据库：取最近入库的 2 场（已按 bvid 去重，只有 ok 记录）
        try:
            candidates = self.history_store.recent(2)
        except Exception as e:
            print(f"⚠️ 洗版：读取历史失败: {e}", flush=True)
            return

        if not candidates:
            return

        for bvid, title, codec, path in candidates:
            if not codec or not path or not os.path.exists(path):
                continue
            if bvid in self.active_tasks:
                continue   # 正在下载/洗版中
            if time.time() - self._wash_failed.get(bvid, 0) < 24 * 3600:
                continue   # 24h 冷却，防反复重下

            # 由入库路径推断该场下载范围，避免把 MC合集 洗成整场版或反之
            scope = 'mc' if MC_TITLE_SUFFIX in path else 'full'

            actual = get_video_codec(path)
            if not actual:
                continue
            if actual == target_codec:
                print(f"  ✓ 洗版检查 {bvid}: 文件已是 {actual}，无需洗版", flush=True)
                continue
            if target_codec == 'av1':
                probe_nums = None
                if scope == 'mc':
                    # MC 场次只探测被选中的 MC 分P，否则某首歌曲分P无 AV1 会导致永远无法洗版
                    try:
                        with yt_dlp.YoutubeDL(self._rewash_flat_opts()) as ydl:
                            info = ydl.extract_info(f"https://www.bilibili.com/video/{bvid}", download=False)
                        selected = _select_mc_parts(info.get('entries') or [])
                        if not selected:
                            print(f"  ⏭️ 洗版暂缓 {bvid}: 无MC分P", flush=True)
                            continue
                        probe_nums = [i + 1 for i, _ in selected]
                    except Exception as e:
                        print(f"⚠️ 洗版 MC 分P解析失败，退回整场探测: {e}", flush=True)
                if not self._probe_av1_availability(bvid, probe_nums):
                    print(f"  ⏭️ 洗版暂缓 {bvid}: B 站尚无 AV1 源，待下轮扫描重试", flush=True)
                    continue

            if not self._match_team(title):
                # 该场次所属队伍未开启自动入库(如手动补档的特殊公演)，不做自动洗版
                print(f"  ⏭️ 洗版跳过 {bvid}: 队伍未开启自动入库，保留手动入库结果", flush=True)
                continue

            print(f"🔄 洗版启动: {bvid} [{codec} → {target_codec}] {title}", flush=True)
            self.active_tasks[bvid] = {"title": title, "status": "洗版下载中...", "progress": "0%", "p_current": 0, "p_total": "?"}
            try:
                self._process_bvid(bvid, keep_existing=True, scope=scope)
                new_codec = get_video_codec(path) if os.path.exists(path) else ''
                if new_codec != target_codec:
                    # 下载失败且原文件被保留：进入冷却，下轮扫描再评估
                    self._wash_failed[bvid] = time.time()
                    print(f"⚠️ 洗版未达标 {bvid}: 文件仍为 {new_codec or '未知'},进入 24h 冷却", flush=True)
                else:
                    print(f"🎉 洗版完成: {bvid} → {new_codec}", flush=True)
            except Exception as e:
                self._wash_failed[bvid] = time.time()
                print(f"⚠️ 洗版异常 {bvid}: {e}", flush=True)

    def scan_all(self, manual: bool = False):
        # 定时调度走「自动入库」设置；前端手动触发(manual=True)走「手动入库」设置
        scope = self._get_download_scope('manual' if manual else 'auto')
        scope_label = '仅MC环节' if scope == 'mc' else '整场公演'
        print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] 全局扫描开始，下载范围: {scope_label}", flush=True)
        with open(self.config_path, 'r', encoding='utf-8') as f:
            data = yaml.safe_load(f)
        for sub in data.get('subscriptions', []):
            # 手动扫描也允许匹配未启用队伍；定时自动扫描仍只处理启用队伍
            self.scan_and_download(sub.get('url'), scope=scope, include_disabled=manual)
        # 扫描结束后自动洗版：独立线程运行，不阻塞后续定时扫描(下载耗时较长)
        threading.Thread(target=self._auto_rewash, daemon=True).start()
