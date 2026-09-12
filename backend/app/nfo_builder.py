import os
import re
import yaml
import xml.etree.ElementTree as ET
from xml.dom import minidom

class NfoBuilder:
    def __init__(self, config_path: str, base_download_path: str):
        self.base_download_path = base_download_path
        self.roster = self._load_roster(config_path)
        self.performances = self._load_performances(config_path)

    def _load_roster(self, config_path: str, enabled_only: bool = True) -> dict:
        if not os.path.exists(config_path): return {}
        with open(config_path, 'r', encoding='utf-8') as f:
            data = yaml.safe_load(f)
        
        roster_dict = {}
        if data and 'teams' in data:
            for team in data['teams']:
                if enabled_only and not team.get('enabled', False):
                    continue
                roster_dict[team['name']] = {
                    "members": team.get('members', []),
                    "keywords": team.get('keywords', [team['name']])
                }
        return roster_dict

    def _load_performances(self, config_path: str) -> list:
        performances_path = os.path.join(os.path.dirname(config_path), "performances.yaml")
        if not os.path.exists(performances_path):
            return []
        with open(performances_path, 'r', encoding='utf-8') as f:
            data = yaml.safe_load(f)
        return (data or {}).get('performances', [])

    def _match_performance(self, title: str) -> list | None:
        for perf in self.performances:
            for kw in perf.get('keywords', []):
                if kw in title:
                    return perf.get('members', [])
        return None

    def _prettify_xml(self, elem) -> str:
        rough_string = ET.tostring(elem, 'utf-8')
        reparsed = minidom.parseString(rough_string)
        return '\n'.join([line for line in reparsed.toprettyxml(indent="  ").split('\n') if line.strip()])

    # 🚀 路线一：移除了 p_number 和 part_title 参数，整场公演共用一个 NFO
    def build_metadata(self, team_name: str, year: str, date_str: str, perf_title: str, plot: str):
        show_dir = os.path.join(self.base_download_path, team_name)
        season_dir = os.path.join(show_dir, f"Season {year}")
        
        # 建立单场公演独立文件夹
        safe_perf_title = re.sub(r'[\\/*?:"<>|]', "", perf_title)
        perf_dir = os.path.join(season_dir, f"{date_str} {safe_perf_title}")
        os.makedirs(perf_dir, exist_ok=True)

        # 补全基础目录结构 (tvshow/season nfo)
        tvshow_nfo_path = os.path.join(show_dir, "tvshow.nfo")
        if not os.path.exists(tvshow_nfo_path):
            tvshow = ET.Element("tvshow")
            ET.SubElement(tvshow, "title").text = f"SNH48 {team_name}"
            with open(tvshow_nfo_path, "w", encoding="utf-8") as f:
                f.write(self._prettify_xml(tvshow))

        season_nfo_path = os.path.join(season_dir, "season.nfo")
        if not os.path.exists(season_nfo_path):
            season = ET.Element("season")
            ET.SubElement(season, "title").text = f"{year} 年度公演"
            ET.SubElement(season, "seasonnumber").text = year
            with open(season_nfo_path, "w", encoding="utf-8") as f:
                f.write(self._prettify_xml(season))

        # 🚀 统一集号：使用 月+日 作为集号，例如 4月24日 = E0424
        mmdd = date_str.replace("-", "")[4:8] 
        episode_num = f"{mmdd}"
        
        # 基础剧集名称：不带分 P 后缀
        episode_filename = f"S{year}E{episode_num} - {safe_perf_title}"
        ep_nfo_path = os.path.join(perf_dir, f"{episode_filename}.nfo")
        
        # 如果这场公演的 NFO 还没生成，则生成一次
        if not os.path.exists(ep_nfo_path):
            ep_details = ET.Element("episodedetails")
            ET.SubElement(ep_details, "title").text = safe_perf_title
            ET.SubElement(ep_details, "season").text = year
            ET.SubElement(ep_details, "episode").text = episode_num
            ET.SubElement(ep_details, "aired").text = date_str
            ET.SubElement(ep_details, "plot").text = plot

            members = self._match_performance(perf_title)
            if members is None:
                team_data = self.roster.get(team_name, {})
                members = team_data.get("members", [])
            for member in members:
                actor = ET.SubElement(ep_details, "actor")
                ET.SubElement(actor, "name").text = member
                ET.SubElement(actor, "role").text = "Member"

            with open(ep_nfo_path, "w", encoding="utf-8") as f:
                f.write(self._prettify_xml(ep_details))
        
        # 返回基础路径给下载器拼接后缀
        return os.path.join(perf_dir, episode_filename)
