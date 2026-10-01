import requests


class EmbyClient:
    """Emby 媒体服务器 API 客户端，用于自动刷新媒体库"""

    def __init__(self, server_url: str, api_key: str, library_name: str):
        self.server_url = server_url.rstrip("/")
        self.api_key = api_key
        self.library_name = library_name
        self._library_id = None  # 缓存，首次使用时查询

    def refresh_library(self) -> bool:
        """刷新配置的媒体库，让 Emby 立即发现新入库的视频文件"""
        if not self.server_url or not self.api_key:
            return False

        library_id = self._get_library_id()
        if not library_id:
            print(f"❌ [Emby] 未找到媒体库: {self.library_name}", flush=True)
            return False

        try:
            url = f"{self.server_url}/emby/Items/{library_id}/Refresh"
            params = {
                "Recursive": "true",
                "ImageRefreshMode": "Default",
                "MetadataRefreshMode": "Default",
                "ReplaceAllImages": "false",
                "ReplaceAllMetadata": "false",
            }
            headers = {"X-Emby-Token": self.api_key}
            resp = requests.post(url, params=params, headers=headers, timeout=15)
            if resp.status_code in (200, 204):
                print(f"🔄 [Emby] 媒体库刷新成功: {self.library_name}", flush=True)
                return True
            else:
                print(f"❌ [Emby] 刷新失败 HTTP {resp.status_code}: {resp.text[:200]}", flush=True)
                return False
        except requests.RequestException as e:
            print(f"❌ [Emby] API 调用异常: {e}", flush=True)
            return False

    @property
    def library_id(self):
        """媒体库 ID（对外暴露，供工具按路径定位条目）。"""
        return self._get_library_id()

    def items(self, parent_id, item_types="Episode", fields="Path", limit=500):
        """分页取回媒体库条目，用于按文件路径定位并精确刷新。"""
        if not self.server_url or not self.api_key:
            return []
        results, start = [], 0
        try:
            while True:
                resp = requests.get(
                    f"{self.server_url}/emby/Items",
                    params={
                        "ParentId": parent_id,
                        "Recursive": "true",
                        "IncludeItemTypes": item_types,
                        "Fields": fields,
                        "StartIndex": start,
                        "Limit": limit,
                    },
                    headers={"X-Emby-Token": self.api_key},
                    timeout=30,
                )
                if resp.status_code != 200:
                    print(f"❌ [Emby] 获取条目失败 HTTP {resp.status_code}", flush=True)
                    return results
                data = resp.json()
                batch = data.get("Items", [])
                results.extend(batch)
                start += len(batch)
                if not batch or start >= data.get("TotalRecordCount", 0):
                    return results
        except requests.RequestException as e:
            print(f"❌ [Emby] 获取条目异常: {e}", flush=True)
            return results

    def refresh_item(self, item_id: str, full: bool = False) -> bool:
        """刷新单个条目；full=True 时重新读取本地 NFO 元数据。"""
        if not item_id or not self.server_url or not self.api_key:
            return False
        try:
            resp = requests.post(
                f"{self.server_url}/emby/Items/{item_id}/Refresh",
                params={
                    "MetadataRefreshMode": "FullRefresh" if full else "Default",
                    "ImageRefreshMode": "Default",
                    "ReplaceAllMetadata": "false",
                    "ReplaceAllImages": "false",
                },
                headers={"X-Emby-Token": self.api_key},
                timeout=30,
            )
            return resp.status_code in (200, 204)
        except requests.RequestException as e:
            print(f"❌ [Emby] 刷新条目失败 {item_id}: {e}", flush=True)
            return False

    def _get_library_id(self):
        """根据 library_name 获取 Emby 媒体库 ID（带缓存）"""
        if self._library_id:
            return self._library_id

        headers = {"X-Emby-Token": self.api_key}
        try:
            # 主要方式：VirtualFolders 接口
            resp = requests.get(
                f"{self.server_url}/emby/Library/VirtualFolders",
                headers=headers,
                timeout=10,
            )
            if resp.status_code == 200:
                for folder in resp.json():
                    if folder.get("Name") == self.library_name:
                        self._library_id = folder.get("ItemId")
                        return self._library_id

            # 备用方式：Items 接口按类型过滤
            resp2 = requests.get(
                f"{self.server_url}/emby/Items",
                params={"IncludeItemTypes": "CollectionFolder", "Recursive": "false"},
                headers=headers,
                timeout=10,
            )
            if resp2.status_code == 200:
                for item in resp2.json().get("Items", []):
                    if item.get("Name") == self.library_name:
                        self._library_id = item.get("Id")
                        return self._library_id

        except requests.RequestException as e:
            print(f"❌ [Emby] 获取媒体库列表失败: {e}", flush=True)

        return None
