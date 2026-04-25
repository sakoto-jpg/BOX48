import os
import yaml
import xml.etree.ElementTree as ET
from xml.dom import minidom

class NfoBuilder:
    def __init__(self, config_path: str, base_download_path: str):
        self.base_download_path = base_download_path
        self.roster = self._load_roster(config_path)

    def _load_roster(self, config_path: str) -> dict:
        """读取外部的 roster.yaml 花名册"""
        if not os.path.exists(config_path):
            print(f"警告：未找到花名册文件 {config_path}")
            return {}
        with open(config_path, 'r', encoding='utf-8') as f:
            data = yaml.safe_load(f)
        
        roster_dict = {}
        if data and 'teams' in data:
            for team in data['teams']:
                if team.get('enabled', False):
                    # 把 keywords 也存下来
                    roster_dict[team['name']] = {
                        "members": team.get('members', []),
                        "keywords": team.get('keywords', [team['name']])
                    }
        return roster_dict

    def _prettify_xml(self, elem) -> str:
        """将 XML 元素格式化为带有缩进的漂亮字符串"""
        rough_string = ET.tostring(elem, 'utf-8')
        reparsed = minidom.parseString(rough_string)
        # 去除多余的空行
        return '\n'.join([line for line in reparsed.toprettyxml(indent="  ").split('\n') if line.strip()])

    def build_metadata(self, team_name: str, year: str, date_str: str, p_number: str, part_title: str, plot: str):
        """
        核心构建函数：自动创建目录并生成所有必要的 NFO
        :param team_name: 如 "TEAM NII"
        :param year: 如 "2026"
        :param date_str: 如 "2026-04-23"
        :param p_number: 如 "01" (分P的序号)
        :param part_title: 如 "《以爱之名》"
        :param plot: B站视频简介
        """
        # 1. 构建物理路径：/downloads/TEAM NII/Season 2026/
        show_dir = os.path.join(self.base_download_path, team_name)
        season_dir = os.path.join(show_dir, f"Season {year}")
        os.makedirs(season_dir, exist_ok=True)

        # 2. 生成剧集 (TV Show) NFO (如果不存在)
        tvshow_nfo_path = os.path.join(show_dir, "tvshow.nfo")
        if not os.path.exists(tvshow_nfo_path):
            tvshow = ET.Element("tvshow")
            ET.SubElement(tvshow, "title").text = f"SNH48 {team_name}"
            ET.SubElement(tvshow, "plot").text = f"SNH48 {team_name} 日常剧场公演录播"
            with open(tvshow_nfo_path, "w", encoding="utf-8") as f:
                f.write(self._prettify_xml(tvshow))

        # 3. 生成季 (Season) NFO (如果不存在)
        season_nfo_path = os.path.join(season_dir, "season.nfo")
        if not os.path.exists(season_nfo_path):
            season = ET.Element("season")
            ET.SubElement(season, "title").text = f"{year} 年度公演"
            ET.SubElement(season, "seasonnumber").text = year
            with open(season_nfo_path, "w", encoding="utf-8") as f:
                f.write(self._prettify_xml(season))

        # 4. 生成单集 (Episode) NFO
        # 命名规则：S2026E042301 - 《以爱之名》
        # 提取日期的月日部分，如 0423
        mmdd = date_str.replace("-", "")[4:8] 
        episode_num = f"{mmdd}{p_number.zfill(2)}" # e.g., 042301
        
        episode_filename = f"S{year}E{episode_num} - {part_title}"
        episode_nfo_path = os.path.join(season_dir, f"{episode_filename}.nfo")
        
        ep_details = ET.Element("episodedetails")
        ET.SubElement(ep_details, "title").text = part_title
        ET.SubElement(ep_details, "season").text = year
        ET.SubElement(ep_details, "episode").text = episode_num
        ET.SubElement(ep_details, "aired").text = date_str
        ET.SubElement(ep_details, "plot").text = plot

        # 🚀 注入演员名单！
        members = self.roster.get(team_name, [])
        for member in members:
            actor_tag = ET.SubElement(ep_details, "actor")
            ET.SubElement(actor_tag, "name").text = member
            ET.SubElement(actor_tag, "role").text = "Member"
            # ET.SubElement(actor_tag, "type").text = "Actor" # 可选

        with open(episode_nfo_path, "w", encoding="utf-8") as f:
            f.write(self._prettify_xml(ep_details))
        
        # 返回预期的视频和弹幕文件名（不带后缀），方便下载器重命名
        return os.path.join(season_dir, episode_filename)