"""
סוכן חיפוש דירות - סורק את יד2, מסנן לפי הקריטריונים ב-config.yaml
ושולח כל דירה חדשה (וכל ירידת מחיר) לטלגרם.

הרצה:
    python agent.py            ריצה רגילה
    python agent.py --test     שולח הודעת בדיקה לטלגרם
    python agent.py --dry-run  מדפיס למסך בלי לשלוח ובלי לשמור
"""

import html
import json
import os
import re
import sys
import time
from pathlib import Path

import requests
import yaml

ROOT = Path(__file__).parent
STATE_FILE = ROOT / "seen.json"
CONFIG_FILE = ROOT / "config.yaml"

DRY_RUN = "--dry-run" in sys.argv
TG_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
TG_CHAT = os.environ.get("TELEGRAM_CHAT_ID", "").strip()

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "he-IL,he;q=0.9,en-US;q=0.8,en;q=0.7",
    "Cache-Control": "no-cache",
    "Upgrade-Insecure-Requests": "1",
}

BLOCK_MARKERS = ("shieldsquare", "are you for real", "perfdrive", "radware", "captcha")
BLOCK_ALERT_EVERY = 6 * 3600   # לא להציף: התראת חסימה לכל היותר פעם ב-6 שעות
KEEP_SEEN_DAYS = 60


# ---------------------------------------------------------------- utils

def g(d, *keys, default=None):
    """שליפה בטוחה ממבנה מקונן."""
    for k in keys:
        if isinstance(d, dict):
            d = d.get(k)
        elif isinstance(d, list) and isinstance(k, int) and -len(d) <= k < len(d):
            d = d[k]
        else:
            return default
        if d is None:
            return default
    return d


def to_num(v):
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return v
    s = re.sub(r"[^\d.]", "", str(v))
    try:
        return float(s) if "." in s else int(s)
    except ValueError:
        return None


def walk(obj):
    """מעבר על כל המילונים במבנה JSON."""
    stack = [obj]
    while stack:
        cur = stack.pop()
        if isinstance(cur, dict):
            yield cur
            stack.extend(cur.values())
        elif isinstance(cur, list):
            stack.extend(cur)


def all_strings(obj):
    out = []
    stack = [obj]
    while stack:
        cur = stack.pop()
        if isinstance(cur, str):
            out.append(cur)
        elif isinstance(cur, dict):
            stack.extend(cur.values())
        elif isinstance(cur, list):
            stack.extend(cur)
    return " ".join(out)


def load_state():
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    return {"seeded": False, "seen": {}, "last_block_alert": 0}


def save_state(state):
    if DRY_RUN:
        return
    STATE_FILE.write_text(
        json.dumps(state, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8"
    )


# ---------------------------------------------------------------- telegram

def tg(method, **payload):
    if DRY_RUN or not TG_TOKEN or not TG_CHAT:
        print(f"[telegram:{method}]", payload.get("caption") or payload.get("text"))
        return True
    payload["chat_id"] = TG_CHAT
    for attempt in range(3):
        try:
            r = requests.post(
                f"https://api.telegram.org/bot{TG_TOKEN}/{method}", data=payload, timeout=30
            )
            if r.ok:
                return True
            if r.status_code == 429:
                time.sleep(int(g(r.json(), "parameters", "retry_after", default=5)) + 1)
                continue
            print("telegram error:", r.status_code, r.text[:300])
            return False
        except requests.RequestException as e:
            print("telegram exception:", e)
            time.sleep(3)
    return False


def send_text(text):
    return tg("sendMessage", text=text, parse_mode="HTML", disable_web_page_preview="false")


def send_listing(item, title=None):
    e = html.escape
    parts = []
    if title:
        parts.append(f"<b>{e(title)}</b>")
    head = []
    if item["price"]:
        head.append(f"<b>₪{item['price']:,}</b>")
    if item["rooms"]:
        head.append(f"{item['rooms']:g} חד'")
    if item["sqm"]:
        head.append(f"{item['sqm']} מ\"ר")
    if head:
        parts.append(" · ".join(head))
    addr = ", ".join(x for x in [item["street"], item["neighborhood"], item["city"]] if x)
    if addr:
        parts.append(f"📍 {e(addr)}")
    extra = []
    if item["floor"] is not None:
        extra.append(f"קומה {item['floor']}")
    extra.append("תיווך" if item["is_agency"] else "פרטי")
    if item["search_name"]:
        extra.append(item["search_name"])
    parts.append(e(" · ".join(str(x) for x in extra)))
    for b in item.get("badges") or []:
        parts.append(e(b))
    parts.append(f'🔗 <a href="{item["link"]}">לפתיחה ביד2</a>')
    caption = "\n".join(parts)[:1000]

    if item["image"]:
        if tg("sendPhoto", photo=item["image"], caption=caption, parse_mode="HTML"):
            return True
    return send_text(caption)


# ---------------------------------------------------------------- yad2

def is_blocked(text):
    low = text.lower()
    return "__NEXT_DATA__" not in text and any(m in low for m in BLOCK_MARKERS)


def fetch_requests(url):
    r = requests.get(url, headers=HEADERS, timeout=30)
    return r.text


def fetch_browser(url):
    """גיבוי: דפדפן אמיתי (Chrome מותקן מראש בשרתי GitHub)."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return ""
    with sync_playwright() as p:
        try:
            browser = p.chromium.launch(channel="chrome", headless=True)
        except Exception:
            browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(
            user_agent=HEADERS["User-Agent"], locale="he-IL", viewport={"width": 1366, "height": 900}
        )
        page = ctx.new_page()
        page.goto(url, wait_until="domcontentloaded", timeout=45000)
        try:
            page.wait_for_selector("#__NEXT_DATA__", state="attached", timeout=20000)
        except Exception:
            pass
        content = page.content()
        browser.close()
        return content


def extract_next_data(text):
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', text, re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(1))
    except json.JSONDecodeError:
        return None


def looks_like_listing(d):
    return (
        isinstance(d.get("token"), str)
        and len(d["token"]) >= 5
        and "price" in d
        and ("address" in d or "additionalDetails" in d)
    )


def normalize(d, search_name):
    token = d["token"]
    addr = d.get("address") or {}
    street = g(addr, "street", "text")
    house_no = g(addr, "house", "number")
    if street and house_no:
        street = f"{street} {house_no}"
    details = d.get("additionalDetails") or {}
    meta = d.get("metaData") or {}
    image = meta.get("coverImage") or g(meta, "images", 0)
    ad_type = str(d.get("adType") or "").lower()
    return {
        "token": token,
        "price": to_num(d.get("price")),
        "rooms": to_num(details.get("roomsCount")),
        "sqm": to_num(details.get("squareMeter")),
        "city": g(addr, "city", "text"),
        "neighborhood": g(addr, "neighborhood", "text"),
        "street": street,
        "floor": g(addr, "house", "floor"),
        "image": image if isinstance(image, str) and image.startswith("http") else None,
        "is_agency": ad_type in ("agency", "commercial") or bool(d.get("isAgency")),
        "text": all_strings(d),
        "link": f"https://www.yad2.co.il/realestate/item/{token}",
        "search_name": search_name,
    }


def scrape(search):
    url, name = search["url"], search.get("name", "")
    text = ""
    try:
        text = fetch_requests(url)
    except requests.RequestException as e:
        print("request failed:", e)
    if not text or is_blocked(text) or "__NEXT_DATA__" not in text:
        print(f"[{name}] direct fetch blocked/empty, trying browser...")
        try:
            text = fetch_browser(url)
        except Exception as e:
            print("browser failed:", e)
            text = ""
    data = extract_next_data(text) if text else None
    if data is None:
        return None  # חסום או מבנה לא מוכר
    items = {}
    for d in walk(data):
        if looks_like_listing(d):
            it = normalize(d, name)
            items.setdefault(it["token"], it)
    return list(items.values())


def fetch_details(item):
    """פותח את עמוד המודעה עצמה כדי לקרוא תיאור מלא ופרטי נכס (חניה וכו')."""
    url = item["link"]
    text = ""
    try:
        text = fetch_requests(url)
    except requests.RequestException:
        pass
    if not text or "__NEXT_DATA__" not in text:
        try:
            text = fetch_browser(url)
        except Exception:
            text = ""
    data = extract_next_data(text) if text else None
    if data is None:
        return False
    item["text"] += " " + all_strings(data)
    for d in walk(data):
        for k, v in d.items():
            kl = str(k).lower()
            if kl.startswith("parking") and isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0:
                item["parking_field"] = True
            if kl.startswith("parking") and v is True:
                item["parking_field"] = True
    return True


# ---------------------------------------------------------------- filters

PARKING_NEG = ("ללא חניה", "ללא חנייה", "אין חניה", "אין חנייה", "בלי חניה", "בלי חנייה")
PARKING_POS = ("חניה", "חנייה", "חניון", "מקום חניה")
NO_PERMIT = ("ללא תו", "אין תו", "לא זכאי לתו", "לא זכאית לתו", "אין זכאות לתו", "ללא זכאות לתו")
BRIGHT_WORDS = ("מוארת", "מואר", "שטופת אור", "שטופה באור", "הרבה אור", "אור טבעי",
                "כיווני אוויר", "כיווני אויר", "חלונות גדולים", "נוף פתוח")


def smart_check(item, cfg):
    """בדיקות שדורשות קריאה של המודעה. מחזיר (עובר?, תגיות להודעה)."""
    text = item["text"]
    badges = []

    pk = cfg.get("parking") or {}
    if pk.get("required"):
        no_permit = any(w in text for w in NO_PERMIT)
        # מוציאים אזכורים של "תו חניה" כדי שלא ייספרו כחניה בדירה
        t = re.sub(r"תו\s*(ה)?חני[י]?ה", " ", text)
        neg = any(w in t for w in PARKING_NEG)
        pos = item.get("parking_field") or (not neg and any(w in t for w in PARKING_POS))
        if pos:
            badges.append("🅿️ חניה בדירה")
        elif no_permit:
            return False, []          # אין חניה וגם אין זכאות לתו
        elif re.search(r"תו\s*(ה)?חני[י]?ה", text):
            badges.append("🅿️ בלי חניה צמודה, כתוב שיש אפשרות לתו חניה")
        else:
            badges.append("🅿️ בלי חניה צמודה - לוודא זכאות לתו חניה")

    br = cfg.get("brightness") or {}
    if br:
        if any(w in text for w in (br.get("exclude_words") or []) if w):
            return False, []
        if any(w in text for w in BRIGHT_WORDS):
            badges.insert(0, "☀️ מוארת")
        elif br.get("strict"):
            return False, []

    prio = [p for p in (cfg.get("priority_neighborhoods") or []) if p]
    item["priority"] = any(p in (item["neighborhood"] or "") for p in prio)
    return True, badges


def matches(item, f):
    p, r, s = item["price"], item["rooms"], item["sqm"]
    if f.get("max_price") and p and p > f["max_price"]:
        return False
    if f.get("min_price") and p and p < f["min_price"]:
        return False
    if f.get("skip_no_price") and not p:
        return False
    if f.get("min_rooms") and r and r < f["min_rooms"]:
        return False
    if f.get("max_rooms") and r and r > f["max_rooms"]:
        return False
    if f.get("min_sqm") and s and s < f["min_sqm"]:
        return False
    if f.get("exclude_agency") and item["is_agency"]:
        return False
    banned = [h.strip() for h in (f.get("exclude_neighborhoods") or []) if h and h.strip()]
    where = " ".join(str(x) for x in (item["neighborhood"], item["street"], item["text"]) if x)
    if any(b in where for b in banned):
        return False
    hoods = [h.strip() for h in (f.get("neighborhoods") or []) if h and h.strip()]
    if hoods and not any(h in (item["neighborhood"] or "") for h in hoods):
        return False
    text = item["text"]
    if any(k in text for k in (f.get("exclude_keywords") or []) if k):
        return False
    must = [k for k in (f.get("must_include_any") or []) if k]
    if must and not any(k in text for k in must):
        return False
    return True


# ---------------------------------------------------------------- main

def main():
    cfg = yaml.safe_load(CONFIG_FILE.read_text(encoding="utf-8")) or {}
    searches = [s for s in (cfg.get("yad2_searches") or []) if s.get("url", "").startswith("http")]
    filters = cfg.get("filters") or {}
    alert_price_drops = cfg.get("alert_price_drops", True)

    if "--test" in sys.argv:
        ok = send_text("✅ סוכן הדירות מחובר לטלגרם ועובד!")
        print("sent" if ok else "failed")
        return

    if not searches:
        print("אין חיפושים ב-config.yaml - הדביקי לינק חיפוש מיד2 תחת yad2_searches")
        return

    state = load_state()
    seen = state.setdefault("seen", {})
    now = int(time.time())
    first_run = not state.get("seeded")
    blocked_searches, new_items, drops = [], [], []
    total_found = 0

    for search in searches:
        items = scrape(search)
        if items is None:
            blocked_searches.append(search.get("name") or search["url"])
            continue
        total_found += len(items)
        print(f"[{search.get('name', '')}] {len(items)} listings on page")
        for it in items:
            prev = seen.get(it["token"])
            if prev is None:
                seen[it["token"]] = {"t": now, "p": it["price"]}
                if not first_run and matches(it, filters):
                    new_items.append(it)
            else:
                old_p = prev.get("p")
                if it["price"] and old_p and it["price"] < old_p:
                    if alert_price_drops and matches(it, filters):
                        drops.append((it, old_p))
                if it["price"] and it["price"] != old_p:
                    prev["p"] = it["price"]

    # בדיקה מעמיקה: פותחים כל מודעה חדשה שעברה את הסינון הבסיסי
    final = []
    for it, title in [(i, "🏠 דירה חדשה") for i in new_items] + \
                     [(i, f"📉 ירידת מחיר (היה ₪{p:,})") for i, p in drops]:
        verified = fetch_details(it)
        time.sleep(1.5)
        if not matches(it, filters):      # בדיקה חוזרת עם התיאור המלא
            continue
        ok, badges = smart_check(it, cfg)
        if not ok:
            continue
        if not verified:
            badges.append("⚠️ לא הצלחתי לפתוח את המודעה המלאה - לבדוק ידנית")
        it["badges"] = badges
        final.append((it, title))

    final.sort(key=lambda x: not x[0].get("priority"))   # מרכז העיר קודם
    for it, title in final:
        if it.get("priority"):
            title = "⭐ " + title + " במיקום מועדף"
        send_listing(it, title=title)
        time.sleep(1)

    if first_run and total_found:
        state["seeded"] = True
        send_text(
            f"🟢 סוכן הדירות פעיל!\nסרקתי {total_found} מודעות קיימות וסימנתי אותן כ\"נראו\".\n"
            "מעכשיו תקבלי כאן רק דירות חדשות שמתאימות לקריטריונים שלך."
        )

    if blocked_searches and now - state.get("last_block_alert", 0) > BLOCK_ALERT_EVERY:
        state["last_block_alert"] = now
        send_text(
            "⚠️ יד2 חסם זמנית את הסריקה (" + ", ".join(blocked_searches) + ").\n"
            "בדרך כלל זה עובר לבד בריצות הבאות. אם זה חוזר הרבה - כדאי להריץ את הסוכן מהמחשב."
        )

    # ניקוי מודעות ישנות מהזיכרון
    cutoff = now - KEEP_SEEN_DAYS * 86400
    for tok in [t for t, v in seen.items() if v.get("t", now) < cutoff]:
        del seen[tok]

    save_state(state)
    print(f"done: {len(new_items)} candidates, {len(final)} sent, blocked={blocked_searches}")


if __name__ == "__main__":
    main()
