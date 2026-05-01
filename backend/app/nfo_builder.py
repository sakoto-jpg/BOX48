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
        if not os.path.exists(config_path): return {}
        with open(config_path, 'r', encoding='utf-8') as f:
            data = yaml.safe_load(f)
        
        roster_dict = {}
        if data and 'teams' in data:
            for team in data['teams']:
                if team.get('enabled', False):
                    roster_dict[team['name']] = {
                        "members": team.get('members', []),
                        "keywords": team.get('keywords', [team['name']]),
                        # 🚀 新增：从 YAML 读取音频偏移量，默认 0
                        "audio_offset": team.get('audio_offset', 0)
                    }
        return roster_dict

    def _prettify_xml(self, elem) -> str:
        rough_string = ET.tostring(elem, 'utf-8')
        reparsed = minidom.parseString(rough_string)
        return '\n'.join([line for line in reparsed.toprettyxml(indent="  ").split('\n') if line.strip()])

    def build_metadata(self, team_name: str, year: str, date_str: str, p_number: str, part_title: str, plot: str, perf_title: str):
        show_dir = os.path.join(self.base_download_path, team_name)
        season_dir = os.path.join(show_dir, f"Season {year}")
        
        # 🚀 建立单场公演独立文件夹
        safe_perf_title = re.sub(r'[\\/*?:"<>|]', "", perf_title)
        perf_dir = os.path.join(season_dir, f"{date_str} {safe_perf_title}")
        os.makedirs(perf_dir, exist_ok=True)

        mmdd = date_str.replace("-", "")[4:8] 
        episode_num = f"{mmdd}{p_number.zfill(2)}"
        episode_filename = f"S{year}E{episode_num} - {part_title}"
        
        ep_details = ET.Element("episodedetails")
        ET.SubElement(ep_details, "title").text = part_title
        ET.SubElement(ep_details, "aired").text = date_str
        ET.SubElement(ep_details, "plot").text = plot

        # 🚀 修复演员 Bug
        team_data = self.roster.get(team_name, {})
        members = team_data.get("members", [])
        for member in members:
            actor = ET.SubElement(ep_details, "actor")
            ET.SubElement(actor, "name").text = member
            ET.SubElement(actor, "role").text = "Member"

        with open(os.path.join(perf_dir, f"{episode_filename}.nfo"), "w", encoding="utf-8") as f:
            f.write(self._prettify_xml(ep_details))
        
        return os.path.join(perf_dir, episode_filename)