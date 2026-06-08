import json
import base64
import hashlib
import io
import os
import random
import re
import threading
import time
import unicodedata
from datetime import date, datetime, timedelta

import requests
from PIL import Image, ImageDraw, ImageFont
from flask import Flask, abort, request

app = Flask(__name__)

PENDING = {}
LAST_DRAFT = {}
RUNTIME_CONFIG = {}
CONFIG_LOADED_AT = 0
CONFIG_TTL_SECONDS = 300
TELEGRAM_OUTBOX = []
PROCESSED_UPDATES = set()
LAST_STATE_SHEET_SYNC = 0
STATE_LOCK = threading.RLock()
LAST_CONFIG_SIGNATURE = ""
CURRENT_CONFIG_SIGNATURE = ""
CONFIG_WATCH_STARTED = False
CONFIG_WATCH_LOCK = threading.Lock()
CONFIG_WATCH_SHEETS = [
    "Settings!A1:B80",
    "Settings_Changes!A1:G300",
    "Content_Prompt!A1:E120",
    "Content_Pillars!A1:H120",
    "Image_Styles!A1:H80",
    "Campaign_Context!A1:K80",
]

CONTENT_HEADERS = [
    "content_id",
    "scheduled_date",
    "scheduled_time",
    "platform",
    "pillar_id",
    "topic",
    "draft_text",
    "image_prompt",
    "image_url",
    "video_url",
    "media_url",
    "media_type",
    "post_url",
    "posted_at",
    "stage",
    "status",
    "updated_at",
]


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
    return (RUNTIME_CONFIG.get("image_provider") or os.environ.get("IMAGE_PROVIDER", "openai")).lower()


def workspace_config(refresh=True):
    if refresh:
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
        "content_prompt_style": RUNTIME_CONFIG.get("content_prompt_style")
        or os.environ.get(
            "CONTENT_PROMPT_STYLE",
            "Viết như một cố vấn thực tế, rõ ý, có chiều sâu. Không viết kiểu quảng cáo lố.",
        ),
        "content_structure": RUNTIME_CONFIG.get("content_structure")
        or os.environ.get(
            "CONTENT_STRUCTURE",
            "Dòng đầu là hook Title Case. Sau đó viết 2-3 đoạn ngắn. Có thể dùng tối đa 3 bullet nếu cần. Cuối bài có CTA và footer.",
        ),
        "content_do_not_use": RUNTIME_CONFIG.get("content_do_not_use")
        or os.environ.get(
            "CONTENT_DO_NOT_USE",
            "Không dùng các cụm sáo rỗng, không bịa số liệu, không dùng nhãn HOOK/NỘI DUNG/CTA/FOOTER.",
        ),
        "content_examples": RUNTIME_CONFIG.get("content_examples") or os.environ.get("CONTENT_EXAMPLES", ""),
        "content_brand_voice": RUNTIME_CONFIG.get("content_brand_voice")
        or os.environ.get(
            "CONTENT_BRAND_VOICE",
            "Sư Tử Vàng nói chuyện rõ ràng, đáng tin, có kinh nghiệm thực tế trong đồng phục và bảo hộ lao động.",
        ),
        "content_pillars_summary": RUNTIME_CONFIG.get("content_pillars_summary") or "",
        "content_pillars_data": RUNTIME_CONFIG.get("content_pillars_data") or [],
    }


def state_file():
    return os.environ.get("BOT_STATE_FILE", "/tmp/telegram_ads_assistant_state.json")


def load_state():
    global PENDING, LAST_DRAFT, RUNTIME_CONFIG, LAST_CONFIG_SIGNATURE, CURRENT_CONFIG_SIGNATURE, PROCESSED_UPDATES, TELEGRAM_OUTBOX
    with STATE_LOCK:
        path = state_file()
        if not os.path.exists(path):
            PENDING = {}
            LAST_DRAFT = {}
            RUNTIME_CONFIG = {}
            LAST_CONFIG_SIGNATURE = ""
            CURRENT_CONFIG_SIGNATURE = ""
            PROCESSED_UPDATES = set()
            TELEGRAM_OUTBOX = []
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                payload = json.load(f)
            PENDING = payload.get("pending", {})
            LAST_DRAFT = payload.get("last_draft", {})
            RUNTIME_CONFIG = payload.get("runtime_config", {})
            LAST_CONFIG_SIGNATURE = payload.get("last_config_signature", "")
            CURRENT_CONFIG_SIGNATURE = payload.get("current_config_signature", "")
            PROCESSED_UPDATES = set(payload.get("processed_updates", []))
            TELEGRAM_OUTBOX = payload.get("telegram_outbox", [])[-30:]
        except Exception:
            app.logger.exception("Could not load bot state; keeping in-memory state")


def save_state():
    with STATE_LOCK:
        path = state_file()
        tmp_path = f"{path}.tmp.{os.getpid()}.{threading.get_ident()}"
        payload = {
            "last_draft": LAST_DRAFT,
            "pending": PENDING,
            "runtime_config": RUNTIME_CONFIG,
            "last_config_signature": LAST_CONFIG_SIGNATURE,
            "current_config_signature": CURRENT_CONFIG_SIGNATURE,
            "processed_updates": list(PROCESSED_UPDATES)[-500:],
            "telegram_outbox": TELEGRAM_OUTBOX[-30:],
        }
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False)
            os.replace(tmp_path, path)
        except Exception:
            app.logger.exception("Could not save bot state")
            try:
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)
            except Exception:
                pass


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


def normalize_header_name(value):
    plain = strip_tone(str(value or "")).strip().lower()
    plain = re.sub(r"[^a-z0-9]+", "_", plain).strip("_")
    return plain


def column_letter(index):
    index += 1
    letters = ""
    while index:
        index, remainder = divmod(index - 1, 26)
        letters = chr(65 + remainder) + letters
    return letters


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


def config_signature(values_by_sheet):
    relevant = {sheet: values_by_sheet.get(sheet, []) for sheet in sorted(["Settings", "Settings_Changes", "Content_Prompt", "Content_Pillars", "Image_Styles", "Campaign_Context"])}
    raw = json.dumps(relevant, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


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
    global CONFIG_LOADED_AT, CURRENT_CONFIG_SIGNATURE
    if not force and time.time() - CONFIG_LOADED_AT < CONFIG_TTL_SECONDS:
        return False
    try:
        payload = google_sheets_batch_get(CONFIG_WATCH_SHEETS)
        values_by_sheet = range_values_map(payload)
        CURRENT_CONFIG_SIGNATURE = config_signature(values_by_sheet)
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

        prompt_rows = active_rows(parse_table(values_by_sheet.get("Content_Prompt", [])))
        for row in prompt_rows:
            setting_type = str(row.get("setting_type", "")).strip()
            value = row.get("value", "")
            if setting_type and value:
                config[setting_type] = value

        pillars = active_rows(parse_table(values_by_sheet.get("Content_Pillars", [])))
        if pillars:
            lines = []
            config["content_pillars_data"] = pillars
            for row in pillars:
                lines.append(
                    " | ".join(
                        x
                        for x in [
                            row.get("pillar_id", ""),
                            row.get("pillar_name", ""),
                            f"Core: {compact_spaces(row.get('core_message', ''))[:500]}",
                            f"Pain: {compact_spaces(row.get('pain_points', ''))[:350]}",
                            f"Angles: {compact_spaces(row.get('content_angles', ''))[:500]}",
                        ]
                        if x
                    )
                )
            config["content_pillars_summary"] = "\n".join(lines)

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


def check_config_updates(notify=True, initialize=False):
    global LAST_CONFIG_SIGNATURE
    with CONFIG_WATCH_LOCK:
        refreshed = refresh_runtime_config_from_sheet(force=True)
        signature = CURRENT_CONFIG_SIGNATURE
        if not signature:
            return {"ok": False, "refreshed": refreshed, "changed": False, "error": "missing signature"}
        if not LAST_CONFIG_SIGNATURE or initialize:
            LAST_CONFIG_SIGNATURE = signature
            save_state()
            return {"ok": True, "refreshed": refreshed, "changed": False, "initialized": True}
        if signature == LAST_CONFIG_SIGNATURE:
            return {"ok": True, "refreshed": refreshed, "changed": False}

        LAST_CONFIG_SIGNATURE = signature
        save_state()
        if notify:
            send_telegram(
                "Đã nhận cập nhật mới từ Sheet.\n"
                "Bot đã reload Content_Prompt, Content_Pillars và các cấu hình liên quan.\n"
                "Các bài viết/lệnh sau sẽ dùng bản mới."
            )
        return {"ok": True, "refreshed": refreshed, "changed": True, "notified": notify}


def config_watch_interval_seconds():
    raw = os.environ.get("CONFIG_WATCH_INTERVAL_SECONDS", "300")
    try:
        return max(60, int(raw))
    except ValueError:
        return 300


def config_watcher_loop():
    try:
        check_config_updates(notify=False, initialize=True)
    except Exception:
        app.logger.exception("Could not initialize config watcher")
    while True:
        time.sleep(config_watch_interval_seconds())
        try:
            check_config_updates(notify=True)
        except Exception:
            app.logger.exception("Could not check config updates")


def start_config_watcher():
    global CONFIG_WATCH_STARTED
    if CONFIG_WATCH_STARTED:
        return False
    if os.environ.get("CONFIG_WATCH_ENABLED", "true").lower() not in ["1", "true", "yes", "on"]:
        return False
    CONFIG_WATCH_STARTED = True
    thread = threading.Thread(target=config_watcher_loop, name="config-watcher", daemon=True)
    thread.start()
    return True


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
    if is_generation_error(draft_text):
        return limit_words(fallback, 8)
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


def safe_json_value(value):
    if isinstance(value, str):
        return value.encode("utf-8", "replace").decode("utf-8")
    if isinstance(value, dict):
        return {safe_json_value(k): safe_json_value(v) for k, v in value.items()}
    if isinstance(value, list):
        return [safe_json_value(item) for item in value]
    return value


def clean_generated_post(text):
    text = (text or "").strip()
    text = text.removeprefix("```").removesuffix("```").strip()
    text = text.replace("**", "")
    text = re.sub(r"(?m)^\s*[*•]\s*", "- ", text)
    text = re.sub(r"(?m)^-\s{2,}", "- ", text)

    match = re.search(r"(HOOK:\s*\n)(.+?)(\n\s*\n|$)", text, flags=re.IGNORECASE | re.DOTALL)
    if match:
        hook = compact_spaces(match.group(2))
        text = text[: match.start(2)] + limit_words(hook, 16) + text[match.end(2) :]
    text = re.sub(r"(?im)^\s*(HOOK|NỘI DUNG|NOI DUNG|CTA|FOOTER)\s*:\s*", "", text)
    text = re.sub(r"(?i)không chỉ là", "không đơn thuần là", text)
    text = re.sub(r"(?i)không chỉ giúp", "giúp", text)
    text = re.sub(r"(?i)không chỉ", "", text)
    text = re.sub(r"(?i)\bmà còn\b", "và", text)
    text = re.sub(r"(?i)không đơn thuần là [^.,\n]+,\s*và là", "là", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    lines = text.splitlines()
    for idx, line in enumerate(lines):
        stripped = line.strip()
        if stripped:
            lines[idx] = line[: len(line) - len(line.lstrip())] + title_case_vietnamese(stripped)
            break
    text = "\n".join(lines)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text


def title_case_vietnamese(text):
    def convert(match):
        word = match.group(0)
        return word[0].upper() + word[1:]

    return re.sub(r"[^\W\d_]+", convert, text, flags=re.UNICODE)


def is_generation_error(text):
    plain = strip_tone(text or "")
    return plain.startswith(("gemini hien khong kha dung", "chua co gemini_api_key", "loi khi tao noi dung"))


def strip_tone(text):
    normalized = unicodedata.normalize("NFD", text or "")
    return "".join(ch for ch in normalized if unicodedata.category(ch) != "Mn").lower()


def is_content_request_plain(plain):
    if any(x in plain for x in ["tao cho toi", "viet cho toi", "viet bai", "tao bai", "tao noi dung", "viet noi dung", "caption", "content"]):
        return True
    return bool(re.search(r"\b(tao|viet)\s+(\d+|mot|moi|cac)?\s*(bai|bai viet|noi dung|caption)\b", plain))


def requested_pillar_id(text):
    match = re.search(r"\bp\s*([1-9])\b", strip_tone(text))
    if not match:
        return ""
    return f"P{match.group(1)}"


def clean_pillar_core(core):
    core = compact_spaces(core)
    quoted = re.findall(r'"([^"]+)"', core)
    if quoted:
        return quoted[-1].strip(' "\'')
    core = re.sub(r"(?i).*core message:\s*", "", core).strip()
    return core.strip(' "\'')


def split_pillar_lines(value, limit=3):
    lines = []
    for raw in re.split(r"\n|•|\d+\.", value or ""):
        item = compact_spaces(raw).strip("- ")
        if item:
            lines.append(item)
        if len(lines) >= limit:
            break
    return lines


def fallback_generate_text(user_text, refresh_config=True):
    config = workspace_config(refresh=refresh_config)
    pillars = config.get("content_pillars_data") or []
    pillar_id = requested_pillar_id(user_text)
    pillar = None
    if pillar_id:
        pillar = next((row for row in pillars if str(row.get("pillar_id", "")).upper() == pillar_id), None)
    if not pillar and pillars:
        pillar = pillars[0]
    if not pillar:
        return "Chưa có dữ liệu Content_Pillars để viết bài fallback. Hãy kiểm tra lại Sheet."

    name = compact_spaces(pillar.get("pillar_name", "Đồng phục doanh nghiệp"))
    core = clean_pillar_core(pillar.get("core_message", ""))
    pain_points = split_pillar_lines(pillar.get("pain_points", ""), 3)

    hook_by_pillar = {
        "P1": "Đồng Phục Thiết Kế Riêng Giúp Doanh Nghiệp Chỉn Chu Hơn",
        "P2": "Chọn Đúng Chất Liệu Giúp Đồng Phục Dễ Mặc Hơn",
        "P3": "Một Bộ Đồng Phục Tốt Cần Được Kiểm Chứng Từ Thực Tế",
        "P4": "Quy Trình Rõ Ràng Giúp Doanh Nghiệp Yên Tâm Hơn",
        "P5": "May Mẫu Miễn Phí Giúp Doanh Nghiệp Giảm Rủi Ro",
        "P6": "Cập Nhật Xu Hướng Giúp Kế Hoạch Đồng Phục Chủ Động Hơn",
    }
    hook = hook_by_pillar.get(str(pillar.get("pillar_id", "")).upper(), f"{name} Cho Doanh Nghiệp")

    body = [title_case_vietnamese(hook), ""]
    if core:
        body.extend([core, ""])
    body.append("Đây là những vấn đề doanh nghiệp thường gặp khi chọn đồng phục:")
    if pain_points:
        body.extend([f"- {item}" for item in pain_points])
    else:
        body.extend(
            [
                "- Chất liệu có phù hợp môi trường làm việc không.",
                "- Phom dáng có giúp người mặc thoải mái không.",
                "- Mẫu thiết kế có giữ được hình ảnh thương hiệu không.",
            ]
        )
    body.extend(["", "Từ đó, doanh nghiệp có thể chọn mẫu phù hợp hơn trước khi đặt số lượng lớn. Cách làm này giúp giảm sai sót, tiết kiệm thời gian và giữ hình ảnh đội ngũ chỉn chu hơn."])
    body.extend(["", config["default_cta"], "", config["default_footer"]])
    return clean_generated_post("\n".join(body))


def generate_content_text(user_text):
    if requested_pillar_id(user_text):
        return fallback_generate_text(user_text, refresh_config=False)
    draft = gemini_generate_text(user_text)
    if is_generation_error(draft):
        return fallback_generate_text(user_text, refresh_config=False)
    return draft


def remember_telegram_send(kind, ok, status_code=None, preview="", error=""):
    TELEGRAM_OUTBOX.append(
        {
            "ts": now_text(),
            "kind": kind,
            "ok": ok,
            "status_code": status_code,
            "preview": preview[:500],
            "error": error[:500],
        }
    )
    del TELEGRAM_OUTBOX[:-30]
    save_state()
    append_bot_event(f"telegram_{kind}", "ok" if ok else "error", preview if ok else error, str(status_code or ""))


def telegram_chunks(text, max_chars=3800):
    text = text or ""
    chunks = []
    remaining = text
    while len(remaining) > max_chars:
        cut = remaining.rfind("\n\n", 0, max_chars)
        if cut < 1200:
            cut = remaining.rfind("\n", 0, max_chars)
        if cut < 1200:
            cut = max_chars
        chunks.append(remaining[:cut].strip())
        remaining = remaining[cut:].strip()
    if remaining:
        chunks.append(remaining)
    return chunks or [""]


def send_telegram(text):
    token = env("TELEGRAM_BOT_TOKEN")
    chat_id = env("TELEGRAM_CHAT_ID")
    for chunk in telegram_chunks(text):
        try:
            res = requests.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                data={"chat_id": chat_id, "text": chunk},
                timeout=20,
            )
            remember_telegram_send("message", res.ok, res.status_code, chunk, "" if res.ok else res.text)
            res.raise_for_status()
        except Exception as exc:
            remember_telegram_send("message", False, None, chunk, str(exc))
            raise


def send_telegram_buttons(text, buttons):
    token = env("TELEGRAM_BOT_TOKEN")
    chat_id = env("TELEGRAM_CHAT_ID")
    chunks = telegram_chunks(text)
    for idx, chunk in enumerate(chunks):
        data = {"chat_id": chat_id, "text": chunk}
        if idx == len(chunks) - 1 and buttons:
            data["reply_markup"] = json.dumps({"inline_keyboard": buttons}, ensure_ascii=False)
        try:
            res = requests.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                data=data,
                timeout=20,
            )
            remember_telegram_send("message_buttons", res.ok, res.status_code, chunk, "" if res.ok else res.text)
            res.raise_for_status()
        except Exception as exc:
            remember_telegram_send("message_buttons", False, None, chunk, str(exc))
            raise


def answer_callback_query(callback_query_id, text=""):
    if not callback_query_id:
        return
    token = env("TELEGRAM_BOT_TOKEN")
    data = {"callback_query_id": callback_query_id}
    if text:
        data["text"] = text[:200]
    try:
        requests.post(f"https://api.telegram.org/bot{token}/answerCallbackQuery", data=data, timeout=10)
    except Exception:
        app.logger.exception("Could not answer callback query")


def draft_action_buttons():
    return [
        [
            {"text": "Tạo ảnh", "callback_data": "draft:create_image"},
            {"text": "Đăng Facebook", "callback_data": "draft:post_facebook"},
        ],
        [
            {"text": "Hủy", "callback_data": "draft:cancel"},
        ],
    ]


def confirm_buttons(code):
    return [
        [
            {"text": "Xác nhận", "callback_data": f"confirm:{code}"},
            {"text": "Hủy", "callback_data": "draft:cancel"},
        ]
    ]


def send_telegram_photo(image_bytes, caption=""):
    token = env("TELEGRAM_BOT_TOKEN")
    chat_id = env("TELEGRAM_CHAT_ID")
    files = {"photo": ("image.png", io.BytesIO(image_bytes), "image/png")}
    data = {"chat_id": chat_id}
    if caption:
        data["caption"] = caption[:1000]
    try:
        res = requests.post(
            f"https://api.telegram.org/bot{token}/sendPhoto",
            data=data,
            files=files,
            timeout=45,
        )
        remember_telegram_send("photo", res.ok, res.status_code, caption, "" if res.ok else res.text)
        res.raise_for_status()
    except Exception as exc:
        remember_telegram_send("photo", False, None, caption, str(exc))
        raise


def run_background(name, target, *args, **kwargs):
    thread = threading.Thread(target=target, args=args, kwargs=kwargs, name=name, daemon=True)
    thread.start()
    return thread


def send_telegram_async(text):
    return run_background("telegram-send-message", send_telegram, text)


def send_telegram_buttons_async(text, buttons):
    return run_background("telegram-send-buttons", send_telegram_buttons, text, buttons)


def answer_callback_query_async(callback_query_id, text=""):
    return run_background("telegram-answer-callback", answer_callback_query, callback_query_id, text)


def process_callback_and_send(data):
    try:
        result = handle_callback(data)
        if result.get("buttons"):
            send_telegram_buttons(result.get("text", ""), result.get("buttons"))
        else:
            send_telegram(result.get("text", ""))
    except Exception as exc:
        app.logger.exception("Could not process callback in background")
        send_telegram(f"Lỗi khi xử lý nút: {exc}")


def process_callback_async(data):
    return run_background("telegram-callback", process_callback_and_send, data)


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


def extract_urls(text):
    return re.findall(r"https?://[^\s<>\"]+", text or "")


def looks_like_image_url(url):
    lowered = (url or "").lower()
    return (
        "drive.google.com" in lowered
        or "docs.google.com" in lowered
        or lowered.endswith((".png", ".jpg", ".jpeg", ".webp"))
    )


def looks_like_video_url(url):
    lowered = (url or "").lower()
    return (
        "drive.google.com" in lowered
        or "docs.google.com" in lowered
        or lowered.endswith((".mp4", ".mov", ".m4v", ".webm"))
    )


def explicit_media_type(value):
    plain = strip_tone(str(value or "")).strip().lower()
    if plain in ["image", "anh", "hinh", "photo", "picture"]:
        return "image"
    if plain in ["video", "clip", "reel", "short"]:
        return "video"
    return ""


def image_file_url(url):
    return (url or "").lower().split("?", 1)[0].endswith((".png", ".jpg", ".jpeg", ".webp"))


def video_file_url(url):
    return (url or "").lower().split("?", 1)[0].endswith((".mp4", ".mov", ".m4v", ".webm"))


def video_mimetype_from_url(url, content_type=""):
    content_type = (content_type or "").split(";", 1)[0].strip().lower()
    if content_type.startswith("video/"):
        return content_type
    lowered = (url or "").lower()
    if lowered.endswith(".mov"):
        return "video/quicktime"
    if lowered.endswith(".webm"):
        return "video/webm"
    return "video/mp4"


def max_video_bytes():
    raw = os.environ.get("MAX_SOCIAL_VIDEO_MB", "50")
    try:
        mb = max(1, int(raw))
    except Exception:
        mb = 50
    return mb * 1024 * 1024


def normalize_image_bytes(image_bytes, max_width=1600, max_height=2000):
    image = Image.open(io.BytesIO(image_bytes))
    image.thumbnail((max_width, max_height), Image.LANCZOS)
    out = io.BytesIO()
    if image.mode not in ["RGB", "RGBA"]:
        image = image.convert("RGB")
    if image.mode == "RGBA":
        bg = Image.new("RGB", image.size, (255, 255, 255))
        bg.paste(image, mask=image.getchannel("A"))
        image = bg
    image.save(out, format="PNG", optimize=True)
    return out.getvalue()


def download_image_from_url(url):
    if not looks_like_image_url(url):
        raise RuntimeError("Link không giống link ảnh hoặc link Google Drive.")
    download_url = google_drive_download_url(url)
    res = requests.get(download_url, timeout=45)
    res.raise_for_status()
    content_type = (res.headers.get("Content-Type") or "").lower()
    if "text/html" in content_type and "drive.google.com" in download_url:
        raise RuntimeError("Google Drive chưa cho tải trực tiếp. Hãy bật quyền Anyone with the link can view.")
    return apply_brand_logo_overlay(normalize_image_bytes(res.content))


def download_video_from_url(url):
    if not looks_like_video_url(url):
        raise RuntimeError("Link không giống link video hoặc link Google Drive.")
    download_url = google_drive_download_url(url)
    limit = max_video_bytes()
    res = requests.get(download_url, stream=True, timeout=120)
    res.raise_for_status()
    content_type = (res.headers.get("Content-Type") or "").lower()
    if "text/html" in content_type and "drive.google.com" in download_url:
        raise RuntimeError("Google Drive chưa cho tải video trực tiếp. Hãy bật quyền Anyone with the link can view.")
    content_length = res.headers.get("Content-Length")
    if content_length and int(content_length) > limit:
        raise RuntimeError(f"Video lớn hơn giới hạn {limit // 1024 // 1024}MB.")
    chunks = []
    total = 0
    for chunk in res.iter_content(chunk_size=1024 * 1024):
        if not chunk:
            continue
        total += len(chunk)
        if total > limit:
            raise RuntimeError(f"Video lớn hơn giới hạn {limit // 1024 // 1024}MB.")
        chunks.append(chunk)
    return b"".join(chunks), video_mimetype_from_url(url, content_type)


