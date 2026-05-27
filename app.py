import json
import base64
import hashlib
import io
import os
import random
import re
import time
import unicodedata
from datetime import date, timedelta

import requests
from PIL import Image, ImageDraw
from flask import Flask, abort, request

app = Flask(__name__)

PENDING = {}
LAST_DRAFT = {}
RUNTIME_CONFIG = {}
CONFIG_LOADED_AT = 0
CONFIG_TTL_SECONDS = 300


AGENT_CATALOG = {
    "manager": "Phân tích câu lệnh, giao việc cho agent phù hợp, giữ CONFIRM cho hành động thật.",
    "ads_report": "Lấy báo cáo, phân tích ads, đề xuất tối ưu từ Meta Ads.",
    "ads_operator": "Dừng, bật lại campaign/adset/ad sau khi người dùng CONFIRM.",
    "content_writer": "Viết bài, caption, nội dung quảng cáo bằng Gemini.",
    "viral_researcher": "Thu thập bài viết viral từ Facebook/nguồn đầu vào theo ngành, chủ đề, Page hoặc link.",
    "research_filter": "Lọc dữ liệu nghiên cứu: bỏ bài kém liên quan, số liệu yếu, trùng lặp, seeding hoặc lệch ngành.",
    "viral_formula_analyst": "Phân tích bài đã lọc để rút công thức hook, bố cục, góc nhìn, CTA cho content_writer học theo.",
    "image_creator": "Tạo ảnh minh họa bằng OpenAI Images API.",
    "social_publisher": "Đăng bài/ảnh lên Facebook, LinkedIn qua Composio sau khi CONFIRM.",
    "memory_scheduler": "Lưu draft, phong cách viết, lịch đăng. Hiện là bản nền, chưa có DB ngoài.",
}


def env(name, default=None):
    value = os.environ.get(name, default)
    if value is None or value == "":
        raise RuntimeError(f"Missing env var: {name}")
    return value


def gemini_model():
    return os.environ.get("GEMINI_MODEL", "gemini-2.5-flash-lite")


def openai_image_model():
    return os.environ.get("OPENAI_IMAGE_MODEL", "gpt-image-1.5")


def gemini_image_model():
    return os.environ.get("GEMINI_IMAGE_MODEL", "gemini-2.5-flash-image")


def image_provider():
    return os.environ.get("IMAGE_PROVIDER", "openai").lower()


def workspace_config():
    refresh_runtime_config_from_sheet()
    return {
        "drive_folder_id": os.environ.get("GOOGLE_DRIVE_FOLDER_ID", ""),
        "sheet_id": os.environ.get("GOOGLE_SHEET_ID", ""),
        "media_folder_id": os.environ.get("GOOGLE_MEDIA_FOLDER_ID", ""),
        "default_cta": RUNTIME_CONFIG.get("default_cta") or os.environ.get("DEFAULT_CTA", "").replace("\\n", "\n"),
        "default_footer": RUNTIME_CONFIG.get("default_footer") or os.environ.get("DEFAULT_FOOTER", "").replace("\\n", "\n"),
        "image_style": RUNTIME_CONFIG.get("image_style") or os.environ.get(
            "DEFAULT_IMAGE_STYLE",
            "Ảnh thật, sạch, chuyên nghiệp, ánh sáng tự nhiên, phù hợp ngành đồng phục và bảo hộ lao động.",
        ),
        "brand_colors": RUNTIME_CONFIG.get("brand_colors") or os.environ.get(
            "DEFAULT_BRAND_COLORS",
            "vàng kim, đen, trắng; dùng vàng kim làm điểm nhấn, đen tạo cảm giác cao cấp, trắng giữ bố cục sạch.",
        ),
        "brand_tone": RUNTIME_CONFIG.get("brand_tone") or os.environ.get(
            "DEFAULT_BRAND_TONE",
            "Rõ ràng, đáng tin, thực tế, không phóng đại.",
        ),
        "brand_logo_url": RUNTIME_CONFIG.get("brand_logo_url") or os.environ.get("BRAND_LOGO_URL", ""),
        "campaign_context": RUNTIME_CONFIG.get("campaign_context", ""),
    }


def state_file():
    return os.environ.get("BOT_STATE_FILE", "/tmp/telegram_ads_assistant_state.json")


def load_state():
    global LAST_DRAFT, RUNTIME_CONFIG
    try:
        with open(state_file(), "r", encoding="utf-8") as f:
            payload = json.load(f)
        LAST_DRAFT = payload.get("last_draft", {})
        RUNTIME_CONFIG = payload.get("runtime_config", {})
    except Exception:
        LAST_DRAFT = {}
        RUNTIME_CONFIG = {}


def save_state():
    try:
        with open(state_file(), "w", encoding="utf-8") as f:
            json.dump({"last_draft": LAST_DRAFT, "runtime_config": RUNTIME_CONFIG}, f, ensure_ascii=False)
    except Exception:
        app.logger.exception("Could not save bot state")


def parse_table(values):
    if not values:
        return []
    headers = [str(x).strip() for x in values[0]]
    rows = []
    for raw in values[1:]:
        row = {}
        for idx, header in enumerate(headers):
            row[header] = raw[idx] if idx < len(raw) else ""
        rows.append(row)
    return rows


def find_value_ranges(payload):
    found = []

    def walk(node):
        if isinstance(node, dict):
            if isinstance(node.get("valueRanges"), list):
                found.extend(node["valueRanges"])
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(payload)
    return found


def google_sheets_batch_get(ranges):
    payload = {"spreadsheet_id": env("GOOGLE_SHEET_ID"), "ranges": ranges}
    return composio_execute("GOOGLESHEETS_BATCH_GET", payload)


def range_values_map(payload):
    mapped = {}
    for item in find_value_ranges(payload):
        range_name = item.get("range", "")
        values = item.get("values") or []
        if "!" in range_name:
            sheet_name = range_name.split("!", 1)[0].strip("'")
        else:
            sheet_name = range_name
        mapped[sheet_name] = values
    return mapped


def active_rows(rows):
    return [row for row in rows if str(row.get("status", "active")).lower() == "active"]


def apply_runtime_config(config):
    global RUNTIME_CONFIG
    cleaned = {k: v for k, v in config.items() if v not in [None, ""]}
    RUNTIME_CONFIG.update(cleaned)


def refresh_runtime_config_from_sheet(force=False):
    global CONFIG_LOADED_AT
    if not force and time.time() - CONFIG_LOADED_AT < CONFIG_TTL_SECONDS:
        return False
    try:
        payload = google_sheets_batch_get(
            [
                "Settings!A1:B80",
                "Settings_Changes!A1:G300",
                "Image_Styles!A1:H80",
                "Campaign_Context!A1:K80",
            ]
        )
        values_by_sheet = range_values_map(payload)
        config = {}

        settings_rows = parse_table(values_by_sheet.get("Settings", []))
        for row in settings_rows:
            key = str(row.get("key", "")).strip()
            value = row.get("value", "")
            if key:
                config[key] = value

        styles = active_rows(parse_table(values_by_sheet.get("Image_Styles", [])))
        if styles:
            style = styles[-1]
            prompt_rules = style.get("prompt_rules", "")
            negative_rules = style.get("negative_rules", "")
            aspect_ratio = style.get("aspect_ratio", "")
            config["image_style"] = " ".join(x for x in [prompt_rules, negative_rules, f"Tỷ lệ: {aspect_ratio}" if aspect_ratio else ""] if x)

        contexts = active_rows(parse_table(values_by_sheet.get("Campaign_Context", [])))
        if contexts:
            context = contexts[-1]
            config["campaign_context"] = (
                f"{context.get('name', '')}. "
                f"Sản phẩm ưu tiên: {context.get('priority_products', '')}. "
                f"Tệp khách hàng: {context.get('target_audience', '')}. "
                f"Thông điệp chính: {context.get('main_message', '')}."
            ).strip()
            if context.get("cta_override"):
                config["default_cta"] = context.get("cta_override")
            if context.get("footer_override"):
                config["default_footer"] = context.get("footer_override")

        changes = active_rows(parse_table(values_by_sheet.get("Settings_Changes", [])))
        for row in changes:
            setting_type = str(row.get("setting_type", "")).strip()
            value = row.get("value", "")
            if setting_type and value:
                config[setting_type] = value

        if not config.get("brand_colors"):
            config["brand_colors"] = os.environ.get(
                "DEFAULT_BRAND_COLORS",
                "vàng kim, đen, trắng; dùng vàng kim làm điểm nhấn, đen tạo cảm giác cao cấp, trắng giữ bố cục sạch.",
            )

        apply_runtime_config(config)
        CONFIG_LOADED_AT = time.time()
        save_state()
        return True
    except Exception:
        app.logger.exception("Could not refresh runtime config from Sheet")
        CONFIG_LOADED_AT = time.time()
        return False


