import os
import requests

sendkey = os.environ["SERVERCHAN_SENDKEY"]

url = f"https://sctapi.ftqq.com/{sendkey}.send"

response = requests.post(
    url,
    data={
        "title": "资金费率监控器测试",
        "desp": """
## GitHub Actions 已启动

如果你能在微信看到这条消息，说明：

- GitHub Actions 正常
- Server酱正常
- SendKey 正常
- 微信推送正常

下一步开始接入资金费率数据。
"""
    },
    timeout=15
)

print("HTTP:", response.status_code)
print("Response:", response.text)
