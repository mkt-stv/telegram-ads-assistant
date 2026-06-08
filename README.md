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
- `COMPOSIO_FACEBOOK_VIDEO_ACTION_ID` = Facebook video upload/post action in Composio
- `SOCIAL_DIRECT_FALLBACK_ENABLED` = `true`
- `FACEBOOK_PAGE_ID` = Facebook Page ID for direct Graph API fallback
- `FACEBOOK_PAGE_ACCESS_TOKEN` = Page access token for direct Facebook text/photo/video posts
- `LINKEDIN_ACCESS_TOKEN` = LinkedIn token with posting scope
- `LINKEDIN_OWNER_URN` = `urn:li:organization:<id>` or `urn:li:person:<id>`
- `LINKEDIN_ORGANIZATION_ID` = optional shortcut if `LINKEDIN_OWNER_URN` is not set
- `LINKEDIN_VERSION` = LinkedIn API version, default `202506`
- `COMPOSIO_LINKEDIN_POST_ACTION_ID`
- `COMPOSIO_LINKEDIN_PHOTO_ACTION_ID`
- `COMPOSIO_LINKEDIN_VIDEO_ACTION_ID`
- `MAX_SOCIAL_VIDEO_MB` = max video download size before posting, default `50`
- `AUTO_POST_SCHEDULED_CONTENT` = set `true` only if scheduled posts should publish without Telegram approval
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

## Social publishing fallback

The bot tries Composio first when the action id is configured. If Composio is missing or fails and `SOCIAL_DIRECT_FALLBACK_ENABLED=true`, it falls back to official APIs:

- Facebook Page text/photo/video: Graph API using `FACEBOOK_PAGE_ID` and `FACEBOOK_PAGE_ACCESS_TOKEN`.
- LinkedIn text/image/video: LinkedIn REST APIs using `LINKEDIN_ACCESS_TOKEN` and `LINKEDIN_OWNER_URN`.

Check current safe config without exposing tokens:

- `GET /debug/social-direct/<secret>`

Video uploads are supported for workflow testing, but large videos should move to a background queue to avoid Telegram callback timeout.

## Scheduler

Default scheduler mode is safe: it scans due rows in `Content`, sends Telegram approval buttons, and does not publish until confirmation.

- Dry-run: `GET /cron/scheduler-v2/<secret>?dry_run=1`
- Create Telegram approval for due rows: `GET /cron/scheduler-v2/<secret>?dry_run=0`
- Auto-post mode requires `AUTO_POST_SCHEDULED_CONTENT=true` and `auto_post=1`.

Content rows need at least these columns:

- `content_id`
- `scheduled_at`
- `platform`
- `draft_text`
- `status`
- Optional: `media_type`, `media_url`, `image_url`, `video_url`, `stage`, `post_url`, `posted_at`, `last_error`.

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