def normalize_draft(draft):
    if isinstance(draft, dict):
        return draft
    if isinstance(draft, str):
        return {"text": draft}
    return {}


def compact_spaces(text):
    return re.sub(r"\s+", " ", (text or "").strip())


def limit_words(text, max_words=8):
    words = compact_spaces(text).split()
    if len(words) <= max_words:
        return " ".join(words)
    return " ".join(words[:max_words])


def image_hook_from_draft(draft_text, fallback="Đồng phục chuẩn, doanh nghiệp chuyên nghiệp"):
    for line in (draft_text or "").splitlines():
        cleaned = compact_spaces(line).strip("-•# ")
        if cleaned and len(cleaned) >= 8:
            return limit_words(cleaned, 8)
    return limit_words(fallback, 8)


def preview_text(text, max_chars=3000):
    text = text or ""
    if len(text) <= max_chars:
        return text
    preview = text[:max_chars]
    if "\n" in preview:
        preview = preview.rsplit("\n", 1)[0]
    return preview.rstrip() + "\n..."


def strip_tone(text):
    normalized = unicodedata.normalize("NFD", text or "")
    return "".join(ch for ch in normalized if unicodedata.category(ch) != "Mn").lower()


def send_telegram(text):
    token = env("TELEGRAM_BOT_TOKEN")
    chat_id = env("TELEGRAM_CHAT_ID")
    requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data={"chat_id": chat_id, "text": text[:3900]},
        timeout=20,
    ).raise_for_status()


def send_telegram_photo(image_bytes, caption=""):
    token = env("TELEGRAM_BOT_TOKEN")
    chat_id = env("TELEGRAM_CHAT_ID")
    files = {"photo": ("image.png", io.BytesIO(image_bytes), "image/png")}
    data = {"chat_id": chat_id}
    if caption:
        data["caption"] = caption[:1000]
    requests.post(
        f"https://api.telegram.org/bot{token}/sendPhoto",
        data=data,
        files=files,
        timeout=45,
    ).raise_for_status()


def google_drive_download_url(url):
    if not url:
        return ""
    patterns = [
        r"/file/d/([a-zA-Z0-9_-]+)",
        r"[?&]id=([a-zA-Z0-9_-]+)",
        r"/d/([a-zA-Z0-9_-]+)",
    ]
    for pattern in patterns:
        match = re.search(pattern, url)
        if match:
            return f"https://drive.google.com/uc?export=download&id={match.group(1)}"
    return url


def download_brand_logo():
    logo_url = workspace_config().get("brand_logo_url", "").strip()
    if not logo_url:
        return None
    res = requests.get(google_drive_download_url(logo_url), timeout=45)
    res.raise_for_status()
    return res.content


def apply_brand_logo_overlay(image_bytes):
    try:
        logo_bytes = download_brand_logo()
        if not logo_bytes:
            return image_bytes

        base = Image.open(io.BytesIO(image_bytes)).convert("RGBA")
        logo = Image.open(io.BytesIO(logo_bytes)).convert("RGBA")
        base_w, base_h = base.size
        max_logo_w = max(120, int(base_w * 0.18))
        if logo.width > max_logo_w:
            ratio = max_logo_w / logo.width
            logo = logo.resize((max_logo_w, max(1, int(logo.height * ratio))), Image.LANCZOS)

        margin = max(28, int(base_w * 0.04))
        pad = max(12, int(base_w * 0.014))
        x = base_w - logo.width - margin
        y = margin

        plate = Image.new("RGBA", base.size, (0, 0, 0, 0))
        draw = ImageDraw.Draw(plate)
        draw.rounded_rectangle(
            [x - pad, y - pad, x + logo.width + pad, y + logo.height + pad],
            radius=max(12, pad),
            fill=(255, 255, 255, 218),
        )
        base = Image.alpha_composite(base, plate)
        base.alpha_composite(logo, (x, y))

        out = io.BytesIO()
        base.convert("RGB").save(out, format="PNG", optimize=True)
        return out.getvalue()
    except Exception:
        app.logger.exception("Could not apply brand logo overlay")
        return image_bytes


def openai_generate_image(prompt):
    api_key = env("OPENAI_API_KEY")
    res = requests.post(
        "https://api.openai.com/v1/images/generations",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json={
            "model": openai_image_model(),
            "prompt": prompt,
            "size": os.environ.get("OPENAI_IMAGE_SIZE", "1024x1024"),
            "n": 1,
        },
        timeout=120,
    )
    if not res.ok:
        raise RuntimeError(res.text[:1000])
    data = res.json()["data"][0]
    if data.get("b64_json"):
        return base64.b64decode(data["b64_json"])
    if data.get("url"):
        img = requests.get(data["url"], timeout=60)
        img.raise_for_status()
        return img.content
    raise RuntimeError("OpenAI image response did not include image data.")


def gemini_generate_image(prompt):
    key = env("GEMINI_API_KEY")
    model = gemini_image_model()
    res = requests.post(
        f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}",
        headers={"Content-Type": "application/json"},
        json={
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"responseModalities": ["TEXT", "IMAGE"]},
        },
        timeout=120,
    )
    if not res.ok:
        raise RuntimeError(res.text[:1000])
    payload = res.json()
    for candidate in payload.get("candidates", []):
        content = candidate.get("content") or {}
        for part in content.get("parts", []):
            inline_data = part.get("inlineData") or part.get("inline_data")
            if inline_data and inline_data.get("data"):
                return base64.b64decode(inline_data["data"])
    raise RuntimeError("Gemini image response did not include image data.")


def generate_image(prompt):
    if image_provider() == "gemini":
        image_bytes = gemini_generate_image(prompt)
    else:
        image_bytes = openai_generate_image(prompt)
    return apply_brand_logo_overlay(image_bytes)


def composio_account_for_tool(tool_slug):
    slug = (tool_slug or "").upper()
    if slug.startswith("GOOGLESHEETS_") or slug.startswith("GOOGLESUPER_"):
        return (
            os.environ.get("COMPOSIO_GOOGLESHEETS_CONNECTED_ACCOUNT_ID")
            or os.environ.get("COMPOSIO_GOOGLE_CONNECTED_ACCOUNT_ID")
            or "ca_L610ErQ5oEIz"
        )
    if slug.startswith("GOOGLEDRIVE_"):
        return os.environ.get("COMPOSIO_GOOGLEDRIVE_CONNECTED_ACCOUNT_ID") or os.environ.get("COMPOSIO_GOOGLE_CONNECTED_ACCOUNT_ID")
    if slug.startswith("LINKEDIN_"):
        return os.environ.get("COMPOSIO_LINKEDIN_CONNECTED_ACCOUNT_ID")
    return os.environ.get("COMPOSIO_CONNECTED_ACCOUNT_ID")


def composio_user_for_tool(tool_slug):
    slug = (tool_slug or "").upper()
    if slug.startswith("GOOGLESHEETS_") or slug.startswith("GOOGLESUPER_"):
        return os.environ.get("COMPOSIO_GOOGLESHEETS_USER_ID", "pg-test-58e161a6-b048-4f5e-b81c-b660fa24086a")
    return os.environ.get("COMPOSIO_USER_ID", "user_rz7pm")


def composio_execute(tool_slug, input_payload):
    api_key = env("COMPOSIO_API_KEY")
    user_id = composio_user_for_tool(tool_slug)
    body = {"arguments": input_payload, "user_id": user_id, "entity_id": user_id}
    connected_account_id = composio_account_for_tool(tool_slug)
    if connected_account_id:
        body["connected_account_id"] = connected_account_id
    res = requests.post(
        f"https://backend.composio.dev/api/v3.1/tools/execute/{tool_slug}",
        headers={"x-api-key": api_key, "Content-Type": "application/json"},
        json=body,
        timeout=45,
    )
    if not res.ok:
        raise RuntimeError(res.text)
    return res.json()


def composio_tool_schema(tool_slug):
    api_key = env("COMPOSIO_API_KEY")
    res = requests.get(
        f"https://backend.composio.dev/api/v3/tools/{tool_slug}",
        headers={"x-api-key": api_key},
        timeout=30,
    )
    if not res.ok:
        raise RuntimeError(res.text)
    return res.json()


def google_sheets_append(sheet_name, values):
    payload = {
        "spreadsheet_id": env("GOOGLE_SHEET_ID"),
        "sheet_name": sheet_name,
        "values": values,
    }
    return composio_execute("GOOGLESHEETS_BATCH_UPDATE", payload)


def google_sheets_write(sheet_name, values, first_cell_location="A1"):
    payload = {
        "spreadsheet_id": env("GOOGLE_SHEET_ID"),
        "sheet_name": sheet_name,
        "values": values,
        "first_cell_location": first_cell_location,
    }
    return composio_execute("GOOGLESHEETS_BATCH_UPDATE", payload)


