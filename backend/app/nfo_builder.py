import os
import re
import yaml
import xml.etree.ElementTree as ET
from xml.dom import minidom

class NfoBuilder:
    def __init__(self, config_path: str, base_download_path: str):
        self.base_download_path = base_download_path
        self.roster = self._load_roster(config_path)

    def _load_roster(self, config_path: str) -> dict:
        if not os.path.exists(config_path):
            print(f"警告：未找到花名册文件 {config_path}")
            return {}
        with open(config_path, 'r', encoding='utf-8') as f:
            data = yaml.safe_load(f)
        
        roster_dict = {}
        if data and 'teams' in data:
            for team in data['teams']:
                if team.get('enabled', False):
                    roster_dict[team['name']] = {
                        "members": team.get('members', []),
                        "keywords": team.get('keywords', [team['name']])
                    }
        return roster_dict

    def _prettify_xml(self, elem) -> str:
        rough_string = ET.tostring(elem, 'utf-8')
        reparsed = minidom.parseString(rough_string)
        return '\n'.join([line for line in reparsed.toprettyxml(indent="  ").split('\n') if line.strip()])

    def build_metadata(self, team_name: str, year: str, date_str: str, p_number: str, part_title: str, plot: str, perf_title: str):
        """
        :param perf_title: 完整的 B 站稿件标题，用于创建该场公演的独立文件夹
        """
        show_dir = os.path.join(self.base_download_path, team_name)
        season_dir = os.path.join(show_dir, f"Season {year}")
        os.makedirs(season_dir, exist_ok=True)

        tvshow_nfo_path = os.path.join(show_dir, "tvshow.nfo")
        if not os.path.exists(tvshow_nfo_path):
            tvshow = ET.Element("tvshow")
            ET.SubElement(tvshow, "title").text = f"SNH48 {team_name}"
            ET.SubElement(tvshow, "plot").text = f"SNH48 {team_name} 日常剧场公演录播"
            with open(tvshow_nfo_path, "w", encoding="utf-8") as f:
                f.write(self._prettify_xml(tvshow))

        season_nfo_path = os.path.join(season_dir, "season.nfo")
        if not os.path.exists(season_nfo_path):
            season = ET.Element("season")
            ET.SubElement(season, "title").text = f"{year} 年度公演"
            ET.SubElement(season, "seasonnumber").text = year
            with open(season_nfo_path, "w", encoding="utf-8") as f:
                f.write(self._prettify_xml(season))

        # 🚀 新增：清理标题中的特殊字符，创建单场公演的独立文件夹
        safe_perf_title = re.sub(r'[\\/*?:"<>|]', "", perf_title)
        perf_dir = os.path.join(season_dir, f"{date_str} {safe_perf_title}")
        os.makedirs(perf_dir, exist_ok=True)

        mmdd = date_str.replace("-", "")[4:8] 
        episode_num = f"{mmdd}{p_number.zfill(2)}"
        
        episode_filename = f"S{year}E{episode_num} - {part_title}"
        episode_nfo_path = os.path.join(perf_dir, f"{episode_filename}.nfo")
        
        ep_details = ET.Element("episodedetails")
        ET.SubElement(ep_details, "title").text = part_title
        ET.SubElement(ep_details, "season").text = year
        ET.SubElement(ep_details, "episode").text = episode_num
        ET.SubElement(ep_details, "aired").text = date_str
        ET.SubElement(ep_details, "plot").text = plot

        # 🚀 修复演员名单 Bug：正确读取字典中的 members 数组
        team_data = self.roster.get(team_name, {})
        members = team_data.get("members", [])
        for member in members:
            actor_tag = ET.SubElement(ep_details, "actor")
            ET.SubElement(actor_tag, "name").text = member
            ET.SubElement(actor_tag, "role").text = "Member"

        with open(episode_nfo_path, "w", encoding="utf-8") as f:
            f.write(self._prettify_xml(ep_details))
        
        # 将文件输出到专属文件夹内
        return os.path.join(perf_dir, episode_filename)