import json
import os
import random
import re
import time
import unicodedata
from datetime import date, timedelta

import requests
from flask import Flask, abort, request

app = Flask(__name__)

PENDING = {}


def env(name, default=None):
    value = os.environ.get(name, default)
    if value is None or value == "":
        raise RuntimeError(f"Missing env var: {name}")
    return value


def gemini_model():
    return os.environ.get("GEMINI_MODEL", "gemini-2.5-flash-lite")


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
Intent hợp lệ: report, recommendations, best_ads, campaigns, pause, resume, cancel, help, unknown.
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


def add_pending(entity, entity_id, status):
    code = str(random.randint(1000, 9999))
    PENDING[code] = {"entity": entity, "id": entity_id, "status": status, "expires": time.time() + 900}
    return code


def confirm(code):
    item = PENDING.get(code)
    if not item or item["expires"] < time.time():
        return "Mã CONFIRM không đúng hoặc đã hết hạn."
    meta_post(item["id"], {"status": item["status"]})
    del PENDING[code]
    return f"Đã thực hiện: {item['entity']} {item['id']} -> {item['status']}"


def handle_text(text):
    plain = strip_tone(text)
    if plain in ["/help", "help"]:
        return help_text()
    if plain.startswith("confirm "):
        return confirm(plain.split()[-1])
    if plain in ["/cancel", "huy", "cancel"]:
        PENDING.clear()
        return "Đã hủy các lệnh đang chờ xác nhận."
    if "bai quang cao" in plain and ("tot" in plain or "hieu qua" in plain):
        return best_ads_text(text)
    if any(x in plain for x in ["nen lam gi", "goi y", "de xuat", "toi uu", "can chu y", "dang te", "dot tien", "toi nen lam gi"]):
        return recommendations_text(text)
    if any(x in plain for x in ["bao cao", "report", "ads hom nay", "ads hnay", "ads hom qua", "tinh hinh ads", "ads the nao"]):
        return report_text(text)
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