def attach_image_to_draft(chat_key, image_url, source="manual_link", async_sheet=True):
    draft = restore_last_draft(chat_key)
    if not draft.get("text"):
        raise RuntimeError("Chưa có bài nháp nào để gắn ảnh. Hãy tạo bài trước.")
    image_bytes = download_image_from_url(image_url)
    draft.pop("image_b64", None)
    draft.pop("video_url", None)
    draft.pop("video_status", None)
    draft["image_url"] = image_url
    draft["media_url"] = image_url
    draft["media_type"] = "image"
    draft["image_status"] = "image_ready"
    draft["image_source"] = source
    draft["media_source"] = source
    remember_last_draft(chat_key, draft, async_write=async_sheet)
    append_content_record(
        topic=f"{source}: {image_url}",
        draft_text=draft.get("text", ""),
        image_prompt=draft.get("image_prompt", ""),
        stage="image_ready",
        status="needs_review",
        async_write=async_sheet,
    )
    return image_bytes, draft


def attach_video_to_draft(chat_key, video_url, source="manual_link", async_sheet=True):
    draft = restore_last_draft(chat_key)
    if not draft.get("text"):
        raise RuntimeError("Chưa có bài nháp nào để gắn video. Hãy tạo bài trước.")
    draft.pop("image_b64", None)
    draft.pop("image_url", None)
    draft.pop("image_status", None)
    draft["video_url"] = video_url
    draft["media_url"] = video_url
    draft["media_type"] = "video"
    draft["video_status"] = "video_ready"
    draft["media_source"] = source
    remember_last_draft(chat_key, draft, async_write=async_sheet)
    append_content_record(
        topic=f"{source}: {video_url}",
        draft_text=draft.get("text", ""),
        image_prompt=draft.get("image_prompt", ""),
        stage="video_ready",
        status="needs_review",
        async_write=async_sheet,
    )
    return draft


def find_image_url_in_content_sheet(draft):
    try:
        payload = google_sheets_batch_get(["Content!A1:Z500"])
        values = range_values_map(payload).get("Content", [])
        rows = parse_table(values)
    except Exception:
        app.logger.exception("Could not read Content sheet for image URL")
        return ""
    if not rows:
        return ""

    content_id = str((draft or {}).get("content_id", "")).strip()
    target = None
    if content_id:
        for row in rows:
            row_ids = [
                str(row.get(key, "")).strip()
                for key in ["content_id", "record_id", "id", "Content ID", "Content_ID"]
            ]
            if content_id in row_ids:
                target = row
                break
    if not target:
        target = rows[-1]

    preferred = ["image_url", "image_link", "media_url", "media_link", "drive_link", "manual_image_url", "Ảnh", "Link ảnh"]

    def find_in_row(row):
        if not row:
            return ""
        for key in preferred:
            value = str(row.get(key, "")).strip()
            if value:
                for url in extract_urls(value) or [value]:
                    if looks_like_image_url(url):
                        return url
        for value in row.values():
            for url in extract_urls(str(value)):
                if looks_like_image_url(url):
                    return url
        return ""

    found = find_in_row(target)
    if found:
        return found

    for row in reversed(rows):
        found = find_in_row(row)
        if found:
            return found

    raw_rows = values[1:] if len(values) > 1 else values
    for raw in reversed(raw_rows):
        for value in raw:
            for url in extract_urls(str(value)):
                if looks_like_image_url(url):
                    return url

    for key in preferred:
        value = str(target.get(key, "")).strip()
        if value:
            for url in extract_urls(value) or [value]:
                if looks_like_image_url(url):
                    return url

    for value in target.values():
        for url in extract_urls(str(value)):
            if looks_like_image_url(url):
                return url
    return ""


def find_image_url_in_content_sheet(draft):
    try:
        payload = google_sheets_batch_get(["Content!A1:Z500"])
        values = range_values_map(payload).get("Content", [])
        rows = parse_table(values)
    except Exception:
        app.logger.exception("Could not read Content sheet for image URL")
        return ""
    if not rows:
        return ""

    content_id = str((draft or {}).get("content_id", "")).strip()
    target = None
    if content_id:
        for row in rows:
            row_ids = [
                str(row.get(key, "")).strip()
                for key in ["content_id", "record_id", "id", "Content ID", "Content_ID"]
            ]
            if content_id in row_ids:
                target = row
                break
    if not target:
        target = rows[-1]

    preferred = ["image_url", "image_link", "media_url", "media_link", "drive_link", "manual_image_url", "Anh", "Link anh", "Ảnh", "Link ảnh"]

    def find_in_row(row):
        if not row:
            return ""
        media_type = explicit_media_type(row.get("media_type") or row.get("Media Type") or row.get("Loại media"))
        stage_plain = strip_tone(str(row.get("stage") or row.get("Stage") or row.get("Trạng thái") or "")).lower()
        if not media_type and ("image" in stage_plain or "anh" in stage_plain):
            media_type = "image"
        if not media_type and "video" in stage_plain:
            media_type = "video"
        if media_type == "video":
            return ""
        for key in preferred:
            key_plain = strip_tone(str(key))
            if "video" in key_plain:
                continue
            value = str(row.get(key, "")).strip()
            if not value:
                continue
            for url in extract_urls(value) or [value]:
                if image_file_url(url) or media_type == "image" or "image" in key_plain or "anh" in key_plain or "hinh" in key_plain:
                    return url
        for key, value in row.items():
            key_plain = strip_tone(str(key))
            if "video" in key_plain:
                continue
            for url in extract_urls(str(value)):
                if image_file_url(url) or (media_type == "image" and looks_like_image_url(url)):
                    return url
        return ""

    found = find_in_row(target)
    if found:
        return found

    for row in reversed(rows):
        found = find_in_row(row)
        if found:
            return found

    raw_rows = values[1:] if len(values) > 1 else values
    for raw in reversed(raw_rows):
        for value in raw:
            for url in extract_urls(str(value)):
                if image_file_url(url):
                    return url
    return ""


def attach_image_from_content_sheet(chat_key, async_sheet=True):
    draft = restore_last_draft(chat_key)
    image_url = find_image_url_in_content_sheet(draft)
    if not image_url:
        raise RuntimeError("Chưa thấy link ảnh Drive trong dòng Content trên Sheet.")
    return attach_image_to_draft(chat_key, image_url, source="content_sheet", async_sheet=async_sheet)


def find_video_url_in_content_sheet(draft):
    try:
        payload = google_sheets_batch_get(["Content!A1:Z500"])
        values = range_values_map(payload).get("Content", [])
        rows = parse_table(values)
    except Exception:
        app.logger.exception("Could not read Content sheet for video URL")
        return ""
    if not rows:
        return ""

    content_id = str((draft or {}).get("content_id", "")).strip()
    target = None
    if content_id:
        for row in rows:
            row_ids = [
                str(row.get(key, "")).strip()
                for key in ["content_id", "record_id", "id", "Content ID", "Content_ID"]
            ]
            if content_id in row_ids:
                target = row
                break
    if not target:
        target = rows[-1]

    preferred = ["video_url", "video_link", "media_url", "media_link", "drive_link", "manual_video_url", "Video", "Link video"]

    def find_in_row(row):
        if not row:
            return ""
        for key in preferred:
            value = str(row.get(key, "")).strip()
            if value:
                for url in extract_urls(value) or [value]:
                    if looks_like_video_url(url):
                        return url
        for key, value in row.items():
            if "video" not in strip_tone(str(key)):
                continue
            for url in extract_urls(str(value)):
                if looks_like_video_url(url):
                    return url
        return ""

    found = find_in_row(target)
    if found:
        return found

    for row in reversed(rows):
        found = find_in_row(row)
        if found:
            return found

    raw_rows = values[1:] if len(values) > 1 else values
    for raw in reversed(raw_rows):
        for value in raw:
            for url in extract_urls(str(value)):
                if url.lower().endswith((".mp4", ".mov", ".m4v", ".webm")):
                    return url
    return ""


def find_video_url_in_content_sheet(draft):
    try:
        payload = google_sheets_batch_get(["Content!A1:Z500"])
        values = range_values_map(payload).get("Content", [])
        rows = parse_table(values)
    except Exception:
        app.logger.exception("Could not read Content sheet for video URL")
        return ""
    if not rows:
        return ""

    content_id = str((draft or {}).get("content_id", "")).strip()
    target = None
    if content_id:
        for row in rows:
            row_ids = [
                str(row.get(key, "")).strip()
                for key in ["content_id", "record_id", "id", "Content ID", "Content_ID"]
            ]
            if content_id in row_ids:
                target = row
                break
    if not target:
        target = rows[-1]

    preferred = ["video_url", "video_link", "media_url", "media_link", "drive_link", "manual_video_url", "Video", "Link video"]

    def find_in_row(row):
        if not row:
            return ""
        media_type = explicit_media_type(row.get("media_type") or row.get("Media Type") or row.get("Loại media"))
        stage_plain = strip_tone(str(row.get("stage") or row.get("Stage") or row.get("Trạng thái") or "")).lower()
        if not media_type and "video" in stage_plain:
            media_type = "video"
        if not media_type and ("image" in stage_plain or "anh" in stage_plain):
            media_type = "image"
        if media_type == "image":
            return ""
        for key in preferred:
            key_plain = strip_tone(str(key))
            value = str(row.get(key, "")).strip()
            if not value:
                continue
            for url in extract_urls(value) or [value]:
                if video_file_url(url) or media_type == "video" or "video" in key_plain:
                    return url
        for key, value in row.items():
            if "video" not in strip_tone(str(key)):
                continue
            for url in extract_urls(str(value)):
                if video_file_url(url) or ((media_type == "video" or "video" in strip_tone(str(key))) and looks_like_video_url(url)):
                    return url
        return ""

    found = find_in_row(target)
    if found:
        return found

    for row in reversed(rows):
        found = find_in_row(row)
        if found:
            return found

    raw_rows = values[1:] if len(values) > 1 else values
    for raw in reversed(raw_rows):
        for value in raw:
            for url in extract_urls(str(value)):
                if video_file_url(url):
                    return url
    return ""


def attach_video_from_content_sheet(chat_key, async_sheet=True):
    draft = restore_last_draft(chat_key)
    video_url = find_video_url_in_content_sheet(draft)
    if not video_url:
        raise RuntimeError("Chưa thấy link video Drive trong dòng Content trên Sheet.")
    return attach_video_to_draft(chat_key, video_url, source="content_sheet", async_sheet=async_sheet)


def download_brand_logo():
    logo_url = workspace_config(refresh=False).get("brand_logo_url", "").strip()
    if not logo_url:
        return None
    res = requests.get(google_drive_download_url(logo_url), timeout=8)
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


def font_for_image(size):
    candidates = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "C:/Windows/Fonts/arial.ttf",
    ]
    for path in candidates:
        try:
            return ImageFont.truetype(path, size)
        except Exception:
            continue
    return ImageFont.load_default()


def mock_generate_image(prompt):
    hook_match = re.search(r'Câu Tiêu đề/HOOK trên ảnh:\s*"([^"]+)"', prompt or "")
    hook = hook_match.group(1) if hook_match else "Đồng phục chuẩn, doanh nghiệp chuyên nghiệp"
    hook = limit_words(hook, 8)

    width, height = 1080, 1350
    img = Image.new("RGB", (width, height), (24, 24, 24))
    draw = ImageDraw.Draw(img)

    for y in range(height):
        shade = int(24 + (y / height) * 28)
        draw.line([(0, y), (width, y)], fill=(shade, shade, shade))

    gold = (212, 168, 72)
    white = (248, 248, 244)
    soft = (235, 225, 205)
    draw.rectangle([0, 0, width, 220], fill=white)
    draw.rectangle([0, 220, width, 234], fill=gold)
    draw.rounded_rectangle([70, 310, 1010, 1020], radius=28, fill=(38, 38, 38), outline=gold, width=5)
    draw.rectangle([120, 760, 960, 940], outline=(86, 86, 86), width=3)
    draw.ellipse([430, 420, 650, 640], fill=(70, 70, 70), outline=gold, width=4)
    draw.rounded_rectangle([340, 630, 740, 920], radius=36, fill=(58, 58, 58), outline=soft, width=3)
    draw.line([340, 700, 740, 700], fill=gold, width=6)
    draw.line([430, 630, 430, 920], fill=(96, 96, 96), width=4)
    draw.line([650, 630, 650, 920], fill=(96, 96, 96), width=4)

    title_font = font_for_image(54)
    note_font = font_for_image(28)
    draw.text((80, 1085), hook, font=title_font, fill=white)
    draw.text((80, 1165), "Ảnh test workflow - sẽ thay bằng ảnh AI thật khi có API ảnh.", font=note_font, fill=(200, 200, 200))
    draw.text((80, 1210), "Tone: vàng kim, đen, trắng. Khung 4:5.", font=note_font, fill=(200, 200, 200))

    out = io.BytesIO()
    img.save(out, format="PNG", optimize=True)
    return out.getvalue()


