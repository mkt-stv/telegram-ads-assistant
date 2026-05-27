# Telegram Ads Assistant on Render

## Render environment variables

Set these in Render:

- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`
- `META_ACCESS_TOKEN`
- `META_AD_ACCOUNT_ID`
- `META_API_VERSION` = `v20.0`
- `GEMINI_API_KEY`
- `OPENAI_API_KEY`
- `OPENAI_IMAGE_MODEL` = `gpt-image-1.5`
- `OPENAI_IMAGE_SIZE` = `1024x1024`
- `COMPOSIO_API_KEY`
- `COMPOSIO_CONNECTED_ACCOUNT_ID`
- `COMPOSIO_USER_ID`
- `COMPOSIO_FACEBOOK_PAGE_ID`
- `COMPOSIO_FACEBOOK_POST_ACTION_ID` = `FACEBOOK_CREATE_POST`
- `COMPOSIO_FACEBOOK_PHOTO_ACTION_ID` = `FACEBOOK_CREATE_PHOTO_POST`
- `WEBHOOK_SECRET`

`WEBHOOK_SECRET` can be any long random string.

## Deploy

Create a Render Web Service from this folder.

Build command:

```bash
pip install -r requirements.txt
```

Start command:

```bash
gunicorn app:app
```

## Set Telegram webhook

After Render gives you a URL, run locally or in Render Shell:

```bash
python set_telegram_webhook.py https://your-service.onrender.com
```

## Supported examples

- `báo cáo ads hôm nay`
- `bài quảng cáo nào đang tốt`
- `nên làm gì hôm nay`
- `campaign nào cần chú ý`
- `xem danh sách campaign`
- `dừng campaign 123456`
- `CONFIRM 1234`

Mutating commands require `CONFIRM`.

## Agent commands

- `/agents`
- `tạo ảnh minh họa cho bài này`
- `tạo bài viết về ... kèm ảnh`
- `đăng bài này lên Facebook`
- `đăng bài này lên LinkedIn`