def google_sheets_add_sheet(sheet_name, rows=200, columns=20):
    payload = {
        "spreadsheetId": env("GOOGLE_SHEET_ID"),
        "properties": {
            "title": sheet_name,
            "sheetType": "GRID",
            "gridProperties": {"rowCount": rows, "columnCount": columns, "frozenRowCount": 1},
        },
    }
    return composio_execute("GOOGLESHEETS_ADD_SHEET", payload)


def now_text():
    return time.strftime("%Y-%m-%d %H:%M:%S")


def new_record_id(prefix):
    return f"{prefix}_{int(time.time())}_{random.randint(1000, 9999)}"


def append_content_record(topic, draft_text="", image_prompt="", stage="draft", status="needs_review", platform="facebook"):
    record_id = new_record_id("content")
    today = date.today().isoformat()
    row = [
        record_id,
        "",
        "",
        platform,
        "",
        topic[:500],
        draft_text,
        image_prompt,
        "",
        "",
        "",
        "",
        "",
        "",
        stage,
        status,
        now_text(),
    ]
    try:
        google_sheets_append("Content", [row])
        return record_id, None
    except Exception as exc:
        app.logger.exception("Could not append content record")
        return record_id, str(exc)


def append_learning(source, finding, recommendation, confidence="medium", status="active"):
    row = [new_record_id("learning"), source, finding, recommendation, confidence, status, now_text()]
    try:
        google_sheets_append("Learnings", [row])
        return None
    except Exception as exc:
        app.logger.exception("Could not append learning")
        return str(exc)


def append_settings_change(setting_type, value, note="", source="telegram"):
    row = [new_record_id("setting"), setting_type, value, note, source, "active", now_text()]
    try:
        google_sheets_append("Settings_Changes", [row])
        return None
    except Exception as exc:
        app.logger.exception("Could not append settings change")
        return str(exc)


def sheet_note(error):
    if error:
        return f"\n\nChưa ghi được vào Sheet: {error[:300]}"
    return "\n\nĐã lưu vào Sheet."


def composio_upload_file(file_bytes, filename, mimetype, toolkit_slug, tool_slug):
    api_key = env("COMPOSIO_API_KEY")
    file_md5 = hashlib.md5(file_bytes).hexdigest()
    res = requests.post(
        "https://backend.composio.dev/api/v3.1/files/upload/request",
        headers={"x-api-key": api_key, "Content-Type": "application/json"},
        json={
            "toolkit_slug": toolkit_slug,
            "tool_slug": tool_slug,
            "filename": filename,
            "mimetype": mimetype,
            "md5": file_md5,
        },
        timeout=30,
    )
    if not res.ok:
        raise RuntimeError(res.text)
    payload = res.json()
    upload_url = payload.get("url") or payload.get("upload_url") or payload.get("presigned_url")
    s3key = payload.get("key") or payload.get("s3key") or payload.get("s3_key")
    if upload_url and not payload.get("exists"):
        put = requests.put(upload_url, data=file_bytes, headers={"Content-Type": mimetype}, timeout=90)
        if not put.ok:
            raise RuntimeError(put.text[:1000])
    if not s3key:
        raise RuntimeError(f"Composio upload response missing s3 key: {payload}")
    return {"name": filename, "mimetype": mimetype, "s3key": s3key}


def post_to_social(platform, text, image_b64=None):
    platform_key = strip_tone(platform).upper()
    if "FACEBOOK" in platform_key:
        if image_b64:
            action_id = os.environ.get("COMPOSIO_FACEBOOK_PHOTO_ACTION_ID", "FACEBOOK_CREATE_PHOTO_POST")
            image_bytes = base64.b64decode(image_b64)
            photo = composio_upload_file(image_bytes, "telegram-post.png", "image/png", "facebook", action_id)
            default_payload = {
                "page_id": env("COMPOSIO_FACEBOOK_PAGE_ID"),
                "message": text,
                "photo": photo,
                "published": True,
            }
            payload_json = os.environ.get("COMPOSIO_FACEBOOK_PHOTO_INPUT_JSON")
        else:
            action_id = env("COMPOSIO_FACEBOOK_POST_ACTION_ID")
            default_payload = {
                "page_id": env("COMPOSIO_FACEBOOK_PAGE_ID"),
                "message": text,
                "published": True,
            }
            payload_json = os.environ.get("COMPOSIO_FACEBOOK_POST_INPUT_JSON")
    elif "LINKEDIN" in platform_key:
        action_id = env("COMPOSIO_LINKEDIN_POST_ACTION_ID")
        default_payload = {"text": text}
        payload_json = os.environ.get("COMPOSIO_LINKEDIN_POST_INPUT_JSON")
    elif "INSTAGRAM" in platform_key:
        action_id = env("COMPOSIO_INSTAGRAM_POST_ACTION_ID")
        default_payload = {"caption": text}
        payload_json = os.environ.get("COMPOSIO_INSTAGRAM_POST_INPUT_JSON")
    else:
        raise RuntimeError(f"Chưa hỗ trợ nền tảng: {platform}")

    if payload_json:
        payload = json.loads(payload_json.replace("{text}", text))
    else:
        payload = default_payload
    return composio_execute(action_id, payload)


def meta_get(path, params=None):
    version = os.environ.get("META_API_VERSION", "v20.0")
    params = dict(params or {})
    params["access_token"] = env("META_ACCESS_TOKEN")
    res = requests.get(
        f"https://graph.facebook.com/{version}/{path}",
        params=params,
        timeout=30,
    )
    if not res.ok:
        raise RuntimeError(res.text)
    return res.json()


def meta_post(path, data):
    version = os.environ.get("META_API_VERSION", "v20.0")
    payload = dict(data)
    payload["access_token"] = env("META_ACCESS_TOKEN")
    res = requests.post(
        f"https://graph.facebook.com/{version}/{path}",
        data=payload,
        timeout=30,
    )
    if not res.ok:
        raise RuntimeError(res.text)
    return res.json()


def fmt(value, decimals=0):
    if value in (None, ""):
        return "không có dữ liệu"
    try:
        return f"{float(value):,.{decimals}f}"
    except Exception:
        return str(value)


def action_value(actions, names):
    for item in actions or []:
        if item.get("action_type") in names:
            return item.get("value")
    return None


def date_range_from_text(text):
    plain = strip_tone(text)
    today = date.today()
    if any(x in plain for x in ["7 ngay", "7d", "tuan"]):
        return "Báo cáo Ads Facebook - 7 ngày gần nhất", today - timedelta(days=7), today - timedelta(days=1)
    if any(x in plain for x in ["hom nay", "hnay", "today"]):
        return "Báo cáo Ads Facebook - Hôm nay", today, today
    return "Báo cáo Ads Facebook - Hôm qua", today - timedelta(days=1), today - timedelta(days=1)


def insights(level, since, until, fields, limit=20):
    account_id = env("META_AD_ACCOUNT_ID")
    return meta_get(
        f"act_{account_id}/insights",
        {
            "fields": fields,
            "level": level,
            "time_range": json.dumps({"since": since.isoformat(), "until": until.isoformat()}),
            "limit": str(limit),
        },
    ).get("data", [])


def report_text(text):
    title, since, until = date_range_from_text(text)
    rows = insights(
        "account",
        since,
        until,
        "spend,impressions,reach,clicks,ctr,cpc,actions,cost_per_action_type,purchase_roas",
        1,
    )
    if not rows:
        return f"Không có dữ liệu Ads trong khoảng {since} đến {until}."
    row = rows[0]
    leads = action_value(row.get("actions"), ["lead", "onsite_conversion.lead_grouped", "offsite_conversion.fb_pixel_lead"])
    cpl = action_value(row.get("cost_per_action_type"), ["lead", "onsite_conversion.lead_grouped", "offsite_conversion.fb_pixel_lead"])
    roas_items = row.get("purchase_roas") or []
    roas = roas_items[0].get("value") if roas_items else None
    lines = [
        title,
        f"Khoảng ngày: {since} đến {until}",
        "",
        "Tổng quan",
        f"Spend: {fmt(row.get('spend'))} VND",
        f"Impressions: {fmt(row.get('impressions'))}",
        f"Reach: {fmt(row.get('reach'))}",
        f"Clicks: {fmt(row.get('clicks'))}",
        f"CTR: {fmt(row.get('ctr'), 2)}%",
        f"CPC: {fmt(row.get('cpc'))} VND",
        f"Leads: {fmt(leads)}",
        f"Cost/Lead: {fmt(cpl)} VND",
        f"Purchase ROAS: {fmt(roas, 2)}",
    ]
    return "\n".join(lines)


