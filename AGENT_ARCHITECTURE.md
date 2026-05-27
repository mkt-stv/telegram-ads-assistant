# Telegram Ads Assistant Agent Architecture

## Runtime Flow

Telegram sends each message to the Render webhook. The Flask app passes the text to the Agent Manager. The manager chooses a focused agent, the agent calls the right service, then the bot replies in Telegram.

```text
Telegram
  -> Render Flask webhook
  -> Agent Manager
  -> Agent con
  -> Google Sheet / Meta Ads / Gemini / OpenAI Images / Composio
  -> Telegram reply
```

## Agents

- `manager`: classifies intent, routes work, keeps risky actions behind CONFIRM.
- `ads_report`: reports, best ads, weak campaigns, optimization suggestions.
- `ads_operator`: pauses/resumes campaign, ad set, or ad after CONFIRM.
- `content_writer`: creates Vietnamese posts and captions with Gemini.
- `viral_researcher`: collects viral Facebook post candidates from configured sources, links, copied text, Pages, or later a scheduled data source.
- `research_filter`: scores and filters research data before analysis. It removes noisy posts, duplicates, weak engagement, unrelated industries, and suspicious seeding.
- `viral_formula_analyst`: turns filtered examples into reusable writing formulas for hooks, structure, proof, emotional angle, and CTA.
- `image_creator`: creates illustration images with OpenAI Images API.
- `social_publisher`: posts text or text+image to Facebook/LinkedIn via Composio after CONFIRM.
- `memory_scheduler`: stores the latest draft now; later will own style memory, schedule, and database.

## Google Sheet Workspace

The shared workspace is `STV AI Agent Workspace` in the Drive folder `AI AUTOMATION`.

- Drive folder ID: `11T-9iJ-Q7WL6SnKXZ7cPzGw1FjTmPEeV`
- Sheet ID: `1CjQsVzTAJSBXjhZGD3iwailQ7tTzUc4KjoV7fukywTk`
- Media folder ID: `1HqojclzE5iaPVovTGa-_A43P5oLx8CLw`

Tabs:

- `Settings`: global config, CTA, footer, brand tone, default post times.
- `Content_Pillars`: P1-P6 strategy data.
- `Research`: viral post candidates and research notes.
- `Content`: content calendar, draft text, image URL, approval status, post URL, 7-day engagement.
- `Reports`: daily, weekly, monthly reports.
- `Learnings`: reusable insights for Agent Manager.

The next production step is to replace `/tmp` draft storage with this Sheet. Render cannot use the local Codex Google Drive connector, so runtime auth must be one of:

- Google service account shared into the Sheet.
- Google Apps Script proxy owned by this Sheet.
- Composio Google Sheets action, if connected in the same Composio project.

## Viral Research Flow

This should be a three-agent chain, not a direct researcher -> writer path.

```text
viral_researcher
  -> research_filter
  -> viral_formula_analyst
  -> content_writer
```

Reason: raw viral data is noisy. A post can look viral because of ads, giveaway mechanics, controversy, celebrity effect, Page size, or seeding. The filter agent protects the writing agent from learning the wrong pattern.

Recommended scoring fields:

- Relevance to uniform, workwear, PPE, textile, B2B buying, HR/admin, factory, restaurant, hotel, school, or company uniforms.
- Engagement quality: comments with buying intent count higher than likes.
- Hook clarity: first 1-2 lines must create a reason to continue reading.
- Structure: easy to reuse without copying.
- Trust signals: real use case, comparison, checklist, before/after, price logic, material explanation.
- Risk flags: giveaway bait, outrage bait, unrelated trend, copied meme, fake urgency, low-context viral content.

Current bot supports:

- `nghiên cứu viral Facebook`
- `phân tích công thức viral: <paste nội dung/link/ghi chú bài viết>`

Later automation should add a database table for collected posts, source Page, engagement, filter score, extracted formula, and generated draft.

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