def generate_image(prompt):
    provider = image_provider()
    if provider == "mock":
        image_bytes = mock_generate_image(prompt)
    elif provider == "gemini":
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
        timeout=max(5, min(25, int(os.environ.get("COMPOSIO_EXECUTE_TIMEOUT_SECONDS", "25")))),
    )
    if not res.ok:
        raise RuntimeError(res.text)
    payload = res.json()
    if composio_payload_failed(payload):
        raise RuntimeError(json.dumps(payload, ensure_ascii=False)[:1500])
    return payload


def composio_payload_failed(payload):
    if not isinstance(payload, dict):
        return False
    failure_keys = ["error", "errors", "exception", "traceback"]
    for key in failure_keys:
        if payload.get(key):
            return True
    for key in ["successful", "success", "ok"]:
        if key in payload and payload.get(key) is False:
            return True
    data = payload.get("data")
    if isinstance(data, dict):
        for key in failure_keys:
            if data.get(key):
                return True
        for key in ["successful", "success", "ok"]:
            if key in data and data.get(key) is False:
                return True
    return False


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


def bot_state_key(name):
    return f"bot_state:{name}"


def durable_state_value(value):
    if isinstance(value, dict):
        cleaned = {}
        allowed = {
            "text",
            "content_id",
            "image_url",
            "video_url",
            "media_url",
            "media_type",
            "image_status",
            "video_status",
            "image_source",
            "media_source",
            "image_prompt",
            "platform",
            "type",
            "expires",
            "running",
            "last_error",
            "status",
            "entity",
            "id",
        }
        for key, item in value.items():
            if isinstance(item, dict):
                cleaned[key] = {k: v for k, v in item.items() if k in allowed and isinstance(v, (str, int, float, bool))}
            else:
                cleaned[key] = item
        return cleaned
    return value


def bot_state_cell(name):
    return {"last_draft": "A2", "pending": "A3"}.get(name, "A20")


def write_bot_state(name, value, async_write=True):
    state_value = durable_state_value(value or {})
    row = [bot_state_key(name), json.dumps(state_value, ensure_ascii=False), now_text(), "1"]

    def write_row():
        google_sheets_write("Bot_State", [row], bot_state_cell(name))

    if async_write:
        run_background("bot-state-write", write_row)
        return None
    try:
        write_row()
        return None
    except Exception as exc:
        app.logger.exception("Could not write bot state")
        return str(exc)


def read_bot_state(name):
    try:
        payload = google_sheets_batch_get(["Bot_State!A1:D20"])
        rows = parse_table(range_values_map(payload).get("Bot_State", []))
        wanted = bot_state_key(name)
        for row in rows:
            if str(row.get("key", "")).strip() == wanted:
                raw = row.get("value_json", "")
                return json.loads(raw) if raw else {}
    except Exception:
        app.logger.exception("Could not read bot state")
    return {}


def remember_last_draft(chat_key, draft, async_write=True):
    LAST_DRAFT[chat_key] = draft or {}
    save_state()
    write_bot_state("last_draft", LAST_DRAFT, async_write=async_write)


def remember_pending_state(async_write=True):
    save_state()
    write_bot_state("pending", PENDING, async_write=async_write)


def restore_pending_state():
    if PENDING:
        return PENDING
    stored = read_bot_state("pending")
    if isinstance(stored, dict) and stored:
        PENDING.update(stored)
        save_state()
    return PENDING


def restore_last_draft(chat_key):
    draft = normalize_draft(LAST_DRAFT.get(chat_key))
    if draft:
        return draft
    stored = read_bot_state("last_draft")
    if isinstance(stored, dict) and stored:
        LAST_DRAFT.update(stored)
        save_state()
    return normalize_draft(LAST_DRAFT.get(chat_key))


def now_text():
    return time.strftime("%Y-%m-%d %H:%M:%S")


def new_record_id(prefix):
    return f"{prefix}_{int(time.time())}_{random.randint(1000, 9999)}"


def append_bot_event(event_type, status="ok", detail="", ref_id="", async_write=True):
    row = [new_record_id("event"), now_text(), event_type, status, ref_id, str(detail)[:3000]]

    def write_event():
        try:
            google_sheets_append("Bot_Events", [row])
        except Exception:
            app.logger.exception("Could not append bot event")

    if async_write:
        run_background("bot-event-append", write_event)
        return None
    try:
        write_event()
        return None
    except Exception as exc:
        return str(exc)


def enqueue_task(task_type, payload, source_update_id="", chat_id="", async_write=True):
    task_id = new_record_id("task")
    row = [
        task_id,
        source_update_id,
        str(chat_id),
        task_type,
        json.dumps(payload or {}, ensure_ascii=False)[:10000],
        "queued",
        "",
        "0",
        "",
        "",
        now_text(),
        now_text(),
    ]

    def write_task():
        try:
            google_sheets_append("Task_Queue", [row])
            append_bot_event("task_queued", "ok", task_type, task_id)
        except Exception:
            app.logger.exception("Could not enqueue task")

    if async_write:
        run_background("task-queue-append", write_task)
        return task_id, None
    try:
        write_task()
        return task_id, None
    except Exception as exc:
        return task_id, str(exc)


def append_content_record(topic, draft_text="", image_prompt="", stage="draft", status="needs_review", platform="facebook", async_write=False):
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
    if async_write:
        def write_later():
            try:
                google_sheets_append("Content", [row])
            except Exception:
                app.logger.exception("Could not append content record async")

        threading.Thread(target=write_later, name="content-sheet-append", daemon=True).start()
        return record_id, None
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


CONTENT_COLUMN_ALIASES = {
    "content_id": ["content_id", "record_id", "id", "content id", "ma bai", "ma noi dung"],
    "scheduled_at": ["scheduled_at", "schedule_at", "publish_at", "datetime", "lich_dang", "thoi_gian_dang"],
    "scheduled_date": ["scheduled_date", "schedule_date", "date", "ngay", "ngay_dang", "ngay_len_lich"],
    "scheduled_time": ["scheduled_time", "schedule_time", "time", "post_time", "scheduled_hour", "gio", "gio_dang", "khung_gio"],
    "platform": ["platform", "nen_tang", "kenh"],
    "topic": ["topic", "chu_de", "title", "tieu_de", "pillar", "content_pillar"],
    "draft_text": ["draft_text", "content", "noi_dung", "caption", "post_text", "bai_viet"],
    "image_prompt": ["image_prompt", "prompt_anh", "prompt_tao_anh"],
    "media_type": ["media_type", "loai_media", "loai_file", "type"],
    "media_url": ["media_url", "media_link", "drive_link", "link_media", "link_file"],
    "image_url": ["image_url", "image_link", "anh", "link_anh", "hinh", "link_hinh"],
    "video_url": ["video_url", "video_link", "video", "link_video"],
    "stage": ["stage", "giai_doan"],
    "status": ["status", "trang_thai", "approval_status"],
    "posted_at": ["posted_at", "posted_time", "thoi_gian_da_dang"],
    "post_url": ["post_url", "posted_url", "link_bai_dang", "url_bai_dang"],
    "result_preview": ["result_preview", "ket_qua", "composio_result"],
    "last_error": ["last_error", "error", "loi"],
    "updated_at": ["updated_at", "cap_nhat_luc"],
}


def content_header_index(headers, canonical):
    aliases = {normalize_header_name(x) for x in CONTENT_COLUMN_ALIASES.get(canonical, [canonical])}
    for idx, header in enumerate(headers or []):
        if normalize_header_name(header) in aliases:
            return idx
    return None


def content_row_value(row, headers, canonical, default=""):
    idx = content_header_index(headers, canonical)
    if idx is None or idx >= len(row):
        return default
    return str(row[idx] or "").strip()


def read_content_sheet():
    payload = google_sheets_batch_get(["Content!A1:AZ500"])
    values = range_values_map(payload).get("Content", [])
    headers = [str(x).strip() for x in values[0]] if values else []
    rows = values[1:] if len(values) > 1 else []
    return headers, rows


def write_content_row_cells(row_number, headers, updates):
    written = {}
    missing = []
    for canonical, value in updates.items():
        idx = content_header_index(headers, canonical)
        if idx is None:
            missing.append(canonical)
            continue
        google_sheets_write("Content", [[value]], f"{column_letter(idx)}{row_number}")
        written[canonical] = value
    return {"written": written, "missing": missing}


def update_content_by_id(content_id, updates):
    if not content_id:
        return {"ok": False, "error": "missing content_id"}
    headers, rows = read_content_sheet()
    for offset, row in enumerate(rows, start=2):
        if content_row_value(row, headers, "content_id") == str(content_id):
            result = write_content_row_cells(offset, headers, updates)
            return {"ok": True, "row": offset, **result}
    return {"ok": False, "error": f"content_id not found: {content_id}"}


def append_posting_log(content_id, platform, media_type, status, result="", error=""):
    row = [
        new_record_id("post"),
        str(content_id or ""),
        platform,
        media_type,
        status,
        json.dumps(result, ensure_ascii=False)[:5000] if not isinstance(result, str) else result[:5000],
        str(error or "")[:1000],
        now_text(),
    ]
    try:
        google_sheets_append("Posting_Log", [row])
        return None
    except Exception as exc:
        app.logger.exception("Could not append posting log")
        return str(exc)


def find_first_url_deep(value):
    if isinstance(value, str):
        urls = extract_urls(value)
        return urls[0] if urls else ""
    if isinstance(value, dict):
        for key in ["post_url", "posted_url", "permalink_url", "url", "link"]:
            found = find_first_url_deep(value.get(key))
            if found:
                return found
        for item in value.values():
            found = find_first_url_deep(item)
            if found:
                return found
    if isinstance(value, list):
        for item in value:
            found = find_first_url_deep(item)
            if found:
                return found
    return ""


def bangkok_now():
    return datetime.utcnow() + timedelta(hours=7)


def parse_scheduled_at(value):
    raw = str(value or "").strip()
    if not raw:
        return None


def scheduled_at_from_row(row, headers):
    raw = content_row_value(row, headers, "scheduled_at")
    if raw:
        return raw, parse_scheduled_at(raw)
    scheduled_date = content_row_value(row, headers, "scheduled_date")
    scheduled_time = content_row_value(row, headers, "scheduled_time")
    if scheduled_date and scheduled_time:
        raw = f"{scheduled_date} {scheduled_time}"
        return raw, parse_scheduled_at(raw)
    if scheduled_date:
        return scheduled_date, parse_scheduled_at(scheduled_date)
    return "", None
    cleaned = compact_spaces(raw.replace("T", " ").replace("Z", "").replace("\u00a0", " ")).strip().strip("'\"")
    cleaned = re.sub(r"\s+[+-]\d{2}:?\d{2}$", "", cleaned)
    formats = [
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y-%m-%d",
        "%d/%m/%Y %H:%M:%S",
        "%d/%m/%Y %H:%M",
        "%d/%m/%Y",
    ]
    for fmt in formats:
        try:
            return datetime.strptime(cleaned, fmt)
        except ValueError:
            pass
    match = re.search(r"(\d{4})\D+(\d{1,2})\D+(\d{1,2})(?:\D+(\d{1,2})\D+(\d{1,2})(?:\D+(\d{1,2}))?)?", cleaned)
    if match:
        year, month, day = [int(match.group(i)) for i in range(1, 4)]
        hour = int(match.group(4) or 0)
        minute = int(match.group(5) or 0)
        second = int(match.group(6) or 0)
        try:
            return datetime(year, month, day, hour, minute, second)
        except ValueError:
            return None
    try:
        return datetime.fromisoformat(cleaned)
    except ValueError:
        return None


def scheduler_status_plain(value):
    return strip_tone(str(value or "")).strip().replace(" ", "_")


def is_scheduler_ready_status(value):
    plain = scheduler_status_plain(value)
    return plain in {
        "scheduled",
        "approved",
        "ready",
        "ready_to_post",
        "duyet",
        "da_duyet",
        "cho_dang",
        "waiting_schedule",
    }


def scheduled_media_from_row(row, headers):
    media_type = explicit_media_type(content_row_value(row, headers, "media_type"))
    image_url = content_row_value(row, headers, "image_url")
    video_url = content_row_value(row, headers, "video_url")
    media_url = content_row_value(row, headers, "media_url")
    if not media_type:
        if video_url or video_file_url(media_url):
            media_type = "video"
        elif image_url or image_file_url(media_url):
            media_type = "image"
    if media_type == "video" and not video_url:
        video_url = media_url
    if media_type == "image" and not image_url:
        image_url = media_url
    return media_type or "text", image_url, video_url


def pending_code():
    for _ in range(20):
        code = str(random.randint(100000, 999999))
        if code not in PENDING:
            return code
    return new_record_id("confirm")


def process_due_content(limit=5, dry_run=True, auto_post=False):
    headers, rows = read_content_sheet()
    has_schedule = content_header_index(headers, "scheduled_at") is not None or content_header_index(headers, "scheduled_date") is not None
    missing_required = [key for key in ["status", "draft_text"] if content_header_index(headers, key) is None]
    if not has_schedule:
        missing_required.append("scheduled_at_or_scheduled_date")
    if missing_required:
        return {"ok": False, "error": "Content sheet thiếu cột bắt buộc.", "missing": missing_required}

    now_dt = bangkok_now()
    due = []
    skipped = 0
    processed = []
    errors = []
    for offset, row in enumerate(rows, start=2):
        status = content_row_value(row, headers, "status")
        if not is_scheduler_ready_status(status):
            skipped += 1
            continue
        scheduled_at_raw, scheduled_at = scheduled_at_from_row(row, headers)
        if not scheduled_at:
            errors.append({"row": offset, "error": "scheduled_at không đọc được", "value": scheduled_at_raw})
            continue
        if scheduled_at > now_dt:
            skipped += 1
            continue
        due.append((offset, row, scheduled_at))
        if len(due) >= limit:
            break

    if dry_run:
        return {
            "ok": True,
            "dry_run": True,
            "now_bangkok": now_dt.strftime("%Y-%m-%d %H:%M:%S"),
            "due_count": len(due),
            "skipped": skipped,
            "errors": errors[:10],
            "due": [
                {
                    "row": row_number,
                    "content_id": content_row_value(row, headers, "content_id"),
                    "scheduled_at": scheduled_at.strftime("%Y-%m-%d %H:%M:%S"),
                    "status": content_row_value(row, headers, "status"),
                    "platform": content_row_value(row, headers, "platform", "Facebook") or "Facebook",
                    "media_type": scheduled_media_from_row(row, headers)[0],
                    "preview": preview_text(content_row_value(row, headers, "draft_text"), 180),
                }
                for row_number, row, scheduled_at in due
            ],
        }

    for row_number, row, scheduled_at in due:
        content_id = content_row_value(row, headers, "content_id") or new_record_id("content")
        platform = content_row_value(row, headers, "platform", "Facebook") or "Facebook"
        draft_text = content_row_value(row, headers, "draft_text")
        topic = content_row_value(row, headers, "topic")
        media_type, image_url, video_url = scheduled_media_from_row(row, headers)
        try:
            if not draft_text:
                draft_text = generate_content_text(topic or "Tạo 1 bài viết P1")
                write_content_row_cells(row_number, headers, {"draft_text": draft_text, "updated_at": now_text()})
            if auto_post:
                result = post_to_social(platform, draft_text, image_url=image_url, video_url=video_url)
                post_url = find_first_url_deep(result)
                write_content_row_cells(
                    row_number,
                    headers,
                    {
                        "status": "posted",
                        "stage": "posted",
                        "posted_at": now_text(),
                        "post_url": post_url,
                        "result_preview": json.dumps(result, ensure_ascii=False)[:1000],
                        "updated_at": now_text(),
                    },
                )
                append_posting_log(content_id, platform, media_type, "posted", result)
                append_bot_event("scheduler_posted", "ok", content_id, str(row_number))
                processed.append({"row": row_number, "content_id": content_id, "status": "posted"})
            else:
                code = add_pending_social(platform, draft_text, None, image_url, content_id, video_url, media_type)
                media_note = "kèm video" if video_url else "kèm ảnh" if image_url else "text-only"
                send_telegram_buttons(
                    (
                        f"Bài đã đến lịch đăng.\n"
                        f"Content ID: {content_id}\n"
                        f"Nền tảng: {platform}\n"
                        f"Media: {media_note}\n"
                        f"Lịch: {scheduled_at.strftime('%Y-%m-%d %H:%M')}\n\n"
                        f"{preview_text(draft_text, 1200)}\n\n"
                        f"Mã xác nhận: {code}\nMã hết hạn sau 15 phút."
                    ),
                    confirm_buttons(code),
                )
                write_content_row_cells(
                    row_number,
                    headers,
                    {"status": "pending_confirm", "stage": "waiting_approval", "result_preview": f"CONFIRM {code}", "updated_at": now_text()},
                )
                append_posting_log(content_id, platform, media_type, "pending_confirm", f"CONFIRM {code}")
                append_bot_event("scheduler_pending_confirm", "ok", content_id, str(row_number))
                processed.append({"row": row_number, "content_id": content_id, "status": "pending_confirm", "code": code})
        except Exception as exc:
            err = str(exc)[:500]
            write_content_row_cells(row_number, headers, {"status": "failed", "last_error": err, "updated_at": now_text()})
            append_posting_log(content_id, platform, media_type, "failed", "", err)
            append_bot_event("scheduler_failed", "error", err, str(row_number))
            errors.append({"row": row_number, "content_id": content_id, "error": err})

    return {
        "ok": not errors,
        "dry_run": False,
        "auto_post": auto_post,
        "processed": processed,
        "errors": errors,
        "skipped": skipped,
        "now_bangkok": now_dt.strftime("%Y-%m-%d %H:%M:%S"),
    }


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


def post_to_social(platform, text, image_b64=None, image_url=None, video_url=None):
    platform_key = strip_tone(platform).upper()
    if "FACEBOOK" in platform_key:
        image_bytes = None
        video_bytes = None
        video_mimetype = "video/mp4"
        if video_url:
            action_id = os.environ.get("COMPOSIO_FACEBOOK_VIDEO_ACTION_ID")
            if not action_id:
                raise RuntimeError("Thiếu COMPOSIO_FACEBOOK_VIDEO_ACTION_ID để đăng video Facebook qua Composio.")
            video_bytes, video_mimetype = download_video_from_url(video_url)
            video = composio_upload_file(video_bytes, "telegram-post-video.mp4", video_mimetype, "facebook", action_id)
            default_payload = {
                "page_id": env("COMPOSIO_FACEBOOK_PAGE_ID"),
                "description": text,
                "video": video,
                "published": True,
            }
            payload_json = os.environ.get("COMPOSIO_FACEBOOK_VIDEO_INPUT_JSON")
        elif image_b64:
            image_bytes = base64.b64decode(image_b64)
        elif image_url:
            image_bytes = download_image_from_url(image_url)
        if not video_url and image_bytes:
            action_id = os.environ.get("COMPOSIO_FACEBOOK_PHOTO_ACTION_ID", "FACEBOOK_CREATE_PHOTO_POST")
            photo = composio_upload_file(image_bytes, "telegram-post.png", "image/png", "facebook", action_id)
            default_payload = {
                "page_id": env("COMPOSIO_FACEBOOK_PAGE_ID"),
                "message": text,
                "photo": photo,
                "published": True,
            }
            payload_json = os.environ.get("COMPOSIO_FACEBOOK_PHOTO_INPUT_JSON")
        elif not video_url:
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
        if "FACEBOOK" in platform_key and video_url:
            payload.setdefault("video", video)
            payload.setdefault("description", text)
            if env("COMPOSIO_FACEBOOK_PAGE_ID"):
                payload.setdefault("page_id", env("COMPOSIO_FACEBOOK_PAGE_ID"))
        if "FACEBOOK" in platform_key and image_bytes:
            payload.setdefault("photo", photo)
            payload.setdefault("message", text)
            if env("COMPOSIO_FACEBOOK_PAGE_ID"):
                payload.setdefault("page_id", env("COMPOSIO_FACEBOOK_PAGE_ID"))
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
Custom prompt style: {config["content_prompt_style"]}
Cấu trúc nội dung cần theo: {config["content_structure"]}
Giọng thương hiệu cần giữ: {config["content_brand_voice"]}
Những điều không được dùng: {config["content_do_not_use"]}
Content Pillars đang áp dụng:
{config["content_pillars_summary"]}

Mẫu bài tham khảo nếu có:
{config["content_examples"]}

Viết tự nhiên, rõ ràng, thực tế. Không dùng giọng quảng cáo quá đà. Không bịa số liệu, chứng nhận, khách hàng, dự án nếu người dùng không cung cấp.
Nếu người dùng yêu cầu bài viết, hãy viết liền mạch như một bài đăng mạng xã hội hoàn chỉnh.
Không ghi các nhãn như HOOK, NỘI DUNG, CTA, FOOTER.
Không dùng dấu hai chấm để đặt tên từng phần.

Dòng đầu tiên là hook, tối đa 16 từ. Viết hoa chữ cái đầu tiên của hook.

Sau hook là phần nội dung chính, ngắn gọn, có chiều sâu thực tế. Có thể dùng bullet ngắn nếu phù hợp.
Không dùng markdown, không dùng dấu **, không in đậm, không gạch đầu dòng quá dài.

Dưới phần nội dung, thêm đúng CTA chuẩn.
Cuối bài, thêm đúng footer chuẩn.

Giữ độ dài vừa phải để gửi Telegram. Không thêm hashtag ngoài phần footer. Tuyệt đối không dùng cấu trúc "không chỉ... mà còn" hoặc các biến thể gần giống.

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
        return clean_generated_post(res.json()["candidates"][0]["content"]["parts"][0]["text"])
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


def create_image_for_draft(user_text, draft_text="", send_draft_first=False):
    image_bytes = generate_image(image_prompt_from_text(user_text, draft_text))
    if send_draft_first and draft_text:
        send_telegram(draft_text)
    send_telegram_photo(image_bytes, "Ảnh minh họa đã tạo. Nếu muốn đăng kèm bài gần nhất, nhắn: đăng bài này lên Facebook")
    return base64.b64encode(image_bytes).decode("ascii")


def agent_manager_route(text):
    plain = strip_tone(text)
    if plain.startswith("confirm "):
        return "ads_operator"
    if any(x in plain for x in ["doi cta", "cap nhat cta", "doi footer", "cap nhat footer", "doi logo", "cap nhat logo", "logo thuong hieu", "doi image provider", "doi provider anh", "provider anh", "doi nguon tao anh", "doi prompt viet bai", "custom prompt", "doi phong cach viet bai", "doi cau truc bai viet", "doi dieu cam khi viet", "doi bai mau", "doi giong thuong hieu", "doi style anh", "doi phong cach anh", "cap nhat style anh", "doi tone", "cap nhat tone", "campaign thang nay", "chien dich thang nay"]):
        return "settings_agent"
    if any(x in plain for x in ["nghien cuu viral", "tim bai viral", "facebook viral", "bai viet viral"]):
        return "viral_researcher"
    if any(x in plain for x in ["phan tich cong thuc viral", "cong thuc viral", "hoc cach viet viral", "loc bai viral"]):
        return "viral_formula_analyst"
    if any(x in plain for x in ["tao anh", "anh minh hoa", "hinh minh hoa", "kem anh", "co anh"]):
        return "image_creator"
    if any(x in plain for x in ["dang bai", "post bai", "up bai", "dang len facebook", "dang len linkedin"]):
        return "social_publisher"
    if is_content_request_plain(plain):
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
            "- Đổi provider ảnh thành: mock hoặc gemini hoặc openai\n"
            "- Đổi phong cách viết bài thành: ...\n"
            "- Đổi cấu trúc bài viết thành: ...\n"
            "- Đổi điều cấm khi viết bài thành: ...\n"
            "- Đổi bài mẫu thành: ...\n"
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
    elif any(x in plain for x in ["image provider", "provider anh", "nguon tao anh"]):
        allowed = {"mock", "gemini", "openai"}
        if value.lower() not in allowed:
            return "Provider ảnh chỉ nhận một trong ba giá trị: mock, gemini, openai."
        key = "image_provider"
        label = "provider ảnh"
        value = value.lower()
    elif any(x in plain for x in ["prompt viet bai", "custom prompt", "phong cach viet bai", "style viet bai"]):
        key = "content_prompt_style"
        label = "phong cách viết bài"
    elif any(x in plain for x in ["cau truc bai viet", "format bai viet", "bo cuc bai viet"]):
        key = "content_structure"
        label = "cấu trúc bài viết"
    elif any(x in plain for x in ["dieu cam khi viet", "khong duoc dung", "tu cam", "cum cam"]):
        key = "content_do_not_use"
        label = "điều cấm khi viết bài"
    elif any(x in plain for x in ["bai mau", "mau bai", "example", "examples"]):
        key = "content_examples"
        label = "bài mẫu tham khảo"
    elif any(x in plain for x in ["giong thuong hieu", "brand voice", "voice thuong hieu"]):
        key = "content_brand_voice"
        label = "giọng thương hiệu"
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
    code = pending_code()
    PENDING[code] = {"entity": entity, "id": entity_id, "status": status, "expires": time.time() + 900}
    remember_pending_state()
    return code


def add_pending_social(platform, text, image_b64=None, image_url=None, content_id=None, video_url=None, media_type=""):
    code = pending_code()
    PENDING[code] = {
        "type": "social_post",
        "platform": platform,
        "text": text,
        "image_b64": image_b64,
        "image_url": image_url,
        "video_url": video_url,
        "media_url": video_url or image_url or "",
        "media_type": media_type or ("video" if video_url else "image" if image_b64 or image_url else "text"),
        "content_id": content_id,
        "expires": time.time() + 900,
    }
    remember_pending_state()
    return code


def confirm(code):
    item = PENDING.pop(code, None)
    save_state()
    if not item or item["expires"] < time.time():
        return "Mã CONFIRM không đúng hoặc đã hết hạn."
    if item.get("type") == "social_post":
        result = post_to_social(item["platform"], item["text"], item.get("image_b64"), item.get("image_url"), item.get("video_url"))
        return f"Đã gửi bài lên {item['platform']} qua Composio.\nKết quả: {json.dumps(result, ensure_ascii=False)[:1000]}"
    meta_post(item["id"], {"status": item["status"]})
    return f"Đã thực hiện: {item['entity']} {item['id']} -> {item['status']}"


def confirm(code):
    restore_pending_state()
    item = PENDING.get(code)
    if not item or item["expires"] < time.time():
        PENDING.pop(code, None)
        remember_pending_state()
        return "Mã CONFIRM không đúng hoặc đã hết hạn."
    if item.get("running"):
        return "Lệnh này đang được xử lý. Chờ kết quả trong giây lát."
    item["running"] = True
    remember_pending_state()
    try:
        if item.get("type") == "social_post":
            result = post_to_social(item["platform"], item["text"], item.get("image_b64"), item.get("image_url"), item.get("video_url"))
            PENDING.pop(code, None)
            remember_pending_state()
            return f"Đã gửi bài lên {item['platform']} qua Composio.\nKết quả: {json.dumps(result, ensure_ascii=False)[:1000]}"
        meta_post(item["id"], {"status": item["status"]})
        PENDING.pop(code, None)
        remember_pending_state()
        return f"Đã thực hiện: {item['entity']} {item['id']} -> {item['status']}"
    except Exception as exc:
        item["running"] = False
        item["last_error"] = str(exc)[:500]
        PENDING[code] = item
        remember_pending_state()
        raise


def confirm(code):
    with STATE_LOCK:
        restore_pending_state()
        item = PENDING.get(code)
        if not item or item["expires"] < time.time():
            PENDING.pop(code, None)
            remember_pending_state()
            return "Mã CONFIRM không đúng hoặc đã hết hạn."
        if item.get("running"):
            return "Lệnh này đang được xử lý. Chờ kết quả trong giây lát."
        item["running"] = True
        PENDING[code] = item
        remember_pending_state()
    try:
        if item.get("type") == "social_post":
            result = post_to_social(item["platform"], item["text"], item.get("image_b64"), item.get("image_url"), item.get("video_url"))
            content_id = item.get("content_id")
            media_type = item.get("media_type") or ("video" if item.get("video_url") else "image" if item.get("image_b64") or item.get("image_url") else "text")
            post_url = find_first_url_deep(result)
            append_posting_log(content_id, item["platform"], media_type, "posted", result)
            if content_id:
                update_content_by_id(
                    content_id,
                    {
                        "status": "posted",
                        "stage": "posted",
                        "posted_at": now_text(),
                        "post_url": post_url,
                        "result_preview": json.dumps(result, ensure_ascii=False)[:1000],
                        "updated_at": now_text(),
                    },
                )
            append_bot_event("social_post_confirmed", "ok", content_id or item["platform"], code)
            with STATE_LOCK:
                PENDING.pop(code, None)
                remember_pending_state()
            return f"Đã gửi bài lên {item['platform']} qua Composio.\nKết quả: {json.dumps(result, ensure_ascii=False)[:1000]}"
        meta_post(item["id"], {"status": item["status"]})
        with STATE_LOCK:
            PENDING.pop(code, None)
            remember_pending_state()
        return f"Đã thực hiện: {item['entity']} {item['id']} -> {item['status']}"
    except Exception as exc:
        err = str(exc)[:500]
        if item.get("type") == "social_post":
            append_posting_log(item.get("content_id"), item.get("platform", ""), item.get("media_type", ""), "failed", "", err)
            if item.get("content_id"):
                update_content_by_id(item.get("content_id"), {"status": "failed", "last_error": err, "updated_at": now_text()})
        with STATE_LOCK:
            item["running"] = False
            item["last_error"] = err
            PENDING[code] = item
            remember_pending_state()
        raise


def handle_text(text, async_sheet=False):
    plain = strip_tone(text)
    chat_key = "default"
    agent = agent_manager_route(text)
    if any(x in plain for x in ["kiem tra lich dang", "check lich dang", "scheduler dry run", "test scheduler"]):
        result = process_due_content(limit=5, dry_run=True, auto_post=False)
        return "Kết quả kiểm tra lịch đăng:\n" + json.dumps(result, ensure_ascii=False, indent=2)[:3000]
    if any(x in plain for x in ["chay lich dang", "quet lich dang", "xu ly lich dang", "scheduler tick"]):
        result = process_due_content(limit=5, dry_run=False, auto_post=False)
        return "Đã chạy lịch đăng:\n" + json.dumps(result, ensure_ascii=False, indent=2)[:3000]
    if plain in ["/agents", "agents", "agent", "kien truc agent", "kien truc bot"]:
        return agents_text()
    if plain in ["/help", "help"]:
        return help_text()
    if plain.startswith("confirm "):
        return confirm(plain.split()[-1])
    if plain in ["/cancel", "huy", "cancel"]:
        PENDING.clear()
        remember_pending_state()
        return "Đã hủy các lệnh đang chờ xác nhận."
    if any(x in plain for x in ["gan video tu sheet", "lay video tu sheet", "cap nhat video tu sheet"]):
        try:
            attach_video_from_content_sheet(chat_key, async_sheet=async_sheet)
            return "Đã gắn video từ Sheet vào bài nháp. Bây giờ có thể bấm Đăng Facebook để tạo mã duyệt."
        except Exception as exc:
            return f"Chưa gắn được video từ Sheet: {exc}"
    if any(x in plain for x in ["gan video", "cap nhat video", "them video", "dung video nay"]):
        urls = [url for url in extract_urls(text) if looks_like_video_url(url)]
        if urls:
            try:
                attach_video_to_draft(chat_key, urls[0], source="telegram_link", async_sheet=async_sheet)
                return "Đã gắn video vào bài nháp. Bây giờ có thể bấm Đăng Facebook để tạo mã duyệt."
            except Exception as exc:
                return f"Chưa gắn được video: {exc}"
    if plain in ["image prompt", "prompt anh", "lay prompt anh", "xem prompt anh"]:
        draft = restore_last_draft(chat_key)
        prompt = draft.get("image_prompt")
        if not prompt:
            return "Chưa có image prompt nào. Hãy nhắn: tạo ảnh minh họa cho bài này"
        return f"Image prompt hiện tại:\n{prompt[:2500]}"
    if any(x in plain for x in ["gan anh tu sheet", "lay anh tu sheet", "cap nhat anh tu sheet", "gan hinh tu sheet", "lay hinh tu sheet"]):
        try:
            image_bytes, draft = attach_image_from_content_sheet(chat_key, async_sheet=async_sheet)
            send_telegram_photo(image_bytes, "Đã lấy ảnh từ Sheet và gắn vào bài nháp gần nhất.")
            return "Đã gắn ảnh từ Sheet vào bài nháp. Bây giờ có thể bấm Đăng Facebook để tạo mã duyệt."
        except Exception as exc:
            return f"Chưa gắn được ảnh từ Sheet: {exc}"
    if any(x in plain for x in ["gan anh", "gan hinh", "cap nhat anh", "them anh", "dung anh nay"]):
        urls = [url for url in extract_urls(text) if looks_like_image_url(url)]
        if urls:
            try:
                image_bytes, draft = attach_image_to_draft(chat_key, urls[0], source="telegram_link", async_sheet=async_sheet)
                send_telegram_photo(image_bytes, "Đã gắn ảnh này vào bài nháp gần nhất.")
                return "Đã gắn ảnh vào bài nháp. Bây giờ có thể bấm Đăng Facebook để tạo mã duyệt."
            except Exception as exc:
                return f"Chưa gắn được ảnh: {exc}"
    if agent == "settings_agent":
        return settings_agent_handle(text)
    if agent == "viral_researcher":
        return viral_research_text(text)
    if agent == "viral_formula_analyst":
        return gemini_analyze_viral_formula(text)
    if agent == "image_creator":
        draft = restore_last_draft(chat_key)
        draft_text = draft.get("text", "")
        if is_generation_error(draft_text):
            draft_text = ""
        wants_new_draft = is_content_request_plain(plain)
        if wants_new_draft:
            draft_text = generate_content_text(text)
        elif not draft_text:
            return "Chưa có bài nháp nào để tạo ảnh. Hãy nhắn: Tạo 1 bài viết P1"
        image_prompt = image_prompt_from_text(text, draft_text)
        try:
            image_b64 = create_image_for_draft(text, draft_text, send_draft_first=wants_new_draft)
        except Exception as exc:
            content_id, sheet_error = append_content_record(
                topic=text,
                draft_text=draft_text,
                image_prompt=image_prompt,
                stage="image_pending",
                status="pending_manual_image",
                async_write=async_sheet,
            )
            remember_last_draft(
                chat_key,
                {
                    "text": draft_text,
                    "image_prompt": image_prompt,
                    "image_status": "pending_manual_image",
                    "content_id": content_id,
                },
                async_write=async_sheet,
            )
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
            async_write=async_sheet,
        )
        remember_last_draft(
            chat_key,
            {"text": draft_text, "image_b64": image_b64, "image_prompt": image_prompt, "content_id": content_id},
            async_write=async_sheet,
        )
        if draft_text and wants_new_draft:
            return "Đã gửi bài viết và ảnh minh họa. Nếu muốn đăng cả bài và ảnh, nhắn: đăng bài này lên Facebook" + sheet_note(sheet_error)
        if draft_text:
            return draft_text + "\n\nĐã tạo ảnh minh họa. Nếu muốn đăng cả bài và ảnh, nhắn: đăng bài này lên Facebook" + sheet_note(sheet_error)
        return "Đã tạo ảnh minh họa. Nếu muốn viết thêm nội dung cho ảnh này, nhắn: viết bài cho ảnh vừa tạo." + sheet_note(sheet_error)
    if "bai quang cao" in plain and ("tot" in plain or "hieu qua" in plain):
        return best_ads_text(text)
    if any(x in plain for x in ["nen lam gi", "goi y", "de xuat", "toi uu", "can chu y", "dang te", "dot tien", "toi nen lam gi"]):
        return recommendations_text(text)
    if any(x in plain for x in ["bao cao", "report", "ads hom nay", "ads hnay", "ads hom qua", "tinh hinh ads", "ads the nao"]):
        return report_text(text)
    if is_content_request_plain(plain):
        draft = generate_content_text(text)
        content_id, sheet_error = append_content_record(topic=text, draft_text=draft, async_write=async_sheet)
        remember_last_draft(chat_key, {"text": draft, "content_id": content_id}, async_write=async_sheet)
        return draft + "\n\nNếu muốn tạo ảnh minh họa, nhắn: tạo ảnh minh họa cho bài này\nNếu muốn đăng bài này, nhắn: đăng bài này lên Facebook" + sheet_note(sheet_error)
    if agent == "social_publisher" or any(x in plain for x in ["dang bai nay len facebook", "dang len facebook", "post bai nay len facebook", "up bai nay len facebook", "dang bai nay len linkedin", "dang len linkedin", "post bai", "up bai", "dang bai"]):
        platform = "LinkedIn" if "linkedin" in plain else "Facebook"
        draft = restore_last_draft(chat_key)
        if not draft:
            return "Chưa có bản nháp nào để đăng. Hãy nhắn: tạo cho tôi một bài viết về ..."
        draft_text = draft.get("text", "")
        image_b64 = draft.get("image_b64")
        image_url = draft.get("image_url")
        video_url = draft.get("video_url") or (draft.get("media_url") if draft.get("media_type") == "video" else "")
        if not image_b64 and not image_url and not video_url:
            try:
                draft = attach_video_from_content_sheet(chat_key, async_sheet=async_sheet)
                video_url = draft.get("video_url") or (draft.get("media_url") if draft.get("media_type") == "video" else "")
            except Exception:
                video_url = ""
        if not image_b64 and not image_url and not video_url:
            try:
                _, draft = attach_image_from_content_sheet(chat_key, async_sheet=async_sheet)
                image_b64 = draft.get("image_b64")
                image_url = draft.get("image_url")
            except Exception:
                image_b64 = ""
                image_url = ""
        code = add_pending_social(platform, draft_text, image_b64, image_url, draft.get("content_id"), video_url, "video" if video_url else "")
        media_note = " kèm ảnh" if image_b64 or image_url else ""
        media_note = " kèm video" if video_url else media_note
        pending_image_note = ""
        if draft.get("image_status") == "pending_manual_image" and not image_b64 and not image_url and not video_url:
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
            draft = generate_content_text(text)
            content_id, sheet_error = append_content_record(topic=text, draft_text=draft, async_write=async_sheet)
            remember_last_draft(chat_key, {"text": draft, "content_id": content_id}, async_write=async_sheet)
            return draft + "\n\nNếu muốn tạo ảnh minh họa, nhắn: tạo ảnh minh họa cho bài này\nNếu muốn đăng bài này, nhắn: đăng bài này lên Facebook" + sheet_note(sheet_error)
        if intent.get("intent") == "cancel":
            PENDING.clear()
            remember_pending_state()
            return "Đã hủy các lệnh đang chờ xác nhận."
        if intent.get("intent") == "gemini_unavailable":
            return (
                "Gemini hiện không khả dụng hoặc đã hết quota. "
                "Bot vẫn xử lý được các lệnh cơ bản: báo cáo ads hôm nay, bài quảng cáo nào đang tốt, "
                "nên làm gì hôm nay, xem danh sách campaign, dừng campaign <id>."
            )
    return "Mình chưa hiểu rõ. Bạn có thể hỏi: báo cáo ads hôm nay, bài quảng cáo nào đang tốt, hoặc nên làm gì hôm nay."


