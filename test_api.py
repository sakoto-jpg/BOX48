import requests
import json

def fetch_live_list():
    url = "https://pocketapi.48.cn/live/api/v1/live/getLiveList"
    headers = {
        "Content-Type": "application/json;charset=utf-8",
        "User-Agent": "PocketFans201807/6.0.0 (iPhone; iOS 14.4; Scale/3.00)",
        "appInfo": '{"vendor":"apple","deviceId":"0","appVersion":"6.0.0","appBuild":"200513","osVersion":"14.4.0","osType":"ios","deviceName":"iphone","os":"ios"}',
    }
    payload = {
        "debug": True,
        "next": "0",
        "groupId": 0,
        "record": False
    }

    try:
        print("正在请求口袋48 API...")
        res = requests.post(url, json=payload, headers=headers, timeout=10)
        data = res.json()
        
        # 只提取 liveList 列表
        live_list = data.get("content", {}).get("liveList", [])
        
        if not live_list:
            print("目前没有任何人开播。")
            # 打印全部返回看看是什么结构
            print(json.dumps(data, indent=2, ensure_ascii=False))
            return

        print(f"当前有 {len(live_list)} 人正在直播。前2个人的数据结构如下：\n")
        # 为了不刷屏，我们只打印前 2 个直播间的数据结构
        print(json.dumps(live_list[:2], indent=2, ensure_ascii=False))
        
    except Exception as e:
        print(f"请求失败: {e}")

if __name__ == "__main__":
    fetch_live_list()