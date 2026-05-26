import os
import sys

import requests


def main():
    if len(sys.argv) != 2:
        raise SystemExit("Usage: python set_telegram_webhook.py https://your-service.onrender.com")
    base_url = sys.argv[1].rstrip("/")
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    secret = os.environ["WEBHOOK_SECRET"]
    url = f"{base_url}/telegram/{secret}"
    res = requests.post(f"https://api.telegram.org/bot{token}/setWebhook", data={"url": url}, timeout=20)
    print(res.text)
    res.raise_for_status()


if __name__ == "__main__":
    main()
