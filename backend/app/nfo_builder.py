import os
import re
import yaml
import xml.etree.ElementTree as ET
from xml.dom import minidom

import group_config
from team_utils import CODE_TO_CANON, FOLDER_TO_CANON


def canon_team(name: str) -> str:
    """把目录名/队伍代码/规范名统一成规范名（TEAM NII / 预备生 ...）。"""
    if not name:
        return ""
    if name in FOLDER_TO_CANON:
        return FOLDER_TO_CANON[name]
    return CODE_TO_CANON.get(str(name).lower(), name)


def clean_members(members) -> list:
    """去掉空值与重复的成员名。"""
    result = []
    for member in members or []:
        name = str(member).strip()
        if name and name not in result:
            result.append(name)
    return result


class NfoBuilder:
    def __init__(self, config_path: str, base_download_path: str, member_store=None):
        self.config_path = config_path
        self.base_download_path = base_download_path
        self.member_store = member_store
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

    def reload_config(self) -> None:
        """重新读取 roster.yaml 与两个剧目名单（wiki 同步后调用）。"""
        self.roster = self._load_roster(self.config_path)
        self.performances = self._load_performances(self.config_path)

    def _load_performances(self, config_path: str) -> list:
        """手工名单（performances.yaml）优先，其次是 wiki 自动生成层。

        两层按 (剧目名, 队伍) 去重；同名同队的条目以手工配置为准。
        """
        base_dir = os.path.dirname(config_path)
        merged, seen = [], set()
        for filename, source in (("performances.yaml", "manual"),
                                 ("performances.wiki.yaml", "wiki")):
            path = os.path.join(base_dir, filename)
            if not os.path.exists(path):
                continue
            try:
                with open(path, 'r', encoding='utf-8') as f:
                    data = yaml.safe_load(f) or {}
            except (OSError, yaml.YAMLError) as exc:
                print(f"⚠️ [NFO] 读取 {filename} 失败: {exc}", flush=True)
                continue
            for entry in data.get('performances', []) or []:
                key = (str(entry.get('title', '')).strip(), canon_team(entry.get('team', '')))
                if key in seen or not key[0]:
                    continue
                seen.add(key)
                item = dict(entry)
                item['_source'] = source
                merged.append(item)
        return merged

    def _match_performance(self, title: str, team_name: str = "",
                           scoped: bool = True) -> list | None:
        """按剧目关键词匹配演职人员名单。

        scoped=True 只认本队条目（同名剧目靠队伍区分）；scoped=False 接受任意
        队伍的条目，用于队名判错或特殊公演的兜底。
        """
        target = canon_team(team_name)
        for perf in self.performances:
            keywords = [str(k) for k in perf.get('keywords', []) if k]
            if not any(kw in title for kw in keywords):
                continue
            members = perf.get('members', []) or []
            if not members:
                continue
            perf_team = canon_team(perf.get('team', ''))
            if scoped and target and perf_team and perf_team != target:
                continue
            return members
        return None

    @staticmethod
    def _team_config(team_name: str) -> dict:
        """按规范名/目录名找到 teams.yaml 中的队伍定义。"""
        canon = canon_team(team_name)
        for team in group_config.teams():
            if team.get("name") == team_name or canon_team(team.get("name", "")) == canon:
                return team
        return {}

    def _team_key(self, team_name: str):
        """规范队伍名 → (分团代码, 队伍代码)，用于查历史任职区间。"""
        team = self._team_config(team_name)
        if team:
            return team.get("group", ""), team.get("code", "")
        return None

    def _roster_members(self, team_name: str) -> list:
        """按队伍名取花名册成员；名字对不上时用规范名再匹配一次。

        特殊公演在 roster.yaml 里叫「特殊公演」，但下载记录里的规范名是
        「SNH48 GROUP」，这里需要兜住这种别名。
        """
        team_data = self.roster.get(team_name)
        if team_data is not None:
            return clean_members(team_data.get('members', []))
        canon = canon_team(team_name)
        full = self._load_roster(self.config_path, enabled_only=False)
        for name, data in full.items():
            if name == team_name or canon_team(name) == canon:
                return clean_members(data.get('members', []))
        return []
    def _members_for(self, title: str, team_name: str, year: str) -> tuple:
        """演职人员来源优先级。

        0) cast_mode=roster 的队伍（拼盘/特别公演）直接用花名册配置
        1) 剧目名单（本队命中，最准确）
        2) 历史任职区间（按公演日期回溯当期阵容）
        3) 剧目名单（跨队兜底，用于队名判错或特殊公演）
        4) 当前花名册
        """
        if self._team_config(team_name).get("cast_mode") == "roster":
            return self._roster_members(team_name), "roster"

        members = self._match_performance(title, team_name, scoped=True)
        if members:
            return clean_members(members), "performances"

        key = self._team_key(team_name)
        if self.member_store and key:
            try:
                year_int = int(str(year)[:4])
            except (TypeError, ValueError):
                year_int = 0
            if year_int:
                historical = self.member_store.members_at(key[0], key[1], year_int)
                if historical:
                    return clean_members(historical), "member_terms"

        other = self._match_performance(title, team_name, scoped=False)
        if other:
            return clean_members(other), "performances_other"

        return self._roster_members(team_name), "roster"
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

            members, source = self._members_for(perf_title, team_name, year)
            print(f"🎭 [NFO] {date_str} {team_name} 演职人员来源: {source}（{len(members)} 人）",
                  flush=True)
            for member in members:
                actor = ET.SubElement(ep_details, "actor")
                ET.SubElement(actor, "name").text = member
                ET.SubElement(actor, "role").text = "Member"

            with open(ep_nfo_path, "w", encoding="utf-8") as f:
                f.write(self._prettify_xml(ep_details))
        
        # 返回基础路径给下载器拼接后缀
        return os.path.join(perf_dir, episode_filename)