def handle_callback(data):
    data = data or ""
    chat_key = "default"
    if data == "draft:cancel":
        PENDING.clear()
        remember_pending_state()
        return {"text": "Đã hủy thao tác đang chờ.", "buttons": None}

    if data == "draft:create_image":
        draft = restore_last_draft(chat_key)
        if not draft.get("text"):
            return {"text": "Chưa có bài nháp nào để tạo ảnh. Hãy nhắn: Tạo 1 bài viết P1", "buttons": None}
        result_text = handle_text("tạo ảnh minh họa cho bài này", async_sheet=True)
        if "Image prompt:" not in result_text and not result_text.startswith("Chưa"):
            result_text = "Đã tạo ảnh minh họa cho bài gần nhất. Nếu muốn đăng kèm bài và ảnh, bấm Đăng Facebook."
        return {"text": result_text, "buttons": draft_action_buttons()}

    if data == "draft:post_facebook":
        draft = restore_last_draft(chat_key)
        draft_text = draft.get("text", "")
        if not draft_text:
            return {"text": "Chưa có bài nháp nào để đăng. Hãy nhắn: Tạo 1 bài viết P1", "buttons": None}
        image_b64 = draft.get("image_b64")
        image_url = draft.get("image_url")
        video_url = draft.get("video_url") or (draft.get("media_url") if draft.get("media_type") == "video" else "")
        if not image_b64 and not image_url and not video_url:
            try:
                draft = attach_video_from_content_sheet(chat_key, async_sheet=True)
                video_url = draft.get("video_url") or (draft.get("media_url") if draft.get("media_type") == "video" else "")
            except Exception:
                video_url = ""
        if not image_b64 and not image_url and not video_url:
            try:
                _, draft = attach_image_from_content_sheet(chat_key, async_sheet=True)
                image_b64 = draft.get("image_b64")
                image_url = draft.get("image_url")
            except Exception:
                image_b64 = ""
                image_url = ""
        code = add_pending_social("Facebook", draft_text, image_b64, image_url, draft.get("content_id"), video_url, "video" if video_url else "")
        media_note = " kèm ảnh" if draft.get("image_b64") else ""
        pending_image_note = "\nẢnh đang chờ tạo thủ công nên lệnh này sẽ đăng text trước." if draft.get("image_status") == "pending_manual_image" and not draft.get("image_b64") else ""
        media_note = " kèm ảnh" if image_b64 or image_url else media_note
        media_note = " kèm video" if video_url else media_note
        if image_b64 or image_url or video_url:
            pending_image_note = ""
        return {
            "text": f"Mình sẽ đăng bản nháp gần nhất{media_note} lên Facebook qua Composio.{pending_image_note}\nMã xác nhận: {code}\nMã hết hạn sau 15 phút.",
            "buttons": confirm_buttons(code),
        }

    if data.startswith("confirm:"):
        code = data.split(":", 1)[1].strip()
        return {"text": confirm(code), "buttons": None}

    return {"text": "Nút này chưa được hỗ trợ.", "buttons": None}


load_state()


@app.get("/health")
def health():
    return {"ok": True}


@app.get("/cron/scheduler-tick/<secret>")
def cron_scheduler_tick(secret):
    if secret != env("WEBHOOK_SECRET"):
        abort(404)
    dry_run = request.args.get("dry_run", "1").lower() not in ["0", "false", "no"]
    auto_post = request.args.get("auto_post", "0").lower() in ["1", "true", "yes"]
    limit_raw = request.args.get("limit", "5")
    try:
        limit = max(1, min(20, int(limit_raw)))
    except ValueError:
        limit = 5
    if auto_post and os.environ.get("AUTO_POST_SCHEDULED_CONTENT", "false").lower() not in ["1", "true", "yes", "on"]:
        return {"ok": False, "error": "AUTO_POST_SCHEDULED_CONTENT chưa bật. Scheduler chỉ được tạo mã duyệt qua Telegram."}, 200
    try:
        return safe_json_value(process_due_content(limit=limit, dry_run=dry_run, auto_post=auto_post)), 200
    except Exception as exc:
        app.logger.exception("Scheduler tick failed")
        append_bot_event("scheduler_tick_failed", "error", str(exc)[:500], "cron")
        return {"ok": False, "error": str(exc)[:1000]}, 200


@app.get("/cron/scheduler-v2/<secret>")
def cron_scheduler_v2(secret):
    if secret != os.environ.get("WEBHOOK_SECRET", ""):
        abort(404)
    try:
        dry_run = request.args.get("dry_run", "1").lower() not in ["0", "false", "no"]
        auto_post = request.args.get("auto_post", "0").lower() in ["1", "true", "yes"]
        limit_raw = request.args.get("limit", "5")
        try:
            limit = max(1, min(20, int(limit_raw)))
        except ValueError:
            limit = 5
        if auto_post and os.environ.get("AUTO_POST_SCHEDULED_CONTENT", "false").lower() not in ["1", "true", "yes", "on"]:
            payload = {"ok": False, "error": "AUTO_POST_SCHEDULED_CONTENT chưa bật. Scheduler chỉ được tạo mã duyệt qua Telegram."}
        else:
            payload = process_due_content(limit=limit, dry_run=dry_run, auto_post=auto_post)
        return app.response_class(
            response=json.dumps(safe_json_value(payload), ensure_ascii=False),
            status=200,
            mimetype="application/json",
        )
    except Exception as exc:
        app.logger.exception("Scheduler v2 failed")
        append_bot_event("scheduler_v2_failed", "error", str(exc)[:500], "cron")
        return app.response_class(
            response=json.dumps({"ok": False, "error": str(exc)[:1000]}, ensure_ascii=False),
            status=200,
            mimetype="application/json",
        )


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


@app.get("/debug/setup-content-calendar/<secret>")
def debug_setup_content_calendar(secret):
    if secret != env("WEBHOOK_SECRET"):
        abort(404)
    try:
        google_sheets_write("Content", [CONTENT_HEADERS], "A1")
        return {"ok": True, "headers": CONTENT_HEADERS}, 200
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:1000]}, 200


@app.get("/debug/scheduler-seed/<secret>")
def debug_scheduler_seed(secret):
    if secret != env("WEBHOOK_SECRET"):
        abort(404)
    now_dt = bangkok_now() - timedelta(minutes=2)
    content_id = new_record_id("sched_test")
    row = [
        content_id,
        now_dt.strftime("%Y-%m-%d"),
        now_dt.strftime("%H:%M"),
        "Facebook",
        "P1",
        "scheduler smoke test",
        "Scheduler Smoke Test\n\nĐây là bài test nội bộ để kiểm tra luồng lên lịch. Không bấm CONFIRM nếu không muốn đăng thật.",
        "",
        "",
        "",
        "",
        "text",
        "",
        "",
        "scheduled",
        "scheduled",
        now_text(),
    ]
    try:
        google_sheets_append("Content", [row])
        return {"ok": True, "content_id": content_id}, 200
    except Exception as exc:
        return {"ok": False, "content_id": content_id, "error": str(exc)[:1000]}, 200


@app.get("/debug/task-queue-test/<secret>")
def debug_task_queue_test(secret):
    if secret != env("WEBHOOK_SECRET"):
        abort(404)
    task_id, error = enqueue_task(
        "debug_test",
        {"message": "Task queue write test", "created_at": now_text()},
        source_update_id=f"debug:{int(time.time())}",
        chat_id=env("TELEGRAM_CHAT_ID"),
        async_write=False,
    )
    return {"ok": error is None, "task_id": task_id, "error": error}, 200