def campaigns_text():
    account_id = env("META_AD_ACCOUNT_ID")
    data = meta_get(
        f"act_{account_id}/campaigns",
        {"fields": "id,name,status,effective_status,daily_budget,lifetime_budget", "limit": "20"},
    ).get("data", [])
    if not data:
        return "Không tìm thấy campaign."
    lines = ["Campaign hiện có:"]
    for c in data:
        budget = c.get("daily_budget") or c.get("lifetime_budget") or "không có"
        lines.append(f"- {c.get('name')}\nid={c.get('id')} | status={c.get('status')} | effective={c.get('effective_status')} | budget={budget}")
    return "\n".join(lines)


def recommendations_text(text):
    title, since, until = date_range_from_text(text)
    rows = insights(
        "campaign",
        since,
        until,
        "campaign_id,campaign_name,spend,impressions,clicks,ctr,cpc,actions,cost_per_action_type",
        50,
    )
    rows = [r for r in rows if float(r.get("spend") or 0) > 0]
    if not rows:
        return f"Không có campaign nào tiêu tiền trong khoảng {since} đến {until}."
    scored = []
    for r in rows:
        leads = float(action_value(r.get("actions"), ["lead", "onsite_conversion.lead_grouped", "offsite_conversion.fb_pixel_lead"]) or 0)
        spend = float(r.get("spend") or 0)
        ctr = float(r.get("ctr") or 0)
        cpc = float(r.get("cpc") or 0)
        score = leads * 100 + ctr * 3 - cpc / 5000
        scored.append((score, leads, spend, ctr, cpc, r))
    good = sorted(scored, reverse=True)[:3]
    bad = sorted(scored, key=lambda x: x[0])[:3]
    lines = [f"Gợi ý tối ưu Ads", f"Khoảng ngày: {since} đến {until}", ""]
    lines.append("Campaign đang tốt:")
    for _, leads, spend, ctr, cpc, r in good:
        lines.append(f"- {r.get('campaign_name')}\nid={r.get('campaign_id')} | Spend {fmt(spend)} | Leads {fmt(leads)} | CTR {fmt(ctr, 2)}% | CPC {fmt(cpc)}")
    lines.append("")
    lines.append("Campaign cần chú ý:")
    for _, leads, spend, ctr, cpc, r in bad:
        reason = "lead thấp" if leads == 0 else "hiệu quả thấp hơn nhóm còn lại"
        lines.append(f"- {r.get('campaign_name')}\nid={r.get('campaign_id')} | Spend {fmt(spend)} | Leads {fmt(leads)} | CTR {fmt(ctr, 2)}% | {reason}")
    lines.append("")
    lines.append("Muốn dừng campaign nào, nhắn: dừng campaign <id>. Bot sẽ yêu cầu CONFIRM.")
    return "\n".join(lines)


def best_ads_text(text):
    title, since, until = date_range_from_text(text)
    rows = insights(
        "ad",
        since,
        until,
        "ad_id,ad_name,campaign_name,spend,impressions,clicks,ctr,cpc,actions,cost_per_action_type",
        50,
    )
    rows = [r for r in rows if float(r.get("spend") or 0) > 0]
    if not rows:
        return f"Không có bài quảng cáo nào tiêu tiền trong khoảng {since} đến {until}."
    scored = []
    for r in rows:
        leads = float(action_value(r.get("actions"), ["lead", "onsite_conversion.lead_grouped", "offsite_conversion.fb_pixel_lead"]) or 0)
        spend = float(r.get("spend") or 0)
        ctr = float(r.get("ctr") or 0)
        cpc = float(r.get("cpc") or 0)
        score = leads * 100 + ctr * 3 - cpc / 5000
        scored.append((score, leads, spend, ctr, cpc, r))
    lines = [f"Bài quảng cáo đang tốt", f"Khoảng ngày: {since} đến {until}", ""]
    for _, leads, spend, ctr, cpc, r in sorted(scored, reverse=True)[:5]:
        lines.append(f"- {r.get('ad_name')}\nid={r.get('ad_id')} | Campaign: {r.get('campaign_name')} | Spend {fmt(spend)} | Leads {fmt(leads)} | CTR {fmt(ctr, 2)}% | CPC {fmt(cpc)}")
    return "\n".join(lines)


def help_text():
    return (
        "Bạn có thể nhắn:\n"
        "báo cáo ads hôm nay\n"
        "bài quảng cáo nào đang tốt\n"
        "nên làm gì hôm nay\n"
        "campaign nào cần chú ý\n"
        "xem danh sách campaign\n"
        "dừng campaign <id>\n"
        "bật lại adset <id>\n"
        "CONFIRM <mã>\n"
        "/cancel"
    )


def gemini_intent(text):
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        return None
    prompt = f"""
Phân loại ý định Telegram Ads assistant.
Chỉ trả JSON, không giải thích.
Intent hợp lệ: report, recommendations, best_ads, campaigns, pause, resume, content, cancel, help, unknown.
Entity hợp lệ: campaign, adset, ad, none.
Text: {text}
JSON schema: {{"intent":"...", "entity":"...", "id":"..."}}
"""
    try:
        res = requests.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/{gemini_model()}:generateContent?key={key}",
            json={"contents": [{"parts": [{"text": prompt}]}]},
            timeout=20,
        )
        if not res.ok:
            app.logger.warning("Gemini failed: %s", res.text[:500])
            return {"intent": "gemini_unavailable", "entity": "none", "id": ""}
        raw = res.json()["candidates"][0]["content"]["parts"][0]["text"]
        raw = raw.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        return json.loads(raw)
    except Exception:
        app.logger.exception("Gemini intent parse failed")
        return {"intent": "gemini_unavailable", "entity": "none", "id": ""}


def gemini_generate_text(user_text):
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        return "Chưa có GEMINI_API_KEY nên chưa tạo nội dung được."
    config = workspace_config()
    prompt = f"""
Bạn là trợ lý marketing tiếng Việt cho ngành đồng phục, bảo hộ lao động, may mặc.
Tone thương hiệu: {config["brand_tone"]}
Ngữ cảnh campaign hiện tại: {config["campaign_context"]}
Viết tự nhiên, rõ ràng, thực tế. Không dùng giọng quảng cáo quá đà. Không bịa số liệu, chứng nhận, khách hàng, dự án nếu người dùng không cung cấp.
Nếu người dùng yêu cầu bài viết, trả lời đúng cấu trúc này:

HOOK:
Một câu mở đầu mạnh, tối đa 16 từ, có dấu tiếng Việt đầy đủ.

NỘI DUNG:
Viết phần nội dung chính, ngắn gọn, có chiều sâu thực tế. Có thể dùng bullet ngắn nếu phù hợp.
Không dùng markdown, không dùng dấu **, không in đậm, không gạch đầu dòng quá dài.

CTA:
Dùng đúng CTA chuẩn bên dưới, không tự đổi ý.

FOOTER:
Dùng đúng footer chuẩn bên dưới, không tự đổi ý.

Giữ độ dài vừa phải để gửi Telegram. Không thêm hashtag ngoài phần footer. Tránh cấu trúc "không chỉ... mà còn".

CTA chuẩn:
{config["default_cta"]}

Footer chuẩn:
{config["default_footer"]}

Yêu cầu của người dùng:
{user_text}
"""
    try:
        res = requests.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/{gemini_model()}:generateContent?key={key}",
            json={"contents": [{"parts": [{"text": prompt}]}]},
            timeout=30,
        )
        if not res.ok:
            app.logger.warning("Gemini content failed: %s", res.text[:500])
            return "Gemini hiện không khả dụng hoặc đã hết quota. Mình chưa tạo nội dung được lúc này."
        return res.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
    except Exception as exc:
        app.logger.exception("Gemini content generation failed")
        return f"Lỗi khi tạo nội dung: {exc}"


def gemini_analyze_viral_formula(user_text):
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        return "Chưa có GEMINI_API_KEY nên chưa phân tích công thức viral được."
    prompt = f"""
Bạn là Viral Formula Analyst cho ngành đồng phục, bảo hộ lao động, may mặc.
Nhiệm vụ:
1. Đọc dữ liệu/bài viết người dùng đưa.
2. Lọc bỏ phần nhiễu: bài không cùng ngành, thiếu ngữ cảnh, seeding, số liệu không đáng tin.
3. Rút ra công thức viết có thể dùng lại cho content_writer.
4. Không sao chép nguyên văn bài gốc.
5. Trả lời bằng tiếng Việt rõ ràng.

Cấu trúc trả lời:
- Bài/ý nào nên giữ
- Bài/ý nào nên loại
- Mẫu hook
- Bố cục nội dung
- Cách tạo niềm tin
- CTA phù hợp
- Công thức viết lại cho ngành đồng phục/bảo hộ
- 3 đề bài content nên viết tiếp

Dữ liệu đầu vào:
{user_text}
"""
    try:
        res = requests.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/{gemini_model()}:generateContent?key={key}",
            json={"contents": [{"parts": [{"text": prompt}]}]},
            timeout=45,
        )
        if not res.ok:
            app.logger.warning("Gemini viral analysis failed: %s", res.text[:500])
            return "Gemini hiện không khả dụng hoặc đã hết quota. Chưa phân tích công thức viral được lúc này."
        return res.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
    except Exception as exc:
        app.logger.exception("Gemini viral formula analysis failed")
        return f"Lỗi khi phân tích công thức viral: {exc}"


