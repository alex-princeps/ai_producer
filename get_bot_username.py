import os
from dotenv import load_dotenv
import requests

load_dotenv(".env")
token = os.getenv("TELEGRAM_TOKEN")
resp = requests.get(f"https://api.telegram.org/bot{token}/getMe").json()
if resp.get("ok"):
    print(f"Bot Username: @{resp['result']['username']}")
else:
    print(f"Error: {resp}")
