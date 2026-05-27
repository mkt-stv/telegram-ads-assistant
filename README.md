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
- `GOOGLE_DRIVE_FOLDER_ID` = `11T-9iJ-Q7WL6SnKXZ7cPzGw1FjTmPEeV`
- `GOOGLE_SHEET_ID` = `1CjQsVzTAJSBXjhZGD3iwailQ7tTzUc4KjoV7fukywTk`
- `GOOGLE_MEDIA_FOLDER_ID` = `1HqojclzE5iaPVovTGa-_A43P5oLx8CLw`
- `DEFAULT_CTA`
- `DEFAULT_FOOTER`
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
- `nghiên cứu viral Facebook`
- `phân tích công thức viral: <nội dung/link bài viết>`

## Google Drive workspace

- Drive folder: `AI AUTOMATION`
- Sheet: `STV AI Agent Workspace`
- Sheet URL: https://docs.google.com/spreadsheets/d/1CjQsVzTAJSBXjhZGD3iwailQ7tTzUc4KjoV7fukywTk/edit
- Media folder ID: `1HqojclzE5iaPVovTGa-_A43P5oLx8CLw`

Sheet tabs:

- `Settings`: CTA, footer, Page ID, lịch đăng mặc định, tone thương hiệu.
- `Content_Pillars`: P1-P6.
- `Research`: dữ liệu nghiên cứu viral.
- `Content`: lịch bài, draft, ảnh, link bài đăng, trạng thái duyệt.
- `Reports`: báo cáo ngày/tuần/tháng.
- `Learnings`: bài học tối ưu cho Agent Manager.