def viral_research_text(text):
    return (
        "Luồng nghiên cứu viral đã sẵn sàng, nhưng bot hiện chưa có nguồn Facebook public ổn định để tự quét toàn Facebook.\n\n"
        "Cách dùng hiện tại:\n"
        "1. Gửi link hoặc copy nội dung các bài bạn thấy viral.\n"
        "2. Nhắn: phân tích công thức viral: <dữ liệu bài viết>\n"
        "3. Bot sẽ giao cho research_filter lọc trước, rồi viral_formula_analyst rút công thức cho content_writer.\n\n"
        "Cách tự động hóa sau này:\n"
        "- Kết nối thêm nguồn dữ liệu qua Composio/Facebook Page/Google Sheet.\n"
        "- Lưu danh sách Page đối thủ hoặc Page ngành.\n"
        "- Chạy lịch nghiên cứu hằng ngày/tuần.\n"
        "- Chỉ đưa bài đạt điểm chất lượng sang Agent phân tích."
    )


def image_prompt_from_text(text, draft_text=""):
    config = workspace_config()
    hook = image_hook_from_draft(draft_text)
    user_request = compact_spaces(text)
    campaign_context = compact_spaces(config["campaign_context"]) or "Không có campaign riêng, dùng định vị thương hiệu Sư Tử Vàng."
    return (
        "Tạo một ảnh minh họa marketing khung dọc 4:5 cho thương hiệu Đồng Phục Cao Cấp Sư Tử Vàng.\n"
        "Toàn bộ chỉ dẫn dưới đây là tiếng Việt, giữ đúng tinh thần Việt Nam, không dùng bối cảnh nước ngoài.\n\n"
        "Yêu cầu hình ảnh:\n"
        "- Tỷ lệ ảnh: 4:5, phù hợp đăng Facebook và LinkedIn.\n"
        "- Phong cách: cao cấp, sang trọng, chuyên nghiệp, sạch, ánh sáng đẹp, bố cục có chiều sâu.\n"
        f"- Tone màu thương hiệu: {config['brand_colors']}\n"
        "- Bối cảnh ở Việt Nam: xưởng may hiện đại, văn phòng doanh nghiệp Việt Nam, công trình hoặc nhà máy tại Việt Nam tùy nội dung.\n"
        "- Chủ thể là người Việt Nam, tác phong chuyên nghiệp, trang phục đồng phục hoặc bảo hộ lao động chỉn chu.\n"
        "- Hình ảnh cần có bối cảnh rõ, chủ thể rõ, sản phẩm đồng phục/bảo hộ rõ.\n"
        "- Không nhồi nhiều chữ lên ảnh.\n"
        "- Chỉ đặt một câu Tiêu đề/HOOK ngắn bằng tiếng Việt trên ảnh, dễ đọc, không quá 8 từ.\n"
        f"- Câu Tiêu đề/HOOK trên ảnh: \"{hook}\"\n"
        "- Chừa vùng trống sạch ở góc trên bên phải để hệ thống đóng logo thật của Sư Tử Vàng sau khi tạo ảnh.\n"
        "- Không thêm CTA, số điện thoại, website, hashtag, đoạn văn dài hoặc chữ nhỏ trên ảnh.\n"
        "- Không tự vẽ logo, không tạo logo giả, không viết tên thương hiệu thành logo. Logo thật sẽ được hệ thống đóng lên ảnh sau.\n"
        "- Nếu cần gợi thương hiệu, chỉ dùng tone vàng kim, đen, trắng và cảm giác cao cấp.\n\n"
        "Điều cần tránh:\n"
        "- Tránh chữ méo, chữ sai chính tả, chữ tiếng Anh không cần thiết.\n"
        "- Tránh gương mặt giả quá rõ, tay lỗi, đồng phục méo, logo bịa, khung cảnh nước ngoài.\n"
        "- Tránh nền rối, màu quá sặc sỡ, phong cách hoạt hình nếu không được yêu cầu.\n\n"
        f"Phong cách ảnh đang áp dụng: {config['image_style']}\n"
        f"Ngữ cảnh campaign: {campaign_context}\n"
        f"Yêu cầu cụ thể của người dùng: {user_request}\n\n"
        f"Nội dung bài viết liên quan để hiểu ngữ cảnh, không đưa nguyên văn toàn bộ lên ảnh:\n{draft_text[:1200]}"
    )


def create_image_for_draft(user_text, draft_text=""):
    image_bytes = generate_image(image_prompt_from_text(user_text, draft_text))
    send_telegram_photo(image_bytes, "Ảnh minh họa đã tạo. Nếu muốn đăng kèm bài gần nhất, nhắn: đăng bài này lên Facebook")
    return base64.b64encode(image_bytes).decode("ascii")


def agent_manager_route(text):
    plain = strip_tone(text)
    if plain.startswith("confirm "):
        return "ads_operator"
    if any(x in plain for x in ["doi cta", "cap nhat cta", "doi footer", "cap nhat footer", "doi logo", "cap nhat logo", "logo thuong hieu", "doi style anh", "doi phong cach anh", "cap nhat style anh", "doi tone", "cap nhat tone", "campaign thang nay", "chien dich thang nay"]):
        return "settings_agent"
    if any(x in plain for x in ["nghien cuu viral", "tim bai viral", "facebook viral", "bai viet viral"]):
        return "viral_researcher"
    if any(x in plain for x in ["phan tich cong thuc viral", "cong thuc viral", "hoc cach viet viral", "loc bai viral"]):
        return "viral_formula_analyst"
    if any(x in plain for x in ["tao anh", "anh minh hoa", "hinh minh hoa", "kem anh", "co anh"]):
        return "image_creator"
    if any(x in plain for x in ["dang bai", "post bai", "up bai", "dang len facebook", "dang len linkedin"]):
        return "social_publisher"
    if any(x in plain for x in ["tao cho toi", "viet cho toi", "viet bai", "tao bai", "caption", "content"]):
        return "content_writer"
    if any(x in plain for x in ["dung", "tat", "pause", "bat", "resume", "chay lai"]):
        return "ads_operator"
    if any(x in plain for x in ["bao cao", "ads hom nay", "ads hnay", "ads hom qua", "bai quang cao", "nen lam gi", "goi y", "de xuat", "campaign"]):
        return "ads_report"
    if any(x in plain for x in ["lich", "moi ngay", "10h", "luu cach viet", "nho cach viet"]):
        return "memory_scheduler"
    return "manager"


def agents_text():
    lines = ["Kiến trúc Agent hiện tại:"]
    for name, desc in AGENT_CATALOG.items():
        lines.append(f"- {name}: {desc}")
    lines.append("")
    lines.append("Luồng xử lý: Telegram -> Agent Manager -> Agent con -> API/Composio/Meta/Gemini/OpenAI -> Telegram.")
    lines.append("Các hành động thật như đăng bài, dừng ads, bật ads vẫn cần CONFIRM.")
    return "\n".join(lines)


def extract_setting_value(text):
    patterns = [r":\s*(.+)$", r"thành\s+(.+)$", r"la\s+(.+)$", r"là\s+(.+)$"]
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE | re.DOTALL)
        if match:
            return match.group(1).strip()
    return ""


def settings_agent_handle(text):
    plain = strip_tone(text)
    value = extract_setting_value(text)
    if not value:
        return (
            "Thiếu nội dung cấu hình.\n"
            "Ví dụ:\n"
            "- Đổi CTA thành: ...\n"
            "- Đổi footer thành: ...\n"
            "- Đổi style ảnh thành: ảnh thật trong xưởng, ánh sáng tự nhiên\n"
            "- Đổi tone màu thương hiệu thành: vàng kim, đen, trắng\n"
            "- Đổi logo thương hiệu thành: link Google Drive hoặc link ảnh PNG\n"
            "- Campaign tháng này là: tập trung đồng phục bảo hộ mùa mưa"
        )

    if "cta" in plain:
        key = "default_cta"
        label = "CTA"
    elif "footer" in plain:
        key = "default_footer"
        label = "footer"
    elif "logo" in plain:
        key = "brand_logo_url"
        label = "logo thương hiệu"
    elif any(x in plain for x in ["style anh", "phong cach anh", "prompt anh", "anh minh hoa"]):
        key = "image_style"
        label = "style ảnh"
    elif any(x in plain for x in ["mau thuong hieu", "tone mau", "mau logo", "brand color", "brand colors"]):
        key = "brand_colors"
        label = "tone màu thương hiệu"
    elif any(x in plain for x in ["tone", "giong van", "van phong"]):
        key = "brand_tone"
        label = "tone thương hiệu"
    elif any(x in plain for x in ["campaign", "chien dich", "thang nay", "mua nay", "dot nay"]):
        key = "campaign_context"
        label = "campaign context"
    else:
        key = "general_note"
        label = "ghi chú cấu hình"

    if key != "general_note":
        RUNTIME_CONFIG[key] = value
        save_state()

    sheet_error = append_settings_change(key, value, note=f"Updated {label}")
    return f"Đã cập nhật {label}.\n\nGiá trị mới:\n{value[:1500]}" + sheet_note(sheet_error)


def add_pending(entity, entity_id, status):
    code = str(random.randint(1000, 9999))
    PENDING[code] = {"entity": entity, "id": entity_id, "status": status, "expires": time.time() + 900}
    return code


def add_pending_social(platform, text, image_b64=None):
    code = str(random.randint(1000, 9999))
    PENDING[code] = {
        "type": "social_post",
        "platform": platform,
        "text": text,
        "image_b64": image_b64,
        "expires": time.time() + 900,
    }
    return code


def confirm(code):
    item = PENDING.get(code)
    if not item or item["expires"] < time.time():
        return "Mã CONFIRM không đúng hoặc đã hết hạn."
    if item.get("type") == "social_post":
        result = post_to_social(item["platform"], item["text"], item.get("image_b64"))
        del PENDING[code]
        return f"Đã gửi bài lên {item['platform']} qua Composio.\nKết quả: {json.dumps(result, ensure_ascii=False)[:1000]}"
    meta_post(item["id"], {"status": item["status"]})
    del PENDING[code]
    return f"Đã thực hiện: {item['entity']} {item['id']} -> {item['status']}"


def handle_text(text):
    plain = strip_tone(text)
    chat_key = "default"
    agent = agent_manager_route(text)
    if plain in ["/agents", "agents", "agent", "kien truc agent", "kien truc bot"]:
        return agents_text()
    if plain in ["/help", "help"]:
        return help_text()
    if plain.startswith("confirm "):
        return confirm(plain.split()[-1])
    if plain in ["/cancel", "huy", "cancel"]:
        PENDING.clear()
        return "Đã hủy các lệnh đang chờ xác nhận."
    if plain in ["image prompt", "prompt anh", "lay prompt anh", "xem prompt anh"]:
        draft = normalize_draft(LAST_DRAFT.get(chat_key))
        prompt = draft.get("image_prompt")
        if not prompt:
            return "Chưa có image prompt nào. Hãy nhắn: tạo ảnh minh họa cho bài này"
        return f"Image prompt hiện tại:\n{prompt[:2500]}"
    if agent == "settings_agent":
        return settings_agent_handle(text)
    if agent == "viral_researcher":
        return viral_research_text(text)
    if agent == "viral_formula_analyst":
        return gemini_analyze_viral_formula(text)
    if agent == "image_creator":
        draft = normalize_draft(LAST_DRAFT.get(chat_key))
        draft_text = draft.get("text", "")
        if any(x in plain for x in ["tao bai", "viet bai", "tao noi dung", "viet noi dung", "caption", "content"]) and not draft_text:
            draft_text = gemini_generate_text(text)
        image_prompt = image_prompt_from_text(text, draft_text)
        try:
            image_b64 = create_image_for_draft(text, draft_text)
        except Exception as exc:
            content_id, sheet_error = append_content_record(
                topic=text,
                draft_text=draft_text,
                image_prompt=image_prompt,
                stage="image_pending",
                status="pending_manual_image",
            )
            LAST_DRAFT[chat_key] = {
                "text": draft_text,
                "image_prompt": image_prompt,
                "image_status": "pending_manual_image",
                "content_id": content_id,
            }
            save_state()
            return (
                "Chưa tạo được ảnh bằng API nên đã chuyển sang hàng chờ tạo ảnh thủ công qua Codex/ChatGPT.\n\n"
                f"Content ID: {content_id}\n\n"
                f"Image prompt:\n{preview_text(image_prompt)}"
                + sheet_note(sheet_error)
                + "\n\nBài viết vẫn có thể duyệt và đăng dạng text. Khi tôi tạo ảnh xong, ảnh sẽ được đưa vào folder Media và cập nhật lại Sheet."
            )
        content_id, sheet_error = append_content_record(
            topic=text,
            draft_text=draft_text,
            image_prompt=image_prompt,
            stage="image_ready",
            status="needs_review",
        )
        LAST_DRAFT[chat_key] = {"text": draft_text, "image_b64": image_b64, "image_prompt": image_prompt, "content_id": content_id}
        save_state()
        if draft_text:
            return draft_text + "\n\nĐã tạo ảnh minh họa. Nếu muốn đăng cả bài và ảnh, nhắn: đăng bài này lên Facebook" + sheet_note(sheet_error)
        return "Đã tạo ảnh minh họa. Nếu muốn viết thêm nội dung cho ảnh này, nhắn: viết bài cho ảnh vừa tạo." + sheet_note(sheet_error)
    if "bai quang cao" in plain and ("tot" in plain or "hieu qua" in plain):
        return best_ads_text(text)
    if any(x in plain for x in ["nen lam gi", "goi y", "de xuat", "toi uu", "can chu y", "dang te", "dot tien", "toi nen lam gi"]):
        return recommendations_text(text)
    if any(x in plain for x in ["bao cao", "report", "ads hom nay", "ads hnay", "ads hom qua", "tinh hinh ads", "ads the nao"]):
        return report_text(text)
    if any(x in plain for x in ["tao cho toi", "viet cho toi", "viet bai", "tao bai", "tao noi dung", "viet noi dung", "caption", "content"]):
        draft = gemini_generate_text(text)
        content_id, sheet_error = append_content_record(topic=text, draft_text=draft)
        LAST_DRAFT[chat_key] = {"text": draft, "content_id": content_id}
        save_state()
        return draft + "\n\nNếu muốn tạo ảnh minh họa, nhắn: tạo ảnh minh họa cho bài này\nNếu muốn đăng bài này, nhắn: đăng bài này lên Facebook" + sheet_note(sheet_error)
    if any(x in plain for x in ["dang bai nay len facebook", "dang len facebook", "post bai nay len facebook", "up bai nay len facebook", "dang bai nay len linkedin", "dang len linkedin"]):
        platform = "LinkedIn" if "linkedin" in plain else "Facebook"
        draft = normalize_draft(LAST_DRAFT.get(chat_key))
        if not draft:
            return "Chưa có bản nháp nào để đăng. Hãy nhắn: tạo cho tôi một bài viết về ..."
        draft_text = draft.get("text", "")
        image_b64 = draft.get("image_b64")
        code = add_pending_social(platform, draft_text, image_b64)
        media_note = " kèm ảnh" if image_b64 else ""
        pending_image_note = ""
        if draft.get("image_status") == "pending_manual_image" and not image_b64:
            pending_image_note = "\nẢnh đang chờ tạo thủ công nên lệnh này sẽ đăng text trước."
        return f"Mình sẽ đăng bản nháp gần nhất{media_note} lên {platform} qua Composio.{pending_image_note}\nGửi: CONFIRM {code}\nMã hết hạn sau 15 phút."
    if any(x in plain for x in ["campaign", "chien dich"]) and not any(x in plain for x in ["dung", "tat", "bat", "pause", "resume"]):
        return campaigns_text()
    match = re.search(r"(dung|tat|pause)\s+(campaign|chien dich|adset|nhom quang cao|ad|ads|quang cao)\s+(\d+)", plain)
    if match:
        entity_raw, entity_id = match.group(2), match.group(3)
        entity = "campaign" if entity_raw in ["campaign", "chien dich"] else "adset" if "adset" in entity_raw or "nhom" in entity_raw else "ad"
        code = add_pending(entity, entity_id, "PAUSED")
        return f"Mình hiểu là dừng {entity} {entity_id}.\nGửi: CONFIRM {code}\nMã hết hạn sau 15 phút."
    match = re.search(r"(bat|resume|chay lai|mo lai)\s+(campaign|chien dich|adset|nhom quang cao|ad|ads|quang cao)\s+(\d+)", plain)
    if match:
        entity_raw, entity_id = match.group(2), match.group(3)
        entity = "campaign" if entity_raw in ["campaign", "chien dich"] else "adset" if "adset" in entity_raw or "nhom" in entity_raw else "ad"
        code = add_pending(entity, entity_id, "ACTIVE")
        return f"Mình hiểu là bật lại {entity} {entity_id}.\nGửi: CONFIRM {code}\nMã hết hạn sau 15 phút."
    intent = gemini_intent(text)
    if intent:
        if intent.get("intent") == "best_ads":
            return best_ads_text(text)
        if intent.get("intent") == "recommendations":
            return recommendations_text(text)
        if intent.get("intent") == "report":
            return report_text(text)
        if intent.get("intent") == "campaigns":
            return campaigns_text()
        if intent.get("intent") in ["pause", "resume"]:
            entity = intent.get("entity") or "none"
            entity_id = intent.get("id") or ""
            if entity in ["campaign", "adset", "ad"] and entity_id.isdigit():
                status = "PAUSED" if intent.get("intent") == "pause" else "ACTIVE"
                code = add_pending(entity, entity_id, status)
                action = "dừng" if status == "PAUSED" else "bật lại"
                return f"Mình hiểu là {action} {entity} {entity_id}.\nGửi: CONFIRM {code}\nMã hết hạn sau 15 phút."
            return "Mình hiểu bạn muốn chỉnh quảng cáo, nhưng thiếu ID campaign/adset/ad. Gửi rõ dạng: dừng campaign <id>."
        if intent.get("intent") == "help":
            return help_text()
        if intent.get("intent") == "content":
            draft = gemini_generate_text(text)
            content_id, sheet_error = append_content_record(topic=text, draft_text=draft)
            LAST_DRAFT[chat_key] = {"text": draft, "content_id": content_id}
            save_state()
            return draft + "\n\nNếu muốn tạo ảnh minh họa, nhắn: tạo ảnh minh họa cho bài này\nNếu muốn đăng bài này, nhắn: đăng bài này lên Facebook" + sheet_note(sheet_error)
        if intent.get("intent") == "cancel":
            PENDING.clear()
            return "Đã hủy các lệnh đang chờ xác nhận."
        if intent.get("intent") == "gemini_unavailable":
            return (
                "Gemini hiện không khả dụng hoặc đã hết quota. "
                "Bot vẫn xử lý được các lệnh cơ bản: báo cáo ads hôm nay, bài quảng cáo nào đang tốt, "
                "nên làm gì hôm nay, xem danh sách campaign, dừng campaign <id>."
            )
    return "Mình chưa hiểu rõ. Bạn có thể hỏi: báo cáo ads hôm nay, bài quảng cáo nào đang tốt, hoặc nên làm gì hôm nay."


