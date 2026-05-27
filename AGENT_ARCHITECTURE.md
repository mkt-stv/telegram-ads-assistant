# Telegram Ads Assistant Agent Architecture

## Runtime Flow

Telegram sends each message to the Render webhook. The Flask app passes the text to the Agent Manager. The manager chooses a focused agent, the agent calls the right service, then the bot replies in Telegram.

```text
Telegram
  -> Render Flask webhook
  -> Agent Manager
  -> Agent con
  -> Meta Ads / Gemini / OpenAI Images / Composio
  -> Telegram reply
```

## Agents

- `manager`: classifies intent, routes work, keeps risky actions behind CONFIRM.
- `ads_report`: reports, best ads, weak campaigns, optimization suggestions.
- `ads_operator`: pauses/resumes campaign, ad set, or ad after CONFIRM.
- `content_writer`: creates Vietnamese posts and captions with Gemini.
- `image_creator`: creates illustration images with OpenAI Images API.
- `social_publisher`: posts text or text+image to Facebook/LinkedIn via Composio after CONFIRM.
- `memory_scheduler`: stores the latest draft now; later will own style memory, schedule, and database.

## Current Image Plan

The bot uses `OPENAI_IMAGE_MODEL`, default `gpt-image-1.5`. OpenAI docs currently list `gpt-image-1.5`, `gpt-image-1`, and `gpt-image-1-mini`; there is no official `gpt-image-2` model name in the docs checked during implementation.

When the user asks for an image:

1. The `image_creator` creates an image prompt from the current draft or user message.
2. OpenAI Images API returns the image.
3. The bot sends the image back to Telegram for review.
4. The image is stored in the latest draft as base64.
5. If the user says `đăng bài này lên Facebook`, `social_publisher` asks for CONFIRM.
6. After CONFIRM, the image is uploaded to Composio file storage and posted with `FACEBOOK_CREATE_PHOTO_POST`.

## Safety Rules

- Reports and content drafts run immediately.
- Posting, pausing ads, and resuming ads require `CONFIRM`.
- Pending CONFIRM codes expire after 15 minutes.
- If an API key or quota is missing, the agent returns a clear error and does not perform the action.

## Next Build Steps

- Add durable database for style memory, schedules, and post history.
- Add scheduled jobs for daily 10:00 content generation.
- Add LinkedIn connected account/action mapping.
- Add selectable Facebook Page by name.
- Add image style presets for product, factory, worker, flat lay, and carousel.