@app.get("/debug/setup-state-tabs/<secret>")
def debug_setup_state_tabs(secret):
    if secret != env("WEBHOOK_SECRET"):
        abort(404)
    try:
        specs = {
            "Bot_State": ["key", "value_json", "updated_at", "version"],
            "Task_Queue": ["task_id", "source_update_id", "chat_id", "task_type", "payload_json", "status", "lease_until", "attempts", "result_preview", "error", "created_at", "updated_at"],
            "Bot_Events": ["event_id", "created_at", "event_type", "status", "ref_id", "detail"],
            "Posting_Log": ["post_event_id", "content_id", "platform", "media_type", "status", "result_json", "error", "created_at"],
        }
        results = []
        for sheet_name, headers in specs.items():
            item = {"sheet": sheet_name}
            try:
                google_sheets_add_sheet(sheet_name, rows=500, columns=max(8, len(headers)))
                item["created"] = True
            except Exception as exc:
                item["created"] = False
                item["create_error"] = str(exc)[:250]
            try:
                google_sheets_write(sheet_name, [headers], "A1")
                item["headers_written"] = True
            except Exception as exc:
                item["headers_written"] = False
                item["write_error"] = str(exc)[:300]
            results.append(item)
        return {"ok": all(item.get("headers_written") for item in results), "results": results}, 200
    except Exception as exc:
        app.logger.exception("Could not setup durable bot tabs")
        return {"ok": False, "error": str(exc)[:1000], "results": []}, 200


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
        "Bot_State": {
            "headers": ["key", "value_json", "updated_at", "version"],
            "rows": [],
        },
        "Task_Queue": {
            "headers": ["task_id", "source_update_id", "chat_id", "task_type", "payload_json", "status", "lease_until", "attempts", "result_preview", "error", "created_at", "updated_at"],
            "rows": [],
        },
        "Bot_Events": {
            "headers": ["event_id", "created_at", "event_type", "status", "ref_id", "detail"],
            "rows": [],
        },
        "Posting_Log": {
            "headers": ["post_event_id", "content_id", "platform", "media_type", "status", "result_json", "error", "created_at"],
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


@app.get("/debug/setup-content-prompt-defaults/<secret>")
def debug_setup_content_prompt_defaults(secret):
    if secret != env("WEBHOOK_SECRET"):
        abort(404)
    defaults = [
        (
            "content_prompt_style",
            "Viết như một cố vấn thực tế, rõ ý, có chiều sâu. Không viết kiểu quảng cáo lố. Ưu tiên câu ngắn, dễ đọc, gần với chủ doanh nghiệp, HR, admin mua hàng.",
            "Default content writing style",
        ),
        (
            "content_structure",
            "Dòng đầu là hook viết hoa chữ cái đầu từng từ. Sau đó viết 2-3 đoạn ngắn. Nếu dùng bullet thì tối đa 3 bullet. Cuối bài luôn có CTA và footer. Không ghi nhãn HOOK, NỘI DUNG, CTA, FOOTER.",
            "Default content structure",
        ),
        (
            "content_do_not_use",
            "Không dùng: không chỉ... mà còn, giải pháp tối ưu, nâng tầm quá nhiều, đột phá, toàn diện, chuyên nghiệp hóa nếu không cần. Không bịa số liệu, tiêu chuẩn, chứng nhận, dự án, khách hàng.",
            "Default content banned phrases and rules",
        ),
        (
            "content_brand_voice",
            "Sư Tử Vàng nói chuyện như một đơn vị may đồng phục có kinh nghiệm thực chiến: rõ ràng, đáng tin, tư vấn thật, không nói như agency quảng cáo.",
            "Default brand voice",
        ),
        (
            "content_examples",
            "",
            "Paste sample posts here when available",
        ),
    ]
    results = []
    for key, value, note in defaults:
        err = append_settings_change(key, value, note=note, source="system")
        results.append({"key": key, "ok": err is None, "error": err})
    refresh_runtime_config_from_sheet(force=True)
    return {"ok": all(item["ok"] for item in results), "results": results}, 200


@app.get("/debug/setup-content-prompt-tab/<secret>")
def debug_setup_content_prompt_tab(secret):
    if secret != env("WEBHOOK_SECRET"):
        abort(404)
    values = [
        ["setting_type", "value", "note", "status", "updated_at"],
        [
            "content_prompt_style",
            "Viết như một cố vấn thực tế, rõ ý, có chiều sâu. Không viết kiểu quảng cáo lố. Ưu tiên câu ngắn, dễ đọc, gần với chủ doanh nghiệp, HR, admin mua hàng.",
            "Phong cách viết chung",
            "active",
            now_text(),
        ],
        [
            "content_structure",
            "Dòng đầu là hook viết hoa chữ cái đầu từng từ. Sau đó viết 2-3 đoạn ngắn. Nếu dùng bullet thì tối đa 3 bullet. Cuối bài luôn có CTA và footer. Không ghi nhãn HOOK, NỘI DUNG, CTA, FOOTER.",
            "Cấu trúc bài viết",
            "active",
            now_text(),
        ],
        [
            "content_do_not_use",
            "Không dùng: không chỉ... mà còn, giải pháp tối ưu, nâng tầm quá nhiều, đột phá, toàn diện, chuyên nghiệp hóa nếu không cần. Không bịa số liệu, tiêu chuẩn, chứng nhận, dự án, khách hàng.",
            "Cụm từ/cách viết cần tránh",
            "active",
            now_text(),
        ],
        [
            "content_brand_voice",
            "Sư Tử Vàng nói chuyện như một đơn vị may đồng phục có kinh nghiệm thực chiến: rõ ràng, đáng tin, tư vấn thật, không nói như agency quảng cáo.",
            "Giọng thương hiệu",
            "active",
            now_text(),
        ],
        [
            "content_examples",
            "",
            "Dán 3-5 bài mẫu vào đây nếu muốn bot học theo giọng viết",
            "active",
            now_text(),
        ],
    ]
    result = []
    try:
        google_sheets_add_sheet("Content_Prompt", rows=120, columns=8)
        result.append({"step": "create_sheet", "ok": True})
    except Exception as exc:
        result.append({"step": "create_sheet", "ok": False, "error": str(exc)[:250]})
    try:
        google_sheets_write("Content_Prompt", values, "A1")
        refresh_runtime_config_from_sheet(force=True)
        result.append({"step": "write_defaults", "ok": True})
    except Exception as exc:
        result.append({"step": "write_defaults", "ok": False, "error": str(exc)[:500]})
    return {"ok": all(item["ok"] or item["step"] == "create_sheet" for item in result), "result": result}, 200


@app.post("/debug/handle/<secret>")
def debug_handle(secret):
    if secret != env("WEBHOOK_SECRET"):
        abort(404)
    payload = request.get_json(force=True, silent=True) or {}
    text = payload.get("text", "")
    if not text:
        return {"ok": False, "error": "missing text"}, 200
    try:
        reply = handle_text(text, async_sheet=True)
        return safe_json_value({"ok": True, "reply": reply}), 200
    except Exception as exc:
        app.logger.exception("Could not handle debug text")
        return safe_json_value({"ok": False, "error": str(exc)}), 200


@app.get("/debug/telegram-outbox/<secret>")
def debug_telegram_outbox(secret):
    if secret != env("WEBHOOK_SECRET"):
        abort(404)
    load_state()
    return {"ok": True, "outbox": TELEGRAM_OUTBOX[-15:]}, 200


@app.get("/debug/bot-events/<secret>")
def debug_bot_events(secret):
    if secret != env("WEBHOOK_SECRET"):
        abort(404)
    try:
        payload = google_sheets_batch_get(["Bot_Events!A1:F80"])
        rows = parse_table(range_values_map(payload).get("Bot_Events", []))
        return {"ok": True, "events": rows[-20:]}, 200
    except Exception as exc:
        app.logger.exception("Could not read bot events")
        return {"ok": False, "error": str(exc)[:1000], "events": []}, 200


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


@app.get("/debug/logo-overlay-test/<secret>")
def debug_logo_overlay_test(secret):
    if secret != env("WEBHOOK_SECRET"):
        abort(404)
    base = Image.new("RGB", (1080, 1350), (28, 28, 28))
    draw = ImageDraw.Draw(base)
    draw.rectangle([0, 0, 1080, 260], fill=(245, 245, 245))
    draw.rectangle([0, 260, 1080, 1350], fill=(32, 32, 32))
    draw.rectangle([70, 930, 1010, 1260], outline=(212, 168, 72), width=8)
    draw.text((90, 960), "Khu vực ảnh mẫu 4:5", fill=(255, 255, 255))
    raw = io.BytesIO()
    base.save(raw, format="PNG")
    image_bytes = apply_brand_logo_overlay(raw.getvalue())
    return {
        "ok": True,
        "has_brand_logo_url": bool(workspace_config().get("brand_logo_url")),
        "input_bytes": len(raw.getvalue()),
        "output_bytes": len(image_bytes),
        "note": "Nếu output_bytes > input_bytes và không có lỗi log, lớp đóng logo đã chạy.",
    }, 200


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
        "content_prompt_style": config["content_prompt_style"][:300],
        "content_structure": config["content_structure"][:300],
        "content_do_not_use": config["content_do_not_use"][:300],
        "content_brand_voice": config["content_brand_voice"][:300],
        "has_content_examples": bool(config["content_examples"]),
        "content_pillars_summary": config["content_pillars_summary"][:800],
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
        "content_prompt_style": config["content_prompt_style"][:500],
        "content_structure": config["content_structure"][:500],
        "content_do_not_use": config["content_do_not_use"][:500],
        "content_brand_voice": config["content_brand_voice"][:500],
        "has_content_examples": bool(config["content_examples"]),
        "content_pillars_summary": config["content_pillars_summary"][:1200],
        "has_default_cta": bool(config["default_cta"]),
        "has_default_footer": bool(config["default_footer"]),
        "has_brand_logo_url": bool(config["brand_logo_url"]),
    }, 200


@app.get("/debug/check-config-updates/<secret>")
def debug_check_config_updates(secret):
    if secret != env("WEBHOOK_SECRET"):
        abort(404)
    notify = request.args.get("notify", "true").lower() not in ["0", "false", "no"]
    initialize = request.args.get("initialize", "false").lower() in ["1", "true", "yes"]
    try:
        result = check_config_updates(notify=notify, initialize=initialize)
    except Exception as exc:
        app.logger.exception("Could not check config updates from debug endpoint")
        result = {"ok": False, "changed": False, "error": str(exc)[:500]}
    return {
        **result,
        "watch_enabled": os.environ.get("CONFIG_WATCH_ENABLED", "true").lower() not in ["0", "false", "no", "off"],
        "watch_interval_seconds": config_watch_interval_seconds(),
        "has_signature": bool(CURRENT_CONFIG_SIGNATURE),
        "has_last_signature": bool(LAST_CONFIG_SIGNATURE),
    }, 200


@app.post("/telegram/<secret>")
def telegram(secret):
    if secret != env("WEBHOOK_SECRET"):
        abort(404)
    load_state()
    update = request.get_json(force=True, silent=True) or {}
    update_id = update.get("update_id")
    if update_id is not None:
        if update_id in PROCESSED_UPDATES:
            return {"ok": True, "duplicate": True}
        PROCESSED_UPDATES.add(update_id)
        if len(PROCESSED_UPDATES) > 500:
            PROCESSED_UPDATES.clear()
        save_state()

    callback = update.get("callback_query") or {}
    if callback:
        callback_message = callback.get("message") or {}
        callback_chat = callback_message.get("chat") or {}
        if str(callback_chat.get("id")) != str(env("TELEGRAM_CHAT_ID")):
            return {"ok": True}
        answer_callback_query_async(callback.get("id"), "Dang xu ly")
        try:
            result = handle_callback(callback.get("data", ""))
            if result.get("buttons"):
                send_telegram_buttons(result.get("text", ""), result.get("buttons"))
            else:
                send_telegram(result.get("text", ""))
        except Exception as exc:
            app.logger.exception("Could not enqueue Telegram callback")
            try:
                send_telegram_async(f"Loi khi xu ly nut: {exc}")
            except Exception:
                app.logger.exception("Could not enqueue callback error to Telegram")
        return {"ok": True}
        answer_callback_query(callback.get("id"), "Đang xử lý")
        try:
            process_callback_async(callback.get("data", ""))
        except Exception as exc:
            app.logger.exception("Could not process Telegram callback")
            try:
                send_telegram(f"Lỗi khi xử lý nút: {exc}")
            except Exception:
                app.logger.exception("Could not send callback error to Telegram")
        return {"ok": True}

    message = update.get("message") or {}
    chat = message.get("chat") or {}
    if str(chat.get("id")) != str(env("TELEGRAM_CHAT_ID")):
        return {"ok": True}
    text = message.get("text")
    if not text:
        return {"ok": True}
    try:
        reply = handle_text(text, async_sheet=True)
        if is_content_request_plain(strip_tone(text)):
            send_telegram_buttons(reply, draft_action_buttons())
        else:
            send_telegram(reply)
    except Exception as exc:
        app.logger.exception("Could not process Telegram message")
        try:
            send_telegram(f"Lỗi khi xử lý lệnh: {exc}")
        except Exception:
            app.logger.exception("Could not send message error to Telegram")
    return {"ok": True}


start_config_watcher()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "10000")))