load_state()


@app.get("/health")
def health():
    return {"ok": True}


@app.get("/debug/gemini/<secret>")
def debug_gemini(secret):
    if secret != env("WEBHOOK_SECRET"):
        abort(404)
    model = request.args.get("model") or gemini_model()
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        return {"ok": False, "model": model, "error": "missing GEMINI_API_KEY"}, 200
    try:
        res = requests.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}",
            json={"contents": [{"parts": [{"text": "Reply with only OK."}]}]},
            timeout=20,
        )
        payload = None
        try:
            payload = res.json()
        except Exception:
            payload = {"raw": res.text[:500]}
        return {
            "ok": res.ok,
            "status_code": res.status_code,
            "model": model,
            "response": payload,
        }, 200
    except Exception as exc:
        return {"ok": False, "model": model, "error": str(exc)}, 200


@app.get("/debug/composio/<secret>")
def debug_composio(secret):
    if secret != env("WEBHOOK_SECRET"):
        abort(404)
    status = {
        "has_api_key": bool(os.environ.get("COMPOSIO_API_KEY")),
        "has_connected_account_id": bool(os.environ.get("COMPOSIO_CONNECTED_ACCOUNT_ID")),
        "user_id": os.environ.get("COMPOSIO_USER_ID", ""),
        "facebook_page_id": os.environ.get("COMPOSIO_FACEBOOK_PAGE_ID", ""),
        "facebook_action_id": os.environ.get("COMPOSIO_FACEBOOK_POST_ACTION_ID", ""),
        "instagram_action_id": os.environ.get("COMPOSIO_INSTAGRAM_POST_ACTION_ID", ""),
    }
    return status


@app.get("/debug/composio-toolkit/<secret>")
def debug_composio_toolkit(secret):
    if secret != env("WEBHOOK_SECRET"):
        abort(404)
    api_key = env("COMPOSIO_API_KEY")
    toolkit = request.args.get("toolkit", "googlesheets")
    search = request.args.get("search", toolkit)
    headers = {"x-api-key": api_key}

    accounts_url = (
        "https://backend.composio.dev/api/v3/connected_accounts"
        f"?toolkit_slugs={toolkit}&statuses=ACTIVE"
    )
    tools_url = (
        "https://backend.composio.dev/api/v3/tools"
        f"?search={requests.utils.quote(search)}&limit=20"
    )
    accounts_res = requests.get(accounts_url, headers=headers, timeout=30)
    tools_res = requests.get(tools_url, headers=headers, timeout=30)

    def safe_json(res):
        try:
            return res.json()
        except Exception:
            return {"raw": res.text[:1000]}

    accounts_payload = safe_json(accounts_res)
    tools_payload = safe_json(tools_res)
    account_items = accounts_payload.get("items") or accounts_payload.get("data") or []
    tool_items = tools_payload.get("items") or tools_payload.get("data") or []
    return {
        "toolkit": toolkit,
        "configured_connected_account_id": (
            os.environ.get("COMPOSIO_GOOGLESHEETS_CONNECTED_ACCOUNT_ID")
            if toolkit == "googlesheets"
            else os.environ.get("COMPOSIO_GOOGLEDRIVE_CONNECTED_ACCOUNT_ID")
            if toolkit == "googledrive"
            else ""
        ),
        "accounts_ok": accounts_res.ok,
        "accounts_status": accounts_res.status_code,
        "active_accounts": [
            {
                "id": item.get("id"),
                "status": item.get("status"),
                "toolkit": ((item.get("toolkit") or {}).get("slug") or item.get("toolkit_slug")),
                "created_at": item.get("created_at"),
            }
            for item in account_items[:10]
            if isinstance(item, dict)
        ],
        "tools_ok": tools_res.ok,
        "tools_status": tools_res.status_code,
        "tools": [
            {
                "slug": item.get("slug"),
                "name": item.get("name"),
                "toolkit": ((item.get("toolkit") or {}).get("slug") or item.get("toolkit_slug")),
            }
            for item in tool_items[:20]
            if isinstance(item, dict)
        ],
    }


@app.get("/debug/composio-tool-schema/<secret>")
def debug_composio_tool_schema(secret):
    if secret != env("WEBHOOK_SECRET"):
        abort(404)
    tool_slug = request.args.get("tool", "GOOGLESHEETS_BATCH_UPDATE")
    try:
        payload = composio_tool_schema(tool_slug)
        item = payload.get("data") or payload
        return {
            "tool": tool_slug,
            "name": item.get("name"),
            "slug": item.get("slug"),
            "toolkit": ((item.get("toolkit") or {}).get("slug") or item.get("toolkit_slug")),
            "input_parameters": item.get("input_parameters"),
        }, 200
    except Exception as exc:
        return {"ok": False, "tool": tool_slug, "error": str(exc)}, 200


@app.get("/debug/sheets-test/<secret>")
def debug_sheets_test(secret):
    if secret != env("WEBHOOK_SECRET"):
        abort(404)
    try:
        now = time.strftime("%Y-%m-%d %H:%M:%S")
        result = google_sheets_append(
            "Learnings",
            [[f"test_{int(time.time())}", "system", "Composio Google Sheets test", "Bot can write to Sheet", "high", "active", now]],
        )
        return {"ok": True, "result": result}, 200
    except Exception as exc:
        return {"ok": False, "error": str(exc)}, 200


@app.get("/debug/content-test/<secret>")
def debug_content_test(secret):
    if secret != env("WEBHOOK_SECRET"):
        abort(404)
    image_prompt = image_prompt_from_text("debug manual image fallback", "Debug draft")
    content_id, error = append_content_record(
        topic="debug manual image fallback",
        draft_text="Debug draft",
        image_prompt=image_prompt,
        stage="image_pending",
        status="pending_manual_image",
    )
    return {"ok": error is None, "content_id": content_id, "error": error}, 200


@app.get("/debug/setup-config-tabs/<secret>")
def debug_setup_config_tabs(secret):
    if secret != env("WEBHOOK_SECRET"):
        abort(404)

    results = []
    specs = {
        "Image_Styles": {
            "headers": ["style_id", "name", "description", "prompt_rules", "negative_rules", "aspect_ratio", "status", "updated_at"],
            "rows": [
                [
                    "img_default",
                    "Ảnh cao cấp 4:5 theo tone Sư Tử Vàng",
                    "Dùng cho post Facebook/LinkedIn ngành đồng phục, bảo hộ lao động và đồng phục doanh nghiệp.",
                    "Ảnh thật, khung dọc 4:5, cao cấp, sang trọng, bối cảnh Việt Nam, chủ thể người Việt Nam, sản phẩm rõ, tone vàng kim - đen - trắng, chỉ có một câu tiêu đề/hook ngắn trên ảnh.",
                    "Tránh nhiều chữ, tránh CTA/footer/hashtag trên ảnh, tránh chữ méo, logo bịa, bối cảnh nước ngoài, mặt người giả quá rõ, nền rối, màu quá sặc sỡ.",
                    "4:5",
                    "active",
                    now_text(),
                ]
            ],
        },
        "Campaign_Context": {
            "headers": ["context_id", "name", "date_from", "date_to", "priority_products", "target_audience", "main_message", "cta_override", "footer_override", "status", "updated_at"],
            "rows": [
                [
                    "ctx_default",
                    "Mặc định",
                    "",
                    "",
                    "Đồng phục cao cấp, đồng phục bảo hộ, đồng phục doanh nghiệp",
                    "Chủ doanh nghiệp, HR, admin, mua hàng, quản lý xưởng",
                    "Đồng phục đúng chuẩn giúp đội ngũ chuyên nghiệp, an toàn, thoải mái hơn.",
                    "",
                    "",
                    "active",
                    now_text(),
                ]
            ],
        },
        "Settings_Changes": {
            "headers": ["change_id", "setting_type", "value", "note", "source", "status", "updated_at"],
            "rows": [],
        },
    }

    for sheet_name, spec in specs.items():
        try:
            google_sheets_add_sheet(sheet_name)
            results.append({"sheet": sheet_name, "created": True})
        except Exception as exc:
            results.append({"sheet": sheet_name, "created": False, "create_error": str(exc)[:200]})
        try:
            values = [spec["headers"]] + spec["rows"]
            google_sheets_write(sheet_name, values, "A1")
            results[-1]["written"] = True
        except Exception as exc:
            results[-1]["written"] = False
            results[-1]["write_error"] = str(exc)[:300]

    return {"ok": True, "results": results}, 200


@app.get("/debug/update-image-style-default/<secret>")
def debug_update_image_style_default(secret):
    if secret != env("WEBHOOK_SECRET"):
        abort(404)
    values = [
        [
            "img_default",
            "Ảnh cao cấp 4:5 theo tone Sư Tử Vàng",
            "Dùng cho post Facebook/LinkedIn ngành đồng phục, bảo hộ lao động và đồng phục doanh nghiệp.",
            "Ảnh thật, khung dọc 4:5, cao cấp, sang trọng, bối cảnh Việt Nam, chủ thể người Việt Nam, sản phẩm rõ, tone vàng kim - đen - trắng, chỉ có một câu tiêu đề/hook ngắn trên ảnh.",
            "Tránh nhiều chữ, tránh CTA/footer/hashtag trên ảnh, tránh chữ méo, logo bịa, bối cảnh nước ngoài, mặt người giả quá rõ, nền rối, màu quá sặc sỡ.",
            "4:5",
            "active",
            now_text(),
        ]
    ]
    try:
        google_sheets_write("Image_Styles", values, "A2")
        append_settings_change(
            "brand_colors",
            "vàng kim, đen, trắng; dùng vàng kim làm điểm nhấn, đen tạo cảm giác cao cấp, trắng giữ bố cục sạch.",
            note="Updated brand image color palette",
            source="system",
        )
        append_settings_change(
            "image_style",
            "Ảnh thật khung dọc 4:5, cao cấp, sang trọng, bối cảnh Việt Nam, chủ thể người Việt Nam, sản phẩm rõ, tone vàng kim - đen - trắng, chỉ có một câu tiêu đề/hook ngắn trên ảnh, không nhiều text.",
            note="Updated default image style",
            source="system",
        )
        refresh_runtime_config_from_sheet(force=True)
        return {"ok": True}, 200
    except Exception as exc:
        return {"ok": False, "error": str(exc)}, 200


@app.post("/debug/handle/<secret>")
def debug_handle(secret):
    if secret != env("WEBHOOK_SECRET"):
        abort(404)
    payload = request.get_json(force=True, silent=True) or {}
    text = payload.get("text", "")
    if not text:
        return {"ok": False, "error": "missing text"}, 200
    try:
        return {"ok": True, "reply": handle_text(text)}, 200
    except Exception as exc:
        return {"ok": False, "error": str(exc)}, 200


@app.get("/debug/openai/<secret>")
def debug_openai(secret):
    if secret != env("WEBHOOK_SECRET"):
        abort(404)
    return {
        "has_api_key": bool(os.environ.get("OPENAI_API_KEY")),
        "image_model": openai_image_model(),
        "image_size": os.environ.get("OPENAI_IMAGE_SIZE", "1024x1024"),
        "image_provider": image_provider(),
    }


@app.get("/debug/logo/<secret>")
def debug_logo(secret):
    if secret != env("WEBHOOK_SECRET"):
        abort(404)
    logo_url = workspace_config().get("brand_logo_url", "")
    result = {
        "has_brand_logo_url": bool(logo_url),
        "download_url_detected": bool(google_drive_download_url(logo_url)) if logo_url else False,
    }
    if logo_url:
        try:
            logo_bytes = download_brand_logo()
            logo = Image.open(io.BytesIO(logo_bytes))
            result.update({"ok": True, "bytes": len(logo_bytes), "width": logo.width, "height": logo.height, "mode": logo.mode})
        except Exception as exc:
            result.update({"ok": False, "error": str(exc)[:500]})
    return result, 200


@app.get("/debug/gemini-image/<secret>")
def debug_gemini_image(secret):
    if secret != env("WEBHOOK_SECRET"):
        abort(404)
    model = request.args.get("model") or gemini_image_model()
    prompt = request.args.get("prompt") or "Create a simple product photo of a yellow work safety uniform on a clean white background."
    old_model = os.environ.get("GEMINI_IMAGE_MODEL")
    os.environ["GEMINI_IMAGE_MODEL"] = model
    try:
        image_bytes = gemini_generate_image(prompt)
        return {
            "ok": True,
            "model": model,
            "bytes": len(image_bytes),
            "mime_guess": "image/png_or_jpeg",
        }, 200
    except Exception as exc:
        return {"ok": False, "model": model, "error": str(exc)}, 200
    finally:
        if old_model is None:
            os.environ.pop("GEMINI_IMAGE_MODEL", None)
        else:
            os.environ["GEMINI_IMAGE_MODEL"] = old_model


@app.get("/debug/workspace/<secret>")
def debug_workspace(secret):
    if secret != env("WEBHOOK_SECRET"):
        abort(404)
    config = workspace_config()
    return {
        "drive_folder_id": config["drive_folder_id"],
        "sheet_id": config["sheet_id"],
        "media_folder_id": config["media_folder_id"],
        "has_default_cta": bool(config["default_cta"]),
        "has_default_footer": bool(config["default_footer"]),
        "has_brand_logo_url": bool(config["brand_logo_url"]),
        "image_style": config["image_style"][:300],
        "brand_tone": config["brand_tone"][:300],
        "campaign_context": config["campaign_context"][:300],
        "runtime_config_keys": sorted(RUNTIME_CONFIG.keys()),
        "config_loaded_at": CONFIG_LOADED_AT,
        "state_file": state_file(),
        "sheet_runtime_auth": "composio" if os.environ.get("COMPOSIO_GOOGLESHEETS_CONNECTED_ACCOUNT_ID") else "not_configured",
    }


@app.get("/debug/reload-config/<secret>")
def debug_reload_config(secret):
    if secret != env("WEBHOOK_SECRET"):
        abort(404)
    refreshed = refresh_runtime_config_from_sheet(force=True)
    config = workspace_config()
    return {
        "ok": True,
        "refreshed": refreshed,
        "keys": sorted(RUNTIME_CONFIG.keys()),
        "image_style": config["image_style"][:500],
        "brand_tone": config["brand_tone"][:500],
        "campaign_context": config["campaign_context"][:500],
        "has_default_cta": bool(config["default_cta"]),
        "has_default_footer": bool(config["default_footer"]),
        "has_brand_logo_url": bool(config["brand_logo_url"]),
    }, 200


@app.post("/telegram/<secret>")
def telegram(secret):
    if secret != env("WEBHOOK_SECRET"):
        abort(404)
    update = request.get_json(force=True, silent=True) or {}
    message = update.get("message") or {}
    chat = message.get("chat") or {}
    if str(chat.get("id")) != str(env("TELEGRAM_CHAT_ID")):
        return {"ok": True}
    text = message.get("text")
    if not text:
        return {"ok": True}
    try:
        send_telegram(handle_text(text))
    except Exception as exc:
        send_telegram(f"Lỗi khi xử lý lệnh: {exc}")
    return {"ok": True}


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "10000")))
