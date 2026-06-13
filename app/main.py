import os
import json
import base64
import hashlib
import time
import uuid
import logging
import requests
from io import BytesIO
from PIL import Image
from datetime import datetime
from flask import Flask, request, jsonify, Response
from pywa.utils import default_flow_request_decryptor, default_flow_response_encryptor

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

MIDTRANS_SERVER_KEY = os.environ.get("MIDTRANS_SERVER_KEY", "")
MIDTRANS_IS_PRODUCTION = os.environ.get("MIDTRANS_IS_PRODUCTION", "false").lower() == "true"
WA_PHONE_NUMBER_ID = os.environ.get("WA_PHONE_NUMBER_ID", "")
WA_ACCESS_TOKEN = os.environ.get("WA_ACCESS_TOKEN", "")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
FLOW_PRIVATE_KEY_PEM = os.environ.get("FLOW_PRIVATE_KEY", "")

# Meta Pixel / Conversions API (Raihmimpi)
META_PIXEL_ID = os.environ.get("META_PIXEL_ID", "404823950728687")
META_PIXEL_ACCESS_TOKEN = os.environ.get("META_PIXEL_ACCESS_TOKEN", "")

MIDTRANS_BASE_URL = (
    "https://app.midtrans.com/snap/v1/transactions"
    if MIDTRANS_IS_PRODUCTION
    else "https://app.sandbox.midtrans.com/snap/v1/transactions"
)
RAIHMIMPI_API = "https://api.raihmimpi.id/campaign"

def get_private_key_pem():
    import base64 as b64
    key_b64 = os.environ.get("FLOW_PRIVATE_KEY_B64", "")
    if key_b64:
        pem = b64.b64decode(key_b64).decode()
    else:
        pem = FLOW_PRIVATE_KEY_PEM.replace("\\n", "\n")
    return pem

def decrypt_request(body):
    decrypted, aes_key, iv = default_flow_request_decryptor(
        body["encrypted_flow_data"],
        body["encrypted_aes_key"],
        body["initial_vector"],
        get_private_key_pem()
    )
    return decrypted, aes_key, iv

def encrypt_response(response_data, aes_key, iv):
    return default_flow_response_encryptor(response_data, aes_key, iv)

_campaigns_cache = {"data": None, "ts": 0}

def get_campaigns(full=False):
    """Ambil kampanye dari API Raihmimpi.
    - full=False (default): return 5 default (page=1) — untuk Flow donasi, cepat
    - full=True: return SEMUA kampanye (page=100 → plateau ~455), dengan cache 5 menit"""
    import time
    if full:
        now = time.time()
        if _campaigns_cache["data"] is not None and (now - _campaigns_cache["ts"]) < 300:
            return _campaigns_cache["data"]
        try:
            resp = requests.get(RAIHMIMPI_API + "?page=100", timeout=30)
            resp.raise_for_status()
            data = resp.json()
            _campaigns_cache["data"] = data
            _campaigns_cache["ts"] = now
            logger.info(f"get_campaigns(full=True) refreshed: {len(data)} kampanye")
            return data
        except Exception as e:
            logger.error(f"Error ambil semua kampanye: {e}")
            return _campaigns_cache["data"] or []
    try:
        resp = requests.get(RAIHMIMPI_API, timeout=10)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        logger.error(f"Error ambil kampanye: {e}")
        return []

def fetch_and_resize_image(url, max_size_kb=60, target_dim=120):
    """Fetch gambar dari URL, resize, dan convert ke base64 (max ~90KB)"""
    try:
        resp = requests.get(url, timeout=8)
        resp.raise_for_status()
        img = Image.open(BytesIO(resp.content))
        img = img.convert("RGB")

        # Resize ke target dimension (square crop center)
        img.thumbnail((target_dim, target_dim))

        quality = 80
        while quality > 20:
            buf = BytesIO()
            img.save(buf, format="JPEG", quality=quality)
            size_kb = buf.tell() / 1024
            if size_kb <= max_size_kb:
                break
            quality -= 10

        return base64.b64encode(buf.getvalue()).decode("utf-8")
    except Exception as e:
        logger.error(f"Error resize image {url}: {e}")
        # 1x1 transparent pixel fallback (base64 PNG)
        return "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUAAarVyFEAAAAASUVORK5CYII="

def format_rupiah(amount):
    try:
        return f"Rp {int(amount):,}".replace(",", ".")
    except:
        return str(amount)

def filter_kampanye_aktif(campaigns):
    """Filter kampanye berdasarkan kampanye_aktif di settings.json.
    Kalau kosong/tidak ada, return semua (fallback default)."""
    try:
        settings = load_settings()
        aktif_ids = settings.get("kampanye_aktif", [])
        if not aktif_ids:
            return campaigns
        aktif_ids_str = [str(x) for x in aktif_ids]
        filtered = [c for c in campaigns if str(c.get("ID_CAMPAIGN", "")) in aktif_ids_str]
        return filtered if filtered else campaigns
    except Exception as e:
        logger.error(f"Error filter kampanye aktif: {e}")
        return campaigns

def format_campaigns_for_flow(campaigns, limit=10):
    campaigns = filter_kampanye_aktif(campaigns)
    result = []
    for c in campaigns[:limit]:
        campaign_id = str(c.get("ID_CAMPAIGN", ""))
        name = c.get("CAMPAIGN_NAME", "")[:72]
        terkumpul = format_rupiah(c.get("TOTAL_DONASI", 0))
        target = format_rupiah(c.get("TARGET_DONASI_UANG", 0))
        result.append({"id": campaign_id, "title": name, "description": f"Terkumpul: {terkumpul} dari {target}"[:72]})
    return result

def format_campaigns_with_images(campaigns, limit=5, tipe_donasi=""):
    """Format kampanye dengan gambar base64 untuk NavigationList.
    tipe_donasi arg di-accept untuk backward compat tapi tidak dipakai (NavigationList Flow 7.x handle navigation sendiri)."""
    campaigns = filter_kampanye_aktif(campaigns)
    result = []
    for c in campaigns[:limit]:
        campaign_id = str(c.get("ID_CAMPAIGN", ""))
        name = c.get("CAMPAIGN_NAME", "")[:30]
        terkumpul_raw = c.get("TOTAL_DONASI", 0)
        target_raw = c.get("TARGET_DONASI_UANG", 0)
        try:
            pct = min(100, round(int(terkumpul_raw) / int(target_raw) * 100))
        except:
            pct = 0
        terkumpul = format_rupiah(terkumpul_raw)

        img_url = c.get("IMG_MOBILE") or c.get("IMG_CAMPAIGNER") or c.get("IMG_BIG", "")
        image_b64 = fetch_and_resize_image(img_url) if img_url else ""

        result.append({
            "id": campaign_id,
            "main-content": {
                "title": name,
                "description": f"Terkumpul {pct}%"[:20]
            },
            "start": {
                "image": image_b64
            }
        })
    return result

def create_midtrans_payment(order_id, amount, donatur_name, phone, campaign_name):
    import base64 as b64
    auth = b64.b64encode(f"{MIDTRANS_SERVER_KEY}:".encode()).decode()
    payload = {
        "transaction_details": {"order_id": order_id, "gross_amount": int(amount)},
        "customer_details": {"first_name": donatur_name, "phone": phone},
        "item_details": [{"id": "DONASI-001", "price": int(amount), "quantity": 1, "name": f"Donasi: {campaign_name[:50]}"}],
        "callbacks": {"finish": f"https://raihmimpi.id/donasi-sukses?order_id={order_id}"}
    }
    resp = requests.post(MIDTRANS_BASE_URL, json=payload,
        headers={"Authorization": f"Basic {auth}", "Content-Type": "application/json"}, timeout=15)
    resp.raise_for_status()
    return resp.json().get("redirect_url")

def send_wa_message(to_phone, message):
    url = f"https://graph.facebook.com/v22.0/{WA_PHONE_NUMBER_ID}/messages"
    resp = requests.post(url, json={"messaging_product": "whatsapp", "to": to_phone, "type": "text", "text": {"body": message}},
        headers={"Authorization": f"Bearer {WA_ACCESS_TOKEN}"}, timeout=10)
    logger.info(f"send_wa_message to={to_phone} status={resp.status_code} body={resp.text[:200]}")
    return resp

def send_wa_buttons(to_phone, body_text, buttons):
    """Kirim interactive reply button message via Graph API.
    buttons: list of {"id": "...", "title": "..."} (max 3, title max 20 char)"""
    url = f"https://graph.facebook.com/v22.0/{WA_PHONE_NUMBER_ID}/messages"
    payload = {
        "messaging_product": "whatsapp",
        "to": to_phone,
        "type": "interactive",
        "interactive": {
            "type": "button",
            "body": {"text": body_text},
            "action": {
                "buttons": [
                    {"type": "reply", "reply": {"id": b["id"], "title": b["title"][:20]}}
                    for b in buttons
                ]
            }
        }
    }
    resp = requests.post(url, json=payload,
        headers={"Authorization": f"Bearer {WA_ACCESS_TOKEN}"}, timeout=10)
    logger.info(f"send_wa_buttons to={to_phone} status={resp.status_code} body={resp.text[:200]}")
    return resp

def send_menu_utama(to_phone):
    """Kirim pesan Menu Utama (sapaan + tombol aksi) ke kontak baru."""
    settings = load_settings()
    menu = settings.get("menu_utama", {})
    message = menu.get("message", "Halo! Yuk donasi via Raihmimpi.")
    buttons_cfg = menu.get("buttons", [])
    buttons = []
    for b in buttons_cfg:
        if not b.get("enabled"):
            continue
        action = b.get("action", "donasi")
        btn_id = "btn_donasi" if action == "donasi" else "btn_admin"
        buttons.append({"id": btn_id, "title": b.get("label", "")})
    if not buttons:
        buttons = [{"id": "btn_donasi", "title": "Mulai Donasi"}]
    return send_wa_buttons(to_phone, message, buttons[:3])

# ID Flow donasi (didapat dari WhatsApp Manager > Flows)
DONASI_FLOW_ID = os.environ.get("DONASI_FLOW_ID", "")

# Kata kunci yang men-trigger Flow donasi
DONASI_KEYWORDS = ["donasi", "infak", "infaq", "sedekah", "zakat", "wakaf", "berdonasi", "donatur"]

def send_wa_flow_message(to_phone, body_text="Yuk mulai donasi via Raihmimpi 🤲", cta_text="Mulai Donasi", screen="PILIH_TIPE"):
    """Kirim interactive Flow message langsung ke user (dalam window 24 jam, tanpa template)."""
    url = f"https://graph.facebook.com/v19.0/{WA_PHONE_NUMBER_ID}/messages"
    flow_token = f"phone_{to_phone}"
    payload = {
        "messaging_product": "whatsapp",
        "to": to_phone,
        "type": "interactive",
        "interactive": {
            "type": "flow",
            "body": {"text": body_text},
            "action": {
                "name": "flow",
                "parameters": {
                    "flow_message_version": "3",
                    "flow_token": flow_token,
                    "flow_id": DONASI_FLOW_ID,
                    "flow_cta": cta_text,
                    "flow_action": "data_exchange"
                }
            }
        }
    }
    resp = requests.post(url, json=payload,
        headers={"Authorization": f"Bearer {WA_ACCESS_TOKEN}", "Content-Type": "application/json"}, timeout=10)
    logger.info(f"send_wa_flow_message status={resp.status_code} body={resp.text}")
    return resp

# ============================================================
# Halosis API (alternatif kirim Flow tanpa Graph API langsung)
# ============================================================
HALOSIS_API_BASE = "https://api.halosis.id/v1"
HALOSIS_EMAIL = os.environ.get("HALOSIS_EMAIL", "")
HALOSIS_PASSWORD = os.environ.get("HALOSIS_PASSWORD", "")
HALOSIS_LONG_TOKEN = os.environ.get("HALOSIS_LONG_TOKEN", "")  # cache long-lived token (60 hari)
HALOSIS_FROM_PHONE = os.environ.get("HALOSIS_FROM_PHONE", "6281316316135")
HALOSIS_DONASI_FLOW_ID = os.environ.get("HALOSIS_DONASI_FLOW_ID", "") or DONASI_FLOW_ID

def halosis_login():
    """Login email+password -> dapat refresh_token (valid 24 jam)."""
    resp = requests.post(f"{HALOSIS_API_BASE}/login",
        json={"email": HALOSIS_EMAIL, "password": HALOSIS_PASSWORD}, timeout=15)
    logger.info(f"halosis_login status={resp.status_code} body={resp.text[:300]}")
    resp.raise_for_status()
    return resp.json().get("refresh_token")

def halosis_get_access_token():
    """refresh_token -> long_lived_token (valid 60 hari)."""
    refresh_token = halosis_login()
    resp = requests.post(f"{HALOSIS_API_BASE}/access-token",
        json={"refresh_token": refresh_token}, timeout=15)
    logger.info(f"halosis_access_token status={resp.status_code} body={resp.text[:300]}")
    resp.raise_for_status()
    return resp.json().get("long_lived_token")

def halosis_send_flow_message(to_phone, message="Yuk mulai donasi via Raihmimpi 🤲"):
    """Kirim WhatsApp Flow message via Halosis API (/v1/messages, type=flow)."""
    token = HALOSIS_LONG_TOKEN or halosis_get_access_token()
    payload = {
        "from_phone_number": HALOSIS_FROM_PHONE,
        "to": to_phone,
        "type": "flow",
        "message": message,
        "flow_id": HALOSIS_DONASI_FLOW_ID
    }
    resp = requests.post(f"{HALOSIS_API_BASE}/messages", json=payload,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"}, timeout=15)
    logger.info(f"halosis_send_flow status={resp.status_code} body={resp.text[:500]}")
    return resp

def notify_telegram(text):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    requests.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
        json={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"}, timeout=10)

def hash_sha256(value):
    """Hash nilai (lowercase, trimmed) dengan SHA256 sesuai standar Meta CAPI."""
    if not value:
        return None
    return hashlib.sha256(str(value).strip().lower().encode("utf-8")).hexdigest()

def send_pixel_event(event_name, phone=None, value=None, currency="IDR", event_id=None,
                      content_name=None, content_ids=None, source_url=None):
    """
    Kirim event ke Meta Conversions API (Pixel Raihmimpi).
    - phone: nomor WA donatur (62...), dipakai sebagai 'ph' (hashed) untuk matching.
    - event_id: untuk deduplikasi jika nanti dikombinasikan dengan Pixel browser-side.
    - source_url: action_source dianggap 'business_messaging' karena ini dari WhatsApp Flow.
    """
    if not META_PIXEL_ACCESS_TOKEN:
        logger.warning(f"send_pixel_event SKIP (no META_PIXEL_ACCESS_TOKEN): {event_name}")
        return None

    user_data = {}
    if phone:
        # Normalisasi: pastikan format 62xxxxxxxxxx tanpa '+'
        clean = phone.replace("+", "").replace(" ", "").replace("-", "")
        user_data["ph"] = [hash_sha256(clean)]

    custom_data = {"currency": currency}
    if value is not None:
        custom_data["value"] = float(value)
    if content_name:
        custom_data["content_name"] = content_name
    if content_ids:
        custom_data["content_ids"] = content_ids

    payload = {
        "data": [{
            "event_name": event_name,
            "event_time": int(time.time()),
            "event_id": event_id or str(uuid.uuid4()),
            "action_source": "business_messaging",
            "messaging_channel": "whatsapp",
            "user_data": user_data,
            "custom_data": custom_data,
        }]
    }

    try:
        resp = requests.post(
            f"https://graph.facebook.com/v22.0/{META_PIXEL_ID}/events",
            params={"access_token": META_PIXEL_ACCESS_TOKEN},
            json=payload, timeout=10)
        logger.info(f"send_pixel_event {event_name} status={resp.status_code} body={resp.text[:300]}")
        return resp
    except Exception as e:
        logger.error(f"send_pixel_event {event_name} failed: {e}", exc_info=True)
        return None

DATA_FILE = os.environ.get("DATA_DIR", "/tmp") + "/transaksi.json"

def load_data():
    if not os.path.exists(DATA_FILE):
        return []
    with open(DATA_FILE, "r") as f:
        return json.load(f)

def save_data(data):
    with open(DATA_FILE, "w") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

# ============================================================
# Inbox / Percakapan WhatsApp (untuk fitur Inbox mirip Halosis)
# ============================================================
INBOX_FILE = os.environ.get("DATA_DIR", "/tmp") + "/inbox.json"

def load_inbox():
    """Struktur: {"contacts": {phone: {"name":..., "phone":..., "labels":[...], "status":"perlu_dibalas|otomatis|selesai",
       "last_message":..., "last_message_at":..., "unread": int}}, "messages": {phone: [ {direction, type, text, timestamp} ]}}"""
    if not os.path.exists(INBOX_FILE):
        return {"contacts": {}, "messages": {}}
    with open(INBOX_FILE, "r") as f:
        return json.load(f)

def save_inbox(inbox):
    with open(INBOX_FILE, "w") as f:
        json.dump(inbox, f, ensure_ascii=False, indent=2)

# ============================================================
# Settings (Menu Utama, dll) - sistem sendiri, tidak terhubung Halosis
# ============================================================
SETTINGS_FILE = os.environ.get("DATA_DIR", "/tmp") + "/settings.json"

DEFAULT_SETTINGS = {
    "menu_utama": {
        "message": "Halo! kak, sekarang kakak bisa donasi via WhatsApp di Raihmimpi.\n\nYuk, coba donasi kak!",
        "buttons": [
            {"enabled": True, "label": "Saya mau donasi", "action": "donasi"},
            {"enabled": True, "label": "Saya mau WA admin", "action": "admin"},
            {"enabled": False, "label": "Kembali ke Donasi", "action": "donasi"},
        ],
    }
}

def load_settings():
    if not os.path.exists(SETTINGS_FILE):
        return DEFAULT_SETTINGS.copy()
    with open(SETTINGS_FILE, "r") as f:
        data = json.load(f)
    merged = DEFAULT_SETTINGS.copy()
    merged.update(data)
    return merged

def save_settings(settings):
    with open(SETTINGS_FILE, "w") as f:
        json.dump(settings, f, ensure_ascii=False, indent=2)

def record_incoming_message(phone, text, msg_type="text", name=None):
    """Simpan pesan masuk ke inbox, update info kontak (unread, last_message, status)."""
    inbox = load_inbox()
    contacts = inbox.setdefault("contacts", {})
    messages = inbox.setdefault("messages", {})

    contact = contacts.get(phone, {})
    contact["phone"] = phone
    if name and not contact.get("name"):
        contact["name"] = name
    elif not contact.get("name"):
        contact["name"] = phone
    contact.setdefault("labels", [])
    contact["status"] = "perlu_dibalas"
    contact["last_message"] = text if msg_type == "text" else f"[{msg_type.upper()}]"
    contact["last_message_at"] = datetime.now().isoformat()
    contact["unread"] = contact.get("unread", 0) + 1
    contacts[phone] = contact

    msg_list = messages.setdefault(phone, [])
    msg_list.append({
        "direction": "in",
        "type": msg_type,
        "text": text,
        "timestamp": datetime.now().isoformat(),
    })

    save_inbox(inbox)
    return contact

def record_outgoing_message(phone, text, msg_type="text"):
    """Simpan pesan keluar (balasan admin) ke inbox."""
    inbox = load_inbox()
    contacts = inbox.setdefault("contacts", {})
    messages = inbox.setdefault("messages", {})

    contact = contacts.get(phone, {"phone": phone, "name": phone, "labels": [], "unread": 0})
    contact["last_message"] = text if msg_type == "text" else f"[{msg_type.upper()}]"
    contact["last_message_at"] = datetime.now().isoformat()
    contacts[phone] = contact

    msg_list = messages.setdefault(phone, [])
    msg_list.append({
        "direction": "out",
        "type": msg_type,
        "text": text,
        "timestamp": datetime.now().isoformat(),
    })

    save_inbox(inbox)
    return contact

def handle_flow_request(decrypted_body):
    action = decrypted_body.get("action")
    screen = decrypted_body.get("screen")
    data = decrypted_body.get("data", {})
    flow_token = decrypted_body.get("flow_token", "")

    logger.info(f"Flow action={action} screen={screen}")

    if action == "ping":
        return {"version": "3.0", "data": {"status": "active"}}

    if action == "INIT":
        campaigns = get_campaigns(full=True)
        phone_init = flow_token.replace("phone_", "") if flow_token.startswith("phone_") else ""
        send_pixel_event("ViewContent", phone=phone_init, currency="IDR",
                          content_name="Donasi via WA Raihmimpi")
        return {"screen": "PILIH_TIPE", "data": {"kampanye_list": format_campaigns_with_images(campaigns)}}

    if action == "data_exchange":
        if screen == "PILIH_KAMPANYE":
            logger.info(f"PILIH_KAMPANYE FULL_BODY: {json.dumps(decrypted_body)[:1000]}")
            tipe_donasi = str(data.get("tipe_donasi", "sekali"))
            # NavigationList Flow 7.x kirim ID item yang diklik via ${form.kampanye_nav}
            # yang sudah di-resolve ke field kampanye_id di payload
            kampanye_id = ""
            for key in ["kampanye_id", "kampanye_nav", "id", "selected_id"]:
                val = data.get(key)
                if isinstance(val, list) and val:
                    kampanye_id = str(val[0])
                    break
                elif isinstance(val, str) and val and not val.startswith("${"):
                    kampanye_id = val
                    break

            logger.info(f"PILIH_KAMPANYE: data_keys={list(data.keys())} kampanye_id={kampanye_id} tipe={tipe_donasi}")

            if kampanye_id:
                # User pilih kampanye -> fetch nama dari API berdasarkan ID
                kampanye_nama = "Kampanye Raihmimpi"
                try:
                    campaigns = get_campaigns(full=True)
                    for c in campaigns:
                        if str(c.get("ID_CAMPAIGN", "")) == kampanye_id:
                            kampanye_nama = c.get("CAMPAIGN_NAME", "Kampanye Raihmimpi")
                            break
                except Exception as e:
                    logger.error(f"Error fetch nama kampanye: {e}")
                logger.info(f"PILIH_KAMPANYE resolved: id={kampanye_id} nama={kampanye_nama}")
                return {"screen": "PILIH_NOMINAL", "data": {
                    "tipe_donasi": tipe_donasi,
                    "kampanye_id": kampanye_id,
                    "kampanye_nama": kampanye_nama,
                }}
            else:
                # Belum ada kampanye dipilih -> return list kampanye (fallback, biasanya tidak terjadi)
                campaigns = get_campaigns(full=True)
                return {"screen": "PILIH_KAMPANYE", "data": {
                    "kampanye_list": format_campaigns_with_images(campaigns, tipe_donasi=tipe_donasi),
                    "tipe_donasi": tipe_donasi
                }}

        if screen == "PILIH_NOMINAL":
            # User klik "Isi Data Donatur" -> trigger AddToCart, lanjut ke screen DATA_DONATUR
            kampanye_id = str(data.get("kampanye_id", ""))
            kampanye_nama = str(data.get("kampanye_nama", ""))
            # Fix: kalau kampanye_nama masih template variable, fetch dari API berdasarkan ID
            if not kampanye_nama or kampanye_nama.startswith("${"):
                try:
                    campaigns = get_campaigns(full=True)
                    for c in campaigns:
                        if str(c.get("id")) == kampanye_id:
                            kampanye_nama = c.get("main-content", {}).get("title", "Kampanye Raihmimpi")
                            break
                    if not kampanye_nama or kampanye_nama.startswith("${"):
                        kampanye_nama = campaigns[0].get("main-content", {}).get("title", "Kampanye Raihmimpi") if campaigns else "Kampanye Raihmimpi"
                except Exception:
                    kampanye_nama = "Kampanye Raihmimpi"
            logger.info(f"PILIH_NOMINAL: kampanye_id={kampanye_id} kampanye_nama={kampanye_nama}")
            nominal = data.get("nominal", "50000")
            nominal_lain = data.get("nominal_lain", 0)
            try:
                final_nominal = int(nominal_lain) if nominal_lain and int(nominal_lain) > 0 else int(nominal)
            except (ValueError, TypeError):
                final_nominal = int(nominal) if str(nominal).isdigit() else 50000

            phone_atc = flow_token.replace("phone_", "") if flow_token.startswith("phone_") else ""
            send_pixel_event("AddToCart", phone=phone_atc, value=final_nominal, currency="IDR",
                              event_id=f"atc_{flow_token}_{kampanye_id}",
                              content_name=kampanye_nama,
                              content_ids=[kampanye_id] if kampanye_id else None)

            try:
                nominal_lain_int = int(nominal_lain) if nominal_lain else 0
            except (ValueError, TypeError):
                nominal_lain_int = 0

            return {"screen": "DATA_DONATUR", "data": {
                "tipe_donasi": data.get("tipe_donasi", "sekali"),
                "kampanye_id": str(kampanye_id),
                "kampanye_nama": str(kampanye_nama),
                "nominal": str(nominal),
                "nominal_lain": nominal_lain_int,
            }}

        if screen == "DATA_DONATUR":
            # User klik Lihat Konfirmasi -> return data ke screen KONFIRMASI
            try:
                nominal_lain_val = int(data.get("nominal_lain", 0) or 0)
            except (ValueError, TypeError):
                nominal_lain_val = 0
            nominal_raw = str(data.get("nominal", "50000"))
            try:
                nominal_int = nominal_lain_val if nominal_lain_val > 0 else int(nominal_raw)
            except (ValueError, TypeError):
                nominal_int = 50000
            nominal_display = f"Rp {nominal_int:,}".replace(",", ".")
            return {"screen": "KONFIRMASI", "data": {
                "tipe_donasi": str(data.get("tipe_donasi", "sekali")),
                "kampanye_id": str(data.get("kampanye_id", "")),
                "kampanye_nama": str(data.get("kampanye_nama", "Kampanye Raihmimpi")),
                "nominal": nominal_raw,
                "nominal_lain": nominal_lain_val,
                "nominal_display": nominal_display,
                "nama_donatur": str(data.get("nama_donatur", "Donatur")),
                "atas_nama": str(data.get("atas_nama", "")),
            }}

        if screen == "KONFIRMASI":
            logger.info(f"KONFIRMASI data: {data}")
            logger.info(f"KONFIRMASI flow_token: {flow_token}")
            try:
                nama_donatur = str(data.get("nama_donatur", "Donatur"))
                kampanye_id = str(data.get("kampanye_id", ""))
                kampanye_nama = str(data.get("kampanye_nama", "Kampanye Raihmimpi"))
                nominal = str(data.get("nominal", "50000"))
                nominal_lain = data.get("nominal_lain", 0)
                atas_nama = str(data.get("atas_nama", nama_donatur))
                tipe = str(data.get("tipe_donasi", "sekali"))
                logger.info(f"KONFIRMASI step1 OK: nominal={nominal} kampanye_nama={kampanye_nama}")

                # Fix template variables yang tidak ter-resolve dari Flow JSON lama
                if not kampanye_id or kampanye_id.startswith("${"):
                    kampanye_id = "unknown"
                if kampanye_nama.startswith("${"):
                    try:
                        kampanye_list = get_campaigns(full=True)
                        if kampanye_list:
                            kampanye_nama = kampanye_list[0].get("main-content", {}).get("title", "Kampanye Raihmimpi")
                            kampanye_id = str(kampanye_list[0].get("id", "unknown"))
                        logger.info(f"KONFIRMASI kampanye fetch OK: {kampanye_nama}")
                    except Exception as e2:
                        logger.error(f"KONFIRMASI kampanye fetch error: {e2}")
                        kampanye_nama = "Kampanye Raihmimpi"

                try:
                    nominal_lain_int = int(nominal_lain) if nominal_lain else 0
                except (ValueError, TypeError):
                    nominal_lain_int = 0

                try:
                    final_nominal = nominal_lain_int if nominal_lain_int > 0 else int(nominal)
                except (ValueError, TypeError):
                    final_nominal = 50000

                logger.info(f"KONFIRMASI step2 OK: final_nominal={final_nominal}")
                phone = flow_token.replace("phone_", "") if flow_token and flow_token.startswith("phone_") else ""
                kid_clean = kampanye_id[-6:].replace(".", "").replace("}", "").replace("{", "")
                order_id = f"RM-{datetime.now().strftime('%Y%m%d%H%M%S')}-{kid_clean}"
                logger.info(f"KONFIRMASI step3 OK: order_id={order_id} phone={phone}")

                payment_url = create_midtrans_payment(order_id, final_nominal, nama_donatur, phone, kampanye_nama)
                logger.info(f"KONFIRMASI step4 OK: payment_url={payment_url[:50] if payment_url else 'None'}")

                transaksi = load_data()
                transaksi.append({"order_id": order_id, "donatur": nama_donatur, "atas_nama": atas_nama, "phone": phone,
                    "kampanye_id": kampanye_id, "kampanye": kampanye_nama, "nominal": final_nominal, "tipe": tipe,
                    "status": "pending", "payment_url": payment_url, "created_at": datetime.now().isoformat()})
                save_data(transaksi)
                logger.info("KONFIRMASI step5 OK: data saved")

                if phone:
                    send_wa_message(phone, f"Assalamu'alaikum *{nama_donatur}*! \U0001f932\n\nTerima kasih berniat berdonasi untuk:\n*{kampanye_nama}*\n\nNominal: *{format_rupiah(final_nominal)}*\n\nSelesaikan donasi di:\n{payment_url}\n\n_Link berlaku 24 jam. Semoga berkah._ \U0001f64f")
                    logger.info(f"KONFIRMASI step6 OK: WA sent to {phone}")

                notify_telegram(f"\U0001f514 <b>Donasi Baru!</b>\n\U0001f464 {nama_donatur} ({phone})\n\U0001f4cb {kampanye_nama}\n\U0001f4b0 {format_rupiah(final_nominal)}\n\U0001f194 {order_id}")
                nominal_display = f"Rp {final_nominal:,}".replace(",", ".")
                return {"screen": "SELESAI", "data": {
                    "payment_url": payment_url or "",
                    "order_id": order_id,
                    "nama_donatur": nama_donatur,
                    "kampanye_nama": kampanye_nama,
                    "nominal_display": nominal_display
                }}
            except Exception as e:
                logger.error(f"KONFIRMASI handler FATAL ERROR: {e}", exc_info=True)
                return {"screen": "SELESAI", "data": {
                    "payment_url": "",
                    "order_id": "ERROR-" + datetime.now().strftime("%H%M%S"),
                    "nama_donatur": "Donatur",
                    "kampanye_nama": "Kampanye Raihmimpi",
                    "nominal_display": "Rp 0"
                }}


    return {"screen": "PILIH_TIPE", "data": {}}

@app.route("/wa-flow", methods=["GET"])
def wa_flow_verify():
    """Verifikasi webhook Meta untuk App subscription."""
    mode = request.args.get("hub.mode")
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")
    logger.info(f"Webhook verify: mode={mode} token={token} challenge={challenge}")
    if mode == "subscribe" and token == WEBHOOK_VERIFY_TOKEN:
        logger.info("Webhook verified successfully")
        return challenge, 200
    logger.warning(f"Webhook verify FAILED: token mismatch atau mode salah")
    return jsonify({"error": "Forbidden"}), 403

@app.route("/wa-flow", methods=["POST"])
def wa_flow_endpoint():
    try:
        body = request.get_json()
        logger.info(f"RAW BODY KEYS: {list(body.keys()) if body else 'None'}")

        if body and "encrypted_aes_key" in body:
            decrypted_body, aes_key, iv = decrypt_request(body)
            response_data = handle_flow_request(decrypted_body)
            encrypted_response = encrypt_response(response_data, aes_key, iv)
            return Response(encrypted_response, mimetype="text/plain")

        if body and "entry" in body:
            try:
                for entry in body.get("entry", []):
                    for change in entry.get("changes", []):
                        value = change.get("value", {})
                        messages = value.get("messages", [])
                        contacts_meta = value.get("contacts", [])
                        for msg in messages:
                            phone = msg.get("from")
                            if not phone:
                                continue
                            msg_type = msg.get("type", "text")
                            button_reply_id = None
                            if msg_type == "text":
                                text = msg.get("text", {}).get("body", "")
                            elif msg_type == "interactive":
                                interactive = msg.get("interactive", {})
                                itype = interactive.get("type")
                                if itype == "button_reply":
                                    button_reply_id = interactive.get("button_reply", {}).get("id")
                                    text = interactive.get("button_reply", {}).get("title", "")
                                elif itype == "nfm_reply":
                                    # Flow submission — tampilkan persis seperti di WhatsApp
                                    text = "📄 Mulai Donasi · Jawaban terkirim"
                                else:
                                    text = json.dumps(interactive)[:500]
                            else:
                                text = f"[{msg_type.upper()}]"

                            contact_name = None
                            for c in contacts_meta:
                                if c.get("wa_id") == phone:
                                    contact_name = c.get("profile", {}).get("name")

                            logger.info(f"WA WEBHOOK MSG from={phone} type={msg_type} button_id={button_reply_id} text={text}")

                            inbox_before = load_inbox()
                            existing_contact = inbox_before.get("contacts", {}).get(phone)

                            record_incoming_message(phone, text, msg_type="text" if msg_type == "text" else msg_type, name=contact_name)

                            # Handle klik tombol Menu Utama
                            if button_reply_id == "btn_donasi":
                                try:
                                    send_wa_flow_message(phone)
                                    logger.info(f"Flow donasi dikirim ke {phone} (klik tombol Mulai Donasi)")
                                except Exception as fe:
                                    logger.error(f"Gagal kirim Flow ke {phone}: {fe}", exc_info=True)
                                continue
                            elif button_reply_id == "btn_admin":
                                try:
                                    send_wa_message(phone, "Admin akan segera membalas pesan Anda. Mohon tunggu sebentar 🙏")
                                    inbox = load_inbox()
                                    inbox["contacts"][phone]["status"] = "perlu_dibalas"
                                    save_inbox(inbox)
                                    logger.info(f"Notifikasi admin dikirim ke {phone}")
                                except Exception as ae:
                                    logger.error(f"Gagal proses btn_admin untuk {phone}: {ae}", exc_info=True)
                                continue

                            text_lower = (text or "").lower()
                            if msg_type == "text" and any(kw in text_lower for kw in DONASI_KEYWORDS):
                                try:
                                    send_wa_flow_message(phone)
                                    logger.info(f"Flow donasi dikirim ke {phone} (keyword match)")
                                except Exception as fe:
                                    logger.error(f"Gagal kirim Flow ke {phone}: {fe}", exc_info=True)
                                continue

                            # Kirim Menu Utama jika pesan masuk pertama & belum di-resolve
                            if msg_type == "text":
                                is_new_contact = existing_contact is None
                                already_resolved = existing_contact and existing_contact.get("status") == "selesai"
                                menu_already_sent = existing_contact and existing_contact.get("menu_sent")
                                if (is_new_contact or not already_resolved) and not menu_already_sent:
                                    try:
                                        send_menu_utama(phone)
                                        inbox = load_inbox()
                                        inbox["contacts"][phone]["menu_sent"] = True
                                        save_inbox(inbox)
                                        logger.info(f"Menu Utama dikirim ke {phone}")
                                    except Exception as me:
                                        logger.error(f"Gagal kirim Menu Utama ke {phone}: {me}", exc_info=True)
            except Exception as we:
                logger.error(f"Error parsing webhook entry: {we}", exc_info=True)
            return jsonify({"status": "ok"}), 200

        response_data = handle_flow_request(body)
        return jsonify(response_data)
    except Exception as e:
        logger.error(f"Error wa-flow: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500

@app.route("/midtrans-callback", methods=["POST"])
def midtrans_callback():
    try:
        data = request.get_json()
        order_id = data.get("order_id")
        status = data.get("transaction_status")
        fraud = data.get("fraud_status", "accept")
        final_status = "lunas" if status in ("capture", "settlement") and fraud == "accept" else ("gagal" if status in ("cancel", "deny", "expire") else status)
        transaksi = load_data()
        donatur, kampanye, kampanye_id, nominal, phone = "", "", "", 0, ""
        for t in transaksi:
            if t["order_id"] == order_id:
                t["status"] = final_status
                t["paid_at"] = datetime.now().isoformat()
                donatur, kampanye, kampanye_id, nominal, phone = t.get("donatur",""), t.get("kampanye",""), t.get("kampanye_id",""), t.get("nominal",0), t.get("phone","")
                break
        save_data(transaksi)
        if final_status == "lunas" and phone:
            send_wa_message(phone, f"✅ *Donasi Berhasil!*\n\nAlhamdulillah donasi Anda diterima.\n\n📋 *{kampanye}*\n💰 {format_rupiah(nominal)}\n🆔 {order_id}\n\nSemoga Allah melipatgandakan kebaikan Anda. 🤲\n_Raihmimpi.id_")
            notify_telegram(f"✅ <b>LUNAS!</b>\n👤 {donatur} ({phone})\n📋 {kampanye}\n💰 {format_rupiah(nominal)}\n🆔 {order_id}")
            send_pixel_event("Purchase", phone=phone, value=nominal, currency="IDR",
                              event_id=f"purchase_{order_id}", content_name=kampanye,
                              content_ids=[kampanye_id] if kampanye_id else None)
        return jsonify({"status": "ok"})
    except Exception as e:
        logger.error(f"Error midtrans-callback: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500

@app.route("/api/kampanye-source", methods=["GET"])
def list_kampanye():
    campaigns = get_campaigns(full=True)
    return jsonify({"total": len(campaigns), "formatted": format_campaigns_for_flow(campaigns)})

@app.route("/", methods=["GET"])
def health():
    return jsonify({"status": "ok", "service": "Raihmimpi WA Flow Backend", "version": "3.2.0"})

@app.route("/halosis-test-send", methods=["GET"])
def halosis_test_send():
    """Debug: kirim test Flow message via Halosis API ke nomor tertentu.
    Usage: /halosis-test-send?to=628112344635"""
    to = request.args.get("to", "")
    if not to:
        return jsonify({"error": "param 'to' wajib diisi, contoh: ?to=628112344635"}), 400
    try:
        resp = halosis_send_flow_message(to)
        return jsonify({
            "to": to,
            "flow_id_used": HALOSIS_DONASI_FLOW_ID,
            "halosis_status": resp.status_code,
            "halosis_body": resp.text[:1000]
        })
    except Exception as e:
        logger.error(f"halosis_test_send error: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500

@app.route("/halosis-flows", methods=["GET"])
def halosis_flows():
    """Debug: list survey flows yang terdaftar di Halosis (cari flow_id donasi)."""
    try:
        token = HALOSIS_LONG_TOKEN or halosis_get_access_token()
        resp = requests.get(f"{HALOSIS_API_BASE}/surveys",
            headers={"Authorization": f"Bearer {token}"}, timeout=15)
        return jsonify({"halosis_status": resp.status_code, "halosis_body": resp.json() if resp.ok else resp.text})
    except Exception as e:
        logger.error(f"halosis_flows error: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500

@app.route("/halosis-webhook", methods=["POST", "GET"])
def halosis_webhook():
    """
    Webhook untuk event 'Message Received' dari Halosis.
    Kalau pesan masuk mengandung keyword donasi, kirim WhatsApp Flow.

    Karena format payload Halosis belum diketahui pasti, fungsi ini
    mencoba beberapa kemungkinan struktur umum sambil mencatat raw body
    ke log untuk debugging.
    """
    if request.method == "GET":
        # Beberapa provider melakukan verifikasi webhook via GET
        return jsonify({"status": "ok"}), 200

    try:
        body = request.get_json(silent=True) or {}
        logger.info(f"HALOSIS WEBHOOK RAW BODY: {json.dumps(body)[:2000]}")

        phone = None
        text = None

        # Struktur asli Halosis: {"type": "message.received", "data": {"from_phone_number": ..., "message": ...}}
        if body.get("type") == "message.received":
            data = body.get("data", {})
            phone = data.get("from_phone_number")
            text = data.get("message")

        # Fallback untuk struktur lain (mirip Cloud API / variasi field)
        if not phone:
            try:
                entry = body.get("entry", [])
                if entry:
                    value = entry[0]["changes"][0]["value"]
                    messages = value.get("messages", [])
                    if messages:
                        phone = messages[0].get("from")
                        text = messages[0].get("text", {}).get("body")
            except Exception:
                pass

        if not phone:
            for key in ["from", "phone", "sender", "wa_id", "from_phone_number"]:
                if body.get(key):
                    phone = body.get(key)
                    break

        if not text:
            for key in ["message", "text", "body", "msg"]:
                val = body.get(key)
                if isinstance(val, str):
                    text = val
                    break

        if (not phone or not text) and isinstance(body.get("data"), dict):
            data = body["data"]
            phone = phone or data.get("from_phone_number") or data.get("from") or data.get("phone")
            text = text or data.get("message") or data.get("text")

        logger.info(f"HALOSIS WEBHOOK PARSED: phone={phone} text={text}")

        if not phone or not text:
            return jsonify({"status": "ignored", "reason": "phone/text not found", "raw": body}), 200

        # Ambil nama kontak jika tersedia di payload
        contact_name = None
        try:
            data = body.get("data", {})
            contact_name = data.get("name") or data.get("from_name") or data.get("contact_name")
        except Exception:
            pass

        # Simpan SEMUA pesan masuk ke inbox (untuk fitur Inbox/Chat)
        clean_phone_inbox = phone.replace("+", "").replace(" ", "").replace("-", "")
        record_incoming_message(clean_phone_inbox, text, msg_type="text", name=contact_name)

        text_lower = text.lower()
        if any(kw in text_lower for kw in DONASI_KEYWORDS):
            # Normalisasi nomor (hapus + dan spasi/strip)
            clean_phone = phone.replace("+", "").replace(" ", "").replace("-", "")
            try:
                resp = halosis_send_flow_message(clean_phone)
                return jsonify({"status": "flow_sent_via_halosis", "to": clean_phone, "halosis_status": resp.status_code, "halosis_body": resp.text[:300]}), 200
            except Exception as he:
                logger.error(f"halosis_send_flow_message failed: {he}", exc_info=True)
                # fallback ke Graph API langsung
                send_wa_flow_message(clean_phone)
                return jsonify({"status": "flow_sent_via_graph_fallback", "to": clean_phone}), 200

        return jsonify({"status": "ignored", "reason": "no keyword match"}), 200

    except Exception as e:
        logger.error(f"Error halosis-webhook: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500

@app.route("/api/inbox", methods=["GET"])
def api_inbox_list():
    """List semua kontak/percakapan, diurutkan dari pesan terbaru."""
    inbox = load_inbox()
    contacts = list(inbox.get("contacts", {}).values())
    contacts_sorted = sorted(contacts, key=lambda c: c.get("last_message_at", ""), reverse=True)
    return jsonify({"contacts": contacts_sorted})

@app.route("/api/inbox/<phone>", methods=["GET"])
def api_inbox_messages(phone):
    """Ambil semua pesan untuk satu kontak, dan reset unread counter."""
    inbox = load_inbox()
    messages = inbox.get("messages", {}).get(phone, [])
    contact = inbox.get("contacts", {}).get(phone, {"phone": phone, "name": phone, "labels": []})

    # Reset unread saat dibuka
    if inbox.get("contacts", {}).get(phone, {}).get("unread", 0) > 0:
        inbox["contacts"][phone]["unread"] = 0
        save_inbox(inbox)

    return jsonify({"contact": contact, "messages": messages})

@app.route("/api/inbox/<phone>/reply", methods=["POST"])
def api_inbox_reply(phone):
    """Kirim balasan teks ke kontak via Halosis API, dan simpan ke inbox."""
    body = request.get_json(silent=True) or {}
    text = body.get("text", "").strip()
    if not text:
        return jsonify({"error": "text wajib diisi"}), 400
    try:
        resp = requests.post(f"{HALOSIS_API_BASE}/messages",
            json={
                "from_phone_number": HALOSIS_FROM_PHONE,
                "to": phone,
                "type": "text",
                "message": text,
            },
            headers={"Authorization": f"Bearer {HALOSIS_LONG_TOKEN or halosis_get_access_token()}", "Content-Type": "application/json"},
            timeout=15)
        record_outgoing_message(phone, text, msg_type="text")
        return jsonify({"status": "sent", "halosis_status": resp.status_code, "halosis_body": resp.text[:300]})
    except Exception as e:
        logger.error(f"api_inbox_reply error: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500

@app.route("/api/inbox/<phone>/label", methods=["POST"])
def api_inbox_label(phone):
    """Update label dan/atau status kontak."""
    body = request.get_json(silent=True) or {}
    inbox = load_inbox()
    contact = inbox.get("contacts", {}).get(phone)
    if not contact:
        return jsonify({"error": "contact not found"}), 404
    if "labels" in body:
        contact["labels"] = body["labels"]
    if "status" in body:
        contact["status"] = body["status"]
    if "name" in body:
        contact["name"] = body["name"]
    inbox["contacts"][phone] = contact
    save_inbox(inbox)
    return jsonify(contact)

@app.route("/api/inbox/<phone>/reset-menu", methods=["POST"])
def api_inbox_reset_menu(phone):
    inbox = load_inbox()
    contact = inbox.get("contacts", {}).get(phone)
    if not contact:
        return jsonify({"error": "contact not found"}), 404
    contact["menu_sent"] = False
    save_inbox(inbox)
    logger.info(f"menu_sent direset untuk {phone}")
    return jsonify({"status": "ok", "phone": phone})

@app.route("/api/inbox/reset-menu-all", methods=["POST"])
def api_inbox_reset_menu_all():
    inbox = load_inbox()
    count = 0
    for phone, contact in inbox.get("contacts", {}).items():
        if contact.get("menu_sent"):
            contact["menu_sent"] = False
            count += 1
    save_inbox(inbox)
    logger.info(f"menu_sent direset untuk {count} kontak")
    return jsonify({"status": "ok", "reset_count": count})

@app.route("/api/settings/menu-utama", methods=["GET"])
def api_get_menu_utama():
    settings = load_settings()
    return jsonify(settings.get("menu_utama", DEFAULT_SETTINGS["menu_utama"]))

@app.route("/api/settings/menu-utama", methods=["POST"])
def api_save_menu_utama():
    body = request.get_json(silent=True) or {}
    settings = load_settings()
    settings["menu_utama"] = {
        "message": body.get("message", ""),
        "buttons": body.get("buttons", []),
    }
    save_settings(settings)
    return jsonify(settings["menu_utama"])

@app.route("/api/blast", methods=["POST"])
def api_blast():
    """Kirim pesan broadcast (WA Blast) ke beberapa kontak via Graph API.
    Body: {"phones": ["62..."], "message": "..."}"""
    body = request.get_json(silent=True) or {}
    phones = body.get("phones", [])
    message = (body.get("message") or "").strip()
    if not phones:
        return jsonify({"error": "phones wajib diisi"}), 400
    if not message:
        return jsonify({"error": "message wajib diisi"}), 400

    results = []
    for phone in phones:
        try:
            send_wa_message(phone, message)
            record_outgoing_message(phone, message, msg_type="text")
            results.append({"phone": phone, "status": "sent"})
        except Exception as e:
            logger.error(f"api_blast error for {phone}: {e}", exc_info=True)
            results.append({"phone": phone, "status": "error", "error": str(e)})

    sent_count = sum(1 for r in results if r["status"] == "sent")
    return jsonify({"total": len(phones), "sent": sent_count, "results": results})


@app.route("/api/donasi", methods=["GET"])
def api_donasi():
    """JSON data semua transaksi donasi, untuk konsumsi dashboard."""
    transaksi = load_data()
    transaksi_sorted = sorted(transaksi, key=lambda t: t.get("created_at", ""), reverse=True)

    total_nominal_lunas = sum(t.get("nominal", 0) for t in transaksi if t.get("status") == "lunas")
    total_donasi_lunas = sum(1 for t in transaksi if t.get("status") == "lunas")
    donatur_set = set(t.get("phone", "") for t in transaksi if t.get("status") == "lunas" and t.get("phone"))

    tipe_count = {}
    for t in transaksi:
        if t.get("status") == "lunas":
            tipe = t.get("tipe", "sekali")
            tipe_count[tipe] = tipe_count.get(tipe, 0) + 1

    kampanye_count = {}
    for t in transaksi:
        if t.get("status") == "lunas":
            k = t.get("kampanye", "Lainnya")
            kampanye_count[k] = kampanye_count.get(k, 0) + 1
    top_kampanye = sorted(kampanye_count.items(), key=lambda x: x[1], reverse=True)[:5]

    return jsonify({
        "summary": {
            "total_nominal": total_nominal_lunas,
            "total_donasi": total_donasi_lunas,
            "total_donatur": len(donatur_set),
        },
        "tipe_donasi": tipe_count,
        "top_kampanye": [{"nama": k, "jumlah": v} for k, v in top_kampanye],
        "transaksi": transaksi_sorted[:100],
    })

LAYOUT_CSS = """
  * { margin:0; padding:0; box-sizing:border-box; font-family:-apple-system,Segoe UI,Roboto,sans-serif; }
  body { background:#f3f4f8; color:#1f2330; display:flex; min-height:100vh; }
  .sidebar { width:220px; background:#5b3df0; color:#fff; padding:24px 0; flex-shrink:0; }
  .sidebar .logo { font-size:20px; font-weight:700; padding:0 24px 24px; }
  .sidebar a { display:flex; align-items:center; gap:10px; padding:12px 24px; color:#e0d9ff; text-decoration:none; font-size:14px; }
  .sidebar a.active { background:rgba(255,255,255,.15); color:#fff; font-weight:600; border-radius:0 20px 20px 0; }
  .sidebar a:hover { color:#fff; }
  .main { flex:1; padding:24px; min-width:0; }
  h1 { font-size:22px; margin-bottom:4px; }
  .subtitle { color:#6b7280; margin-bottom:20px; font-size:14px; }
  .cards { display:grid; grid-template-columns:repeat(auto-fit,minmax(220px,1fr)); gap:16px; margin-bottom:24px; }
  .card { background:#fff; border-radius:12px; padding:20px; box-shadow:0 1px 3px rgba(0,0,0,.08); }
  .card .icon { font-size:28px; }
  .card .value { font-size:28px; font-weight:700; color:#5b3df0; margin:8px 0 4px; }
  .card .label { color:#6b7280; font-size:14px; }
  .grid2 { display:grid; grid-template-columns:1fr 1fr; gap:16px; }
  @media (max-width: 800px) { .grid2 { grid-template-columns:1fr; } .sidebar { display:none; } }
  .panel { background:#fff; border-radius:12px; padding:20px; box-shadow:0 1px 3px rgba(0,0,0,.08); }
  .panel h2 { font-size:16px; margin-bottom:12px; }
  table { width:100%; border-collapse:collapse; font-size:13px; }
  th, td { text-align:left; padding:10px 8px; border-bottom:1px solid #eee; }
  th { color:#6b7280; font-weight:600; }
  tr:hover td { background:#f9fafb; }
  a.row-link { text-decoration:none; color:inherit; }
  .badge { padding:2px 8px; border-radius:6px; font-size:11px; font-weight:600; }
  .badge.lunas { background:#dcfce7; color:#16a34a; }
  .badge.pending { background:#fef3c7; color:#d97706; }
  .badge.gagal { background:#fee2e2; color:#dc2626; }
  .refresh { font-size:12px; color:#9ca3af; margin-top:16px; }
  .filters { display:flex; gap:12px; flex-wrap:wrap; margin-bottom:16px; }
  .filters label { font-size:12px; color:#6b7280; display:block; margin-bottom:4px; }
  .filters input, .filters select { padding:8px 10px; border:1px solid #ddd; border-radius:8px; font-size:13px; min-width:160px; }
  .btn { background:#5b3df0; color:#fff; border:none; padding:9px 20px; border-radius:8px; font-size:13px; font-weight:600; cursor:pointer; }
  .btn:hover { background:#4c30d9; }
  .back-link { display:inline-block; margin-bottom:16px; color:#5b3df0; text-decoration:none; font-size:14px; font-weight:600; }
  .detail-row { display:flex; justify-content:space-between; padding:12px 0; border-bottom:1px solid #eee; font-size:14px; }
  .detail-row .label { color:#6b7280; }
  .detail-row .val { font-weight:600; text-align:right; }

  /* Chat / Inbox */
  .chat-wrap { display:flex; height:calc(100vh - 48px); background:#fff; border-radius:12px; overflow:hidden; box-shadow:0 1px 3px rgba(0,0,0,.08); }
  .chat-list { width:340px; flex-shrink:0; border-right:1px solid #eee; display:flex; flex-direction:column; }
  .chat-tabs { display:flex; gap:8px; padding:12px; border-bottom:1px solid #eee; }
  .chat-tab { flex:1; text-align:center; padding:8px; border-radius:8px; font-size:13px; font-weight:600; cursor:pointer; background:#f3f4f8; color:#6b7280; }
  .chat-tab.active { background:#5b3df0; color:#fff; }
  .chat-items { flex:1; overflow-y:auto; }
  .chat-item { padding:14px 16px; border-bottom:1px solid #f3f4f6; cursor:pointer; display:flex; gap:10px; }
  .chat-item:hover { background:#f9fafb; }
  .chat-item.selected { background:#f0edff; }
  .chat-avatar { width:40px; height:40px; border-radius:50%; background:#e0d9ff; color:#5b3df0; display:flex; align-items:center; justify-content:center; font-weight:700; flex-shrink:0; font-size:15px; }
  .chat-item-body { flex:1; min-width:0; }
  .chat-item-top { display:flex; justify-content:space-between; align-items:baseline; gap:8px; }
  .chat-item-name { font-weight:600; font-size:14px; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
  .chat-item-time { font-size:11px; color:#9ca3af; flex-shrink:0; }
  .chat-item-preview { font-size:12px; color:#6b7280; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; margin-top:2px; }
  .chat-labels { display:flex; gap:4px; margin-top:6px; flex-wrap:wrap; }
  .chat-label { font-size:10px; font-weight:700; padding:2px 8px; border-radius:6px; background:#e0e7ff; color:#4338ca; text-transform:uppercase; }
  .chat-unread { background:#ef4444; color:#fff; font-size:11px; font-weight:700; border-radius:10px; min-width:20px; height:20px; display:flex; align-items:center; justify-content:center; padding:0 6px; }
  .chat-empty { flex:1; display:flex; align-items:center; justify-content:center; color:#9ca3af; font-size:14px; flex-direction:column; gap:12px; text-align:center; padding:40px; }
  .chat-panel { flex:1; display:flex; flex-direction:column; min-width:0; }
  .chat-header { padding:16px 20px; border-bottom:1px solid #eee; display:flex; align-items:center; gap:12px; }
  .chat-header .chat-avatar { width:36px; height:36px; font-size:13px; }
  .chat-header-name { font-weight:700; font-size:15px; }
  .chat-header-phone { font-size:12px; color:#9ca3af; }
  .chat-messages { flex:1; overflow-y:auto; padding:20px; display:flex; flex-direction:column; gap:10px; background:#f9fafb; }
  .chat-bubble { max-width:60%; padding:10px 14px; border-radius:12px; font-size:14px; line-height:1.4; }
  .chat-bubble.in { background:#fff; align-self:flex-start; box-shadow:0 1px 2px rgba(0,0,0,.06); }
  .chat-bubble.out { background:#5b3df0; color:#fff; align-self:flex-end; }
  .chat-bubble-time { font-size:10px; opacity:.6; margin-top:4px; text-align:right; }
  .chat-input-bar { display:flex; gap:10px; padding:14px 16px; border-top:1px solid #eee; }
  .chat-input-bar input { flex:1; padding:10px 14px; border:1px solid #ddd; border-radius:20px; font-size:14px; }
  .chat-input-bar button { background:#5b3df0; color:#fff; border:none; border-radius:20px; padding:10px 22px; font-weight:600; font-size:14px; cursor:pointer; }
  .chat-input-bar button:hover { background:#4c30d9; }

  /* Pengaturan */
  .settings-tabs { display:flex; gap:10px; margin-bottom:20px; }
  .settings-tab { padding:10px 20px; border-radius:10px; font-size:13px; font-weight:600; cursor:pointer; background:#fff; color:#6b7280; box-shadow:0 1px 3px rgba(0,0,0,.06); }
  .settings-tab.active { background:#5b3df0; color:#fff; }
  .settings-section { display:none; }
  .settings-section.active { display:block; }
  .form-group { margin-bottom:16px; }
  .form-group label { display:block; font-size:13px; font-weight:600; margin-bottom:6px; color:#374151; }
  .form-group textarea, .form-group input[type=text] { width:100%; padding:10px 12px; border:1px solid #ddd; border-radius:8px; font-size:14px; font-family:inherit; }
  .form-group textarea { min-height:100px; resize:vertical; }
  .form-hint { font-size:11px; color:#9ca3af; margin-top:4px; }
  .button-row { display:flex; gap:12px; align-items:center; padding:10px 0; border-bottom:1px solid #f3f4f6; }
  .button-row:last-child { border-bottom:none; }
  .button-row input[type=text] { flex:1; padding:8px 10px; border:1px solid #ddd; border-radius:8px; font-size:13px; }
  .button-row select { padding:8px 10px; border:1px solid #ddd; border-radius:8px; font-size:13px; }
  .save-msg { color:#16a34a; font-size:13px; margin-left:12px; }
  .blast-contacts { max-height:260px; overflow-y:auto; border:1px solid #eee; border-radius:8px; padding:8px; margin-bottom:12px; }
  .blast-contact-item { display:flex; align-items:center; gap:10px; padding:8px; border-radius:6px; }
  .blast-contact-item:hover { background:#f9fafb; }
  .blast-count { font-size:12px; color:#6b7280; margin-bottom:8px; }
"""

def render_sidebar(active):
    items = [
        ("dashboard", "/dashboard", "📊", "Dashboard"),
        ("pesanan", "/pesanan", "📋", "Pesanan"),
        ("chat", "/chat", "💬", "Chat"),
        ("kampanye", "/kampanye", "🎯", "Kampanye"),
        ("whatsapp", "/whatsapp", "📱", "WhatsApp"),
    ]
    links = "".join(
        f'<a href="{url}" class="{"active" if key == active else ""}"><span>{icon}</span><span>{label}</span></a>'
        for key, url, icon, label in items
    )
    return f"""<div class="sidebar">
  <div class="logo">🤲 Raihmimpi</div>
  {links}
</div>"""

def render_page(active, title, subtitle, body_html, extra_head=""):
    return f"""<!DOCTYPE html>
<html lang="id">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{title} - Raihmimpi</title>
{extra_head}
<style>{LAYOUT_CSS}</style>
</head>
<body>
{render_sidebar(active)}
<div class="main">
  <h1>{title}</h1>
  <div class="subtitle">{subtitle}</div>
  {body_html}
</div>
</body>
</html>"""

@app.route("/dashboard", methods=["GET"])
def dashboard():
    """Dashboard donasi sederhana (mirip Halosis dashboard)."""
    body = """
  <div class="cards">
    <div class="card"><div class="icon">&#128176;</div><div class="value" id="total_nominal">-</div><div class="label">Total Nominal Donasi (Lunas)</div></div>
    <div class="card"><div class="icon">&#128203;</div><div class="value" id="total_donasi">-</div><div class="label">Total Donasi (Lunas)</div></div>
    <div class="card"><div class="icon">&#128101;</div><div class="value" id="total_donatur">-</div><div class="label">Total Donatur</div></div>
  </div>

  <div class="grid2">
    <div class="panel">
      <h2>Tipe Donasi</h2>
      <canvas id="chartTipe" height="220"></canvas>
    </div>
    <div class="panel">
      <h2>Kampanye Terbanyak</h2>
      <div id="topKampanye"></div>
    </div>
  </div>

  <div class="panel" style="margin-top:16px;">
    <h2>Daftar Donasi Terbaru</h2>
    <table>
      <thead><tr><th>Waktu</th><th>Donatur</th><th>Kampanye</th><th>Nominal</th><th>Tipe</th><th>Status</th></tr></thead>
      <tbody id="tabelDonasi"></tbody>
    </table>
  </div>
  <div class="refresh">Auto-refresh setiap 30 detik &middot; <span id="lastUpdate"></span></div>

<script>
function formatRupiah(n) {
  return "Rp " + Number(n).toLocaleString("id-ID");
}
function formatWaktu(iso) {
  if (!iso) return "-";
  const d = new Date(iso);
  return d.toLocaleString("id-ID", {dateStyle:"medium", timeStyle:"short"});
}
let chart;
async function loadData() {
  const res = await fetch("/api/donasi");
  const json = await res.json();

  document.getElementById("total_nominal").textContent = formatRupiah(json.summary.total_nominal);
  document.getElementById("total_donasi").textContent = json.summary.total_donasi;
  document.getElementById("total_donatur").textContent = json.summary.total_donatur;

  const tipeLabels = Object.keys(json.tipe_donasi).map(t => t === "sekali" ? "Donasi Sekali" : "Donasi Rutin");
  const tipeData = Object.values(json.tipe_donasi);
  if (chart) chart.destroy();
  chart = new Chart(document.getElementById("chartTipe"), {
    type: "pie",
    data: { labels: tipeLabels.length ? tipeLabels : ["Belum ada data"], datasets: [{ data: tipeData.length ? tipeData : [1], backgroundColor: ["#f97316","#5b3df0","#10b981","#f43f5e"] }] },
    options: { plugins: { legend: { position: "bottom" } } }
  });

  const topKampanyeEl = document.getElementById("topKampanye");
  topKampanyeEl.innerHTML = json.top_kampanye.length
    ? json.top_kampanye.map(k => `<div style="display:flex;justify-content:space-between;padding:8px 0;border-bottom:1px solid #eee;"><span>${k.nama}</span><strong>${k.jumlah} donasi</strong></div>`).join("")
    : "<div style='color:#9ca3af'>Belum ada data</div>";

  const tbody = document.getElementById("tabelDonasi");
  tbody.innerHTML = json.transaksi.map(t => `
    <tr class="row-link" onclick="window.location='/pesanan/${t.order_id}'" style="cursor:pointer">
      <td>${formatWaktu(t.created_at)}</td>
      <td>${t.donatur || "-"}</td>
      <td>${t.kampanye || "-"}</td>
      <td>${formatRupiah(t.nominal || 0)}</td>
      <td>${t.tipe === "rutin" ? "Rutin" : "Sekali"}</td>
      <td><span class="badge ${t.status}">${t.status}</span></td>
    </tr>
  `).join("") || "<tr><td colspan='6' style='text-align:center;color:#9ca3af'>Belum ada donasi</td></tr>";

  document.getElementById("lastUpdate").textContent = "Update: " + new Date().toLocaleTimeString("id-ID");
}
loadData();
setInterval(loadData, 30000);
</script>
"""
    return Response(render_page("dashboard", "Dashboard Donasi Raihmimpi", "via WhatsApp Flow &middot; +62 851-1123-4962", body,
        extra_head='<script src="https://cdn.jsdelivr.net/npm/chart.js"></script>'), mimetype="text/html")


@app.route("/pesanan", methods=["GET"])
def pesanan():
    """Daftar pesanan donasi dengan filter (mirip Halosis Daftar Donasi)."""
    body = """
  <div class="panel">
    <div class="filters">
      <div>
        <label>No. Transaksi</label>
        <input type="text" id="f_order" placeholder="RM-...">
      </div>
      <div>
        <label>Nama Donatur</label>
        <input type="text" id="f_donatur" placeholder="Nama donatur">
      </div>
      <div>
        <label>Nomor HP</label>
        <input type="text" id="f_phone" placeholder="62...">
      </div>
      <div>
        <label>Status Donasi</label>
        <select id="f_status">
          <option value="">(Semua)</option>
          <option value="pending">Pending</option>
          <option value="lunas">Lunas</option>
          <option value="gagal">Gagal</option>
        </select>
      </div>
      <div style="align-self:flex-end;">
        <button class="btn" onclick="loadData()">Cari</button>
      </div>
    </div>
    <table>
      <thead><tr>
        <th>No. Transaksi</th><th>Tanggal</th><th>Nama Donatur</th><th>Nomor HP</th>
        <th>Nama Donasi</th><th>Tipe</th><th>Total</th><th>Status</th>
      </tr></thead>
      <tbody id="tabelPesanan"></tbody>
    </table>
  </div>
  <div class="refresh">Auto-refresh setiap 30 detik &middot; <span id="lastUpdate"></span></div>

<script>
function formatRupiah(n) {
  return "Rp " + Number(n).toLocaleString("id-ID");
}
function formatWaktu(iso) {
  if (!iso) return "-";
  const d = new Date(iso);
  return d.toLocaleString("id-ID", {dateStyle:"medium", timeStyle:"short"});
}
let allData = [];
async function loadData() {
  const res = await fetch("/api/donasi");
  const json = await res.json();
  allData = json.transaksi;
  render();
  document.getElementById("lastUpdate").textContent = "Update: " + new Date().toLocaleTimeString("id-ID");
}
function render() {
  const fOrder = document.getElementById("f_order").value.toLowerCase();
  const fDonatur = document.getElementById("f_donatur").value.toLowerCase();
  const fPhone = document.getElementById("f_phone").value.toLowerCase();
  const fStatus = document.getElementById("f_status").value;

  const filtered = allData.filter(t => {
    if (fOrder && !(t.order_id || "").toLowerCase().includes(fOrder)) return false;
    if (fDonatur && !(t.donatur || "").toLowerCase().includes(fDonatur)) return false;
    if (fPhone && !(t.phone || "").toLowerCase().includes(fPhone)) return false;
    if (fStatus && t.status !== fStatus) return false;
    return true;
  });

  const tbody = document.getElementById("tabelPesanan");
  tbody.innerHTML = filtered.map(t => `
    <tr class="row-link" onclick="window.location='/pesanan/${t.order_id}'" style="cursor:pointer">
      <td>${t.order_id}</td>
      <td>${formatWaktu(t.created_at)}</td>
      <td>${t.donatur || "-"}</td>
      <td>${t.phone || "-"}</td>
      <td>${t.kampanye || "-"}</td>
      <td>${t.tipe === "rutin" ? "Rutin" : "Sekali"}</td>
      <td>${formatRupiah(t.nominal || 0)}</td>
      <td><span class="badge ${t.status}">${t.status}</span></td>
    </tr>
  `).join("") || "<tr><td colspan='8' style='text-align:center;color:#9ca3af'>Tidak ada pesanan ditemukan</td></tr>";
}
['f_order','f_donatur','f_phone'].forEach(id => document.getElementById(id).addEventListener("keyup", render));
document.getElementById("f_status").addEventListener("change", render);
loadData();
setInterval(loadData, 30000);
</script>
"""
    return Response(render_page("pesanan", "Daftar Pesanan Donasi", "Semua transaksi donasi via WhatsApp Flow", body), mimetype="text/html")


@app.route("/pesanan/<order_id>", methods=["GET"])
def pesanan_detail(order_id):
    """Detail satu pesanan donasi."""
    transaksi = load_data()
    t = next((x for x in transaksi if x.get("order_id") == order_id), None)

    if not t:
        body = '<div class="panel"><p>Pesanan tidak ditemukan.</p><a href="/pesanan" class="back-link">&larr; Kembali ke Daftar Pesanan</a></div>'
        return Response(render_page("pesanan", "Detail Pesanan", order_id, body), mimetype="text/html")

    def fmt_waktu(iso):
        if not iso:
            return "-"
        try:
            dt = datetime.fromisoformat(iso)
            return dt.strftime("%d %b %Y, %H:%M")
        except Exception:
            return iso

    status_badge = f'<span class="badge {t.get("status","pending")}">{t.get("status","pending")}</span>'

    rows = [
        ("No. Transaksi", t.get("order_id", "-")),
        ("Tanggal Dibuat", fmt_waktu(t.get("created_at"))),
        ("Tanggal Lunas", fmt_waktu(t.get("paid_at")) if t.get("paid_at") else "-"),
        ("Status", status_badge),
        ("Nama Donatur", t.get("donatur", "-")),
        ("Atas Nama", t.get("atas_nama", "-")),
        ("Nomor HP", t.get("phone", "-")),
        ("Kampanye", t.get("kampanye", "-")),
        ("ID Kampanye", t.get("kampanye_id", "-")),
        ("Tipe Donasi", "Donasi Rutin" if t.get("tipe") == "rutin" else "Donasi Sekali"),
        ("Nominal", "Rp " + "{:,}".format(t.get("nominal", 0)).replace(",", ".")),
    ]
    if t.get("payment_url"):
        rows.append(("Link Pembayaran", f'<a href="{t["payment_url"]}" target="_blank" style="color:#5b3df0;">{t["payment_url"][:50]}...</a>'))

    rows_html = "".join(
        f'<div class="detail-row"><span class="label">{label}</span><span class="val">{value}</span></div>'
        for label, value in rows
    )

    body = f"""
  <a href="/pesanan" class="back-link">&larr; Kembali ke Daftar Pesanan</a>
  <div class="panel">
    <h2>Informasi Donasi</h2>
    {rows_html}
  </div>
"""
    return Response(render_page("pesanan", "Detail Pesanan", t.get("order_id", order_id), body), mimetype="text/html")


@app.route("/chat", methods=["GET"])
def chat_page():
    """Halaman Inbox/Chat - list kontak (kiri) + panel percakapan (kanan), mirip Halosis."""
    body = """
  <div class="chat-wrap">
    <div class="chat-list">
      <div class="chat-tabs">
        <div class="chat-tab active" data-status="perlu_dibalas" onclick="setTab(this)">Perlu Dibalas</div>
        <div class="chat-tab" data-status="otomatis" onclick="setTab(this)">Otomatis</div>
        <div class="chat-tab" data-status="selesai" onclick="setTab(this)">Selesai</div>
      </div>
      <div class="chat-items" id="chatItems"></div>
    </div>
    <div class="chat-panel" id="chatPanel">
      <div class="chat-empty">
        <div style="font-size:40px;">💬</div>
        <div>Pilih kontak di sebelah kiri<br>untuk memulai percakapan</div>
      </div>
    </div>
  </div>

<script>
let currentTab = "perlu_dibalas";
let currentPhone = null;
let allContacts = [];

function initials(name) {
  if (!name) return "?";
  const parts = name.trim().split(" ");
  return (parts[0][0] + (parts[1] ? parts[1][0] : "")).toUpperCase();
}
function formatTime(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  const now = new Date();
  if (d.toDateString() === now.toDateString()) {
    return d.toLocaleTimeString("id-ID", {hour:"2-digit", minute:"2-digit"});
  }
  return d.toLocaleDateString("id-ID", {day:"numeric", month:"short"});
}

function setTab(el) {
  document.querySelectorAll(".chat-tab").forEach(t => t.classList.remove("active"));
  el.classList.add("active");
  currentTab = el.dataset.status;
  renderItems();
}

async function loadContacts() {
  const res = await fetch("/api/inbox");
  const json = await res.json();
  allContacts = json.contacts || [];
  renderItems();
}

function renderItems() {
  const filtered = allContacts.filter(c => (c.status || "perlu_dibalas") === currentTab);
  const el = document.getElementById("chatItems");
  if (!filtered.length) {
    el.innerHTML = `<div class="chat-empty">Tidak ada percakapan</div>`;
    return;
  }
  el.innerHTML = filtered.map(c => `
    <div class="chat-item ${c.phone === currentPhone ? 'selected' : ''}" onclick="openChat('${c.phone}')">
      <div class="chat-avatar">${initials(c.name)}</div>
      <div class="chat-item-body">
        <div class="chat-item-top">
          <div class="chat-item-name">${c.name || c.phone}</div>
          <div class="chat-item-time">${formatTime(c.last_message_at)}</div>
        </div>
        <div class="chat-item-preview">${(c.last_message || "").replace(/</g,"&lt;")}</div>
        ${(c.labels && c.labels.length) ? `<div class="chat-labels">${c.labels.map(l => `<span class="chat-label">${l}</span>`).join("")}</div>` : ""}
      </div>
      ${c.unread ? `<div class="chat-unread">${c.unread}</div>` : ""}
    </div>
  `).join("");
}

async function openChat(phone) {
  currentPhone = phone;
  renderItems();
  const res = await fetch(`/api/inbox/${phone}`);
  const json = await res.json();
  const contact = json.contact;
  const messages = json.messages || [];

  const panel = document.getElementById("chatPanel");
  panel.innerHTML = `
    <div class="chat-header" style="position:relative;">
      <div class="chat-avatar">${initials(contact.name)}</div>
      <div style="flex:1;">
        <div class="chat-header-name">${contact.name || contact.phone}</div>
        <div class="chat-header-phone">+${contact.phone}</div>
      </div>
      <div style="position:relative;">
        <button onclick="toggleContactMenu(event)" id="contactMenuBtn" style="background:transparent;border:1px solid #e5e7eb;color:#4b5563;width:36px;height:36px;border-radius:50%;cursor:pointer;font-size:20px;line-height:1;display:flex;align-items:center;justify-content:center;" title="Aksi kontak">⋮</button>
        <div id="contactMenuDropdown" style="display:none;position:absolute;right:0;top:42px;background:#fff;border:1px solid #e5e7eb;border-radius:8px;box-shadow:0 4px 12px rgba(0,0,0,0.08);min-width:180px;z-index:100;overflow:hidden;">
          <div onclick="resetMenuContact('${contact.phone}')" style="padding:10px 14px;cursor:pointer;font-size:14px;display:flex;align-items:center;gap:8px;" onmouseover="this.style.background='#f3f4f6'" onmouseout="this.style.background='#fff'">↻ Reset Menu</div>
          <div onclick="markResolved('${contact.phone}')" style="padding:10px 14px;cursor:pointer;font-size:14px;display:flex;align-items:center;gap:8px;" onmouseover="this.style.background='#f3f4f6'" onmouseout="this.style.background='#fff'">✓ Selesai</div>
          <div onclick="editContactLabel('${contact.phone}')" style="padding:10px 14px;cursor:pointer;font-size:14px;display:flex;align-items:center;gap:8px;" onmouseover="this.style.background='#f3f4f6'" onmouseout="this.style.background='#fff'">🏷 Contact Label</div>
          <div onclick="blokirContact('${contact.phone}')" style="padding:10px 14px;cursor:pointer;font-size:14px;color:#dc2626;display:flex;align-items:center;gap:8px;border-top:1px solid #f3f4f6;" onmouseover="this.style.background='#fef2f2'" onmouseout="this.style.background='#fff'">⊘ Blokir</div>
        </div>
      </div>
    </div>
    <div class="chat-messages" id="chatMessages"></div>
    <div class="chat-input-bar">
      <input type="text" id="chatInput" placeholder="Tulis balasan..." onkeydown="if(event.key==='Enter') sendReply()">
      <button onclick="sendReply()">Kirim</button>
    </div>
  `;

  const msgEl = document.getElementById("chatMessages");
  msgEl.innerHTML = messages.map(m => `
    <div class="chat-bubble ${m.direction}">
      <div>${(m.text || "").replace(/</g,"&lt;")}</div>
      <div class="chat-bubble-time">${formatTime(m.timestamp)}</div>
    </div>
  `).join("") || `<div class="chat-empty">Belum ada pesan</div>`;
  msgEl.scrollTop = msgEl.scrollHeight;

  // refresh list (unread sudah ke-reset di server)
  loadContacts();
}

function toggleContactMenu(e) {
  e.stopPropagation();
  const dd = document.getElementById("contactMenuDropdown");
  dd.style.display = dd.style.display === "block" ? "none" : "block";
}

document.addEventListener("click", function(e) {
  const dd = document.getElementById("contactMenuDropdown");
  if (dd && !e.target.closest("#contactMenuBtn") && !e.target.closest("#contactMenuDropdown")) {
    dd.style.display = "none";
  }
});

async function resetMenuContact(phone) {
  document.getElementById("contactMenuDropdown").style.display = "none";
  if (!confirm("Reset Menu Otomatis untuk kontak ini? Menu Utama akan dikirim ulang saat pesan masuk berikutnya.")) return;
  try {
    const res = await fetch(`/api/inbox/${phone}/reset-menu`, {method:"POST"});
    const json = await res.json();
    if (json.status === "ok") {
      alert("✓ Menu Otomatis sudah direset untuk kontak ini.");
    } else {
      alert("Gagal: " + (json.error || "unknown"));
    }
  } catch (e) {
    alert("Error: " + e.message);
  }
}

async function markResolved(phone) {
  document.getElementById("contactMenuDropdown").style.display = "none";
  if (!confirm("Tandai percakapan ini sebagai Selesai?")) return;
  try {
    await fetch(`/api/inbox/${phone}/label`, {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({status: "selesai"})
    });
    alert("✓ Percakapan ditandai Selesai.");
    loadContacts();
    currentPhone = null;
    document.getElementById("chatPanel").innerHTML = `<div class="chat-empty"><div style="font-size:40px;">💬</div><div>Pilih kontak di sebelah kiri<br>untuk memulai percakapan</div></div>`;
  } catch (e) {
    alert("Error: " + e.message);
  }
}

async function editContactLabel(phone) {
  document.getElementById("contactMenuDropdown").style.display = "none";
  const current = (allContacts.find(c => c.phone === phone) || {}).labels || [];
  const input = prompt("Label kontak (pisahkan dengan koma, contoh: VIP, donatur-rutin):", current.join(", "));
  if (input === null) return;
  const labels = input.split(",").map(s => s.trim()).filter(Boolean);
  try {
    await fetch(`/api/inbox/${phone}/label`, {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({labels})
    });
    alert("✓ Label disimpan.");
    loadContacts();
  } catch (e) {
    alert("Error: " + e.message);
  }
}

async function blokirContact(phone) {
  document.getElementById("contactMenuDropdown").style.display = "none";
  if (!confirm("Blokir kontak ini? Pesan dari kontak ini akan diabaikan oleh sistem.")) return;
  try {
    await fetch(`/api/inbox/${phone}/label`, {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({status: "blokir"})
    });
    alert("✓ Kontak diblokir.");
    loadContacts();
    currentPhone = null;
    document.getElementById("chatPanel").innerHTML = `<div class="chat-empty"><div style="font-size:40px;">💬</div><div>Pilih kontak di sebelah kiri<br>untuk memulai percakapan</div></div>`;
  } catch (e) {
    alert("Error: " + e.message);
  }
}

async function sendReply() {
  const input = document.getElementById("chatInput");
  const text = input.value.trim();
  if (!text || !currentPhone) return;
  input.value = "";
  input.disabled = true;
  try {
    await fetch(`/api/inbox/${currentPhone}/reply`, {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({text})
    });
    openChat(currentPhone);
  } finally {
    input.disabled = false;
    input.focus();
  }
}

loadContacts();
setInterval(() => {
  loadContacts();
  if (currentPhone) openChat(currentPhone);
}, 15000);
</script>
"""
    return Response(render_page("chat", "Chat", "Inbox percakapan WhatsApp Raihmimpi", body), mimetype="text/html")


@app.route("/whatsapp", methods=["GET"])
def whatsapp_page():
    """Halaman Pengaturan: Menu Utama (auto-reply) dan WA Blast - sistem Raihmimpi sendiri."""
    body = """
  <div class="settings-tabs">
    <div class="settings-tab active" data-tab="menu-utama" onclick="setSettingsTab(this)">Menu Utama</div>
    <div class="settings-tab" data-tab="wa-blast" onclick="setSettingsTab(this)">WA Blast</div>
  </div>

  <div class="settings-section active" id="section-menu-utama">
    <div class="panel">
      <h2>Menu Utama</h2>
      <p class="form-hint" style="margin-bottom:16px;">Pesan sapaan otomatis beserta tombol aksi yang dikirim ke kontak baru.</p>

      <div class="form-group">
        <label>Isi Pesan</label>
        <textarea id="menuMessage" maxlength="1024" placeholder="Halo! kak, ..."></textarea>
        <div class="form-hint"><span id="menuMessageCount">0</span>/1024</div>
      </div>

      <div class="form-group">
        <label>Tombol</label>
        <div id="menuButtons"></div>
      </div>

      <button class="btn" onclick="saveMenuUtama()">Simpan</button>
      <span class="save-msg" id="menuSaveMsg"></span>
    </div>
  </div>

  <div class="settings-section" id="section-wa-blast">
    <div class="panel">
      <h2>WA Blast</h2>
      <p class="form-hint" style="margin-bottom:16px;">Kirim pesan broadcast ke kontak yang sudah pernah chat dengan Raihmimpi.</p>

      <div class="form-group">
        <label>Pilih Kontak</label>
        <div class="blast-count" id="blastCount">0 kontak dipilih</div>
        <div class="blast-contacts" id="blastContacts">Memuat kontak...</div>
        <button class="btn secondary" type="button" onclick="toggleAllBlast()">Pilih/Batal Semua</button>
      </div>

      <div class="form-group">
        <label>Isi Pesan</label>
        <textarea id="blastMessage" placeholder="Tulis pesan broadcast..."></textarea>
      </div>

      <button class="btn" onclick="sendBlast()">Kirim Blast</button>
      <span class="save-msg" id="blastSendMsg"></span>
    </div>
  </div>

<script>
function setSettingsTab(el) {
  document.querySelectorAll(".settings-tab").forEach(t => t.classList.remove("active"));
  document.querySelectorAll(".settings-section").forEach(s => s.classList.remove("active"));
  el.classList.add("active");
  document.getElementById("section-" + el.dataset.tab).classList.add("active");
  if (el.dataset.tab === "wa-blast") loadBlastContacts();
}

// ---- Menu Utama ----
const ACTION_OPTIONS = [
  {value: "donasi", label: "Mulai Donasi"},
  {value: "admin", label: "Hubungi Admin"},
  {value: "kampanye", label: "Pilih Kampanye"},
];

function renderButtonRow(btn, idx) {
  const optsHtml = ACTION_OPTIONS.map(o => `<option value="${o.value}" ${o.value === btn.action ? "selected" : ""}>${o.label}</option>`).join("");
  return `
    <div class="button-row">
      <input type="checkbox" ${btn.enabled ? "checked" : ""} onchange="updateButton(${idx}, 'enabled', this.checked)">
      <input type="text" value="${(btn.label||"").replace(/"/g,'&quot;')}" placeholder="Label tombol" maxlength="20" oninput="updateButton(${idx}, 'label', this.value)">
      <select onchange="updateButton(${idx}, 'action', this.value)">${optsHtml}</select>
    </div>
  `;
}

let menuButtons = [];
function renderMenuButtons() {
  document.getElementById("menuButtons").innerHTML = menuButtons.map((b, i) => renderButtonRow(b, i)).join("");
}
function updateButton(idx, key, value) {
  menuButtons[idx][key] = value;
}

async function loadMenuUtama() {
  const res = await fetch("/api/settings/menu-utama");
  const data = await res.json();
  document.getElementById("menuMessage").value = data.message || "";
  document.getElementById("menuMessageCount").textContent = (data.message || "").length;
  menuButtons = data.buttons || [];
  renderMenuButtons();
}
document.getElementById("menuMessage").addEventListener("input", function() {
  document.getElementById("menuMessageCount").textContent = this.value.length;
});

async function saveMenuUtama() {
  const message = document.getElementById("menuMessage").value;
  await fetch("/api/settings/menu-utama", {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({message, buttons: menuButtons})
  });
  const msg = document.getElementById("menuSaveMsg");
  msg.textContent = "Tersimpan!";
  setTimeout(() => msg.textContent = "", 2000);
}

// ---- WA Blast ----
let blastContactsData = [];
async function loadBlastContacts() {
  const res = await fetch("/api/inbox");
  const json = await res.json();
  blastContactsData = json.contacts || [];
  const el = document.getElementById("blastContacts");
  if (!blastContactsData.length) {
    el.innerHTML = "<div style='color:#9ca3af;padding:8px;'>Belum ada kontak</div>";
    return;
  }
  el.innerHTML = blastContactsData.map(c => `
    <label class="blast-contact-item">
      <input type="checkbox" value="${c.phone}" onchange="updateBlastCount()">
      <span>${c.name || c.phone} <span style="color:#9ca3af">(${c.phone})</span></span>
    </label>
  `).join("");
  updateBlastCount();
}
function updateBlastCount() {
  const checked = document.querySelectorAll("#blastContacts input:checked").length;
  document.getElementById("blastCount").textContent = checked + " kontak dipilih";
}
function toggleAllBlast() {
  const boxes = document.querySelectorAll("#blastContacts input");
  const allChecked = Array.from(boxes).every(b => b.checked);
  boxes.forEach(b => b.checked = !allChecked);
  updateBlastCount();
}
async function sendBlast() {
  const phones = Array.from(document.querySelectorAll("#blastContacts input:checked")).map(b => b.value);
  const message = document.getElementById("blastMessage").value.trim();
  const msgEl = document.getElementById("blastSendMsg");
  if (!phones.length) { msgEl.textContent = "Pilih minimal 1 kontak"; msgEl.style.color = "#dc2626"; return; }
  if (!message) { msgEl.textContent = "Isi pesan dulu"; msgEl.style.color = "#dc2626"; return; }
  msgEl.textContent = "Mengirim...";
  msgEl.style.color = "#6b7280";
  const res = await fetch("/api/blast", {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({phones, message})
  });
  const json = await res.json();
  msgEl.style.color = "#16a34a";
  msgEl.textContent = `Terkirim ke ${json.sent}/${json.total} kontak`;
}

loadMenuUtama();
</script>
"""
    return Response(render_page("whatsapp", "WhatsApp", "Menu Utama dan WhatsApp Blast - sistem Raihmimpi", body), mimetype="text/html")


@app.route("/api/kampanye-list", methods=["GET"])
def api_kampanye_list():
    """Return semua kampanye dari API Raihmimpi + flag aktif."""
    campaigns = get_campaigns(full=True)
    settings = load_settings()
    aktif_ids = [str(x) for x in settings.get("kampanye_aktif", [])]
    result = []
    for c in campaigns:
        cid = str(c.get("ID_CAMPAIGN", ""))
        result.append({
            "id": cid,
            "name": c.get("CAMPAIGN_NAME", ""),
            "image": c.get("IMG_MOBILE") or c.get("IMG_CAMPAIGNER") or c.get("IMG_BIG", ""),
            "total_donasi": c.get("TOTAL_DONASI", 0),
            "target": c.get("TARGET_DONASI_UANG", 0),
            "aktif": (cid in aktif_ids) if aktif_ids else False,
        })
    return jsonify({"campaigns": result, "filter_active": bool(aktif_ids)})

@app.route("/api/settings/kampanye-aktif", methods=["GET", "POST"])
def api_settings_kampanye_aktif():
    settings = load_settings()
    if request.method == "GET":
        return jsonify({"kampanye_aktif": settings.get("kampanye_aktif", [])})
    body = request.get_json(silent=True) or {}
    ids = body.get("kampanye_aktif", [])
    if not isinstance(ids, list):
        return jsonify({"error": "kampanye_aktif harus list"}), 400
    settings["kampanye_aktif"] = [str(x) for x in ids]
    save_settings(settings)
    logger.info(f"Kampanye aktif disimpan: {len(ids)} kampanye")
    return jsonify({"status": "ok", "kampanye_aktif": settings["kampanye_aktif"]})

@app.route("/kampanye", methods=["GET"])
def kampanye_page():
    """Halaman kelola kampanye yang ditampilkan di Flow donasi."""
    body = """
  <div class="panel">
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:16px;flex-wrap:wrap;gap:12px;">
      <div>
        <h3 style="margin:0 0 4px;">Pilih Kampanye Aktif</h3>
        <p style="color:#6b7280;margin:0;font-size:14px;">Kampanye yang dicentang akan muncul di Flow donasi WhatsApp (max 5 yang ditampilkan dengan gambar).</p>
      </div>
      <div style="display:flex;gap:8px;">
        <button class="btn" onclick="clearAll()" style="background:#f3f4f6;color:#374151;">Kosongkan</button>
        <button class="btn" onclick="saveSelection()" id="btnSimpan">Simpan</button>
      </div>
    </div>
    <div id="filterStatus" style="margin-bottom:16px;padding:10px 14px;background:#fef3c7;border:1px solid #f59e0b;border-radius:8px;color:#92400e;font-size:14px;display:none;"></div>

    <h4 style="margin:20px 0 10px;color:#111827;font-size:15px;">Kampanye Aktif <span id="aktifCount" style="color:#6b7280;font-weight:normal;font-size:13px;"></span></h4>
    <div id="kampanyeAktifGrid" style="display:grid;grid-template-columns:repeat(auto-fill,minmax(280px,1fr));gap:16px;margin-bottom:24px;">
      <div style="color:#9ca3af;grid-column:1/-1;text-align:center;padding:20px;font-size:14px;">Belum ada kampanye aktif</div>
    </div>

    <h4 style="margin:24px 0 10px;color:#111827;font-size:15px;">Daftar Kampanye Lainnya <span id="totalCount" style="color:#6b7280;font-weight:normal;font-size:13px;"></span></h4>
    <input type="text" id="searchKampanye" placeholder="Cari berdasarkan ID atau nama kampanye..." onkeyup="renderList()" style="width:100%;padding:10px 14px;border:1px solid #e5e7eb;border-radius:8px;font-size:14px;margin-bottom:12px;box-sizing:border-box;">
    <div id="kampanyeList" style="border:1px solid #e5e7eb;border-radius:8px;max-height:500px;overflow-y:auto;">
      <div style="color:#9ca3af;text-align:center;padding:20px;">Memuat...</div>
    </div>
  </div>

<script>
let allCampaigns = [];

function formatRupiah(n) {
  return "Rp " + Number(n || 0).toLocaleString("id-ID");
}

async function loadKampanye() {
  try {
    const res = await fetch("/api/kampanye-list");
    const json = await res.json();
    allCampaigns = json.campaigns || [];
    const filterStatus = document.getElementById("filterStatus");
    if (json.filter_active) {
      const aktifCount = allCampaigns.filter(c => c.aktif).length;
      filterStatus.style.display = "block";
      filterStatus.textContent = `Filter aktif: ${aktifCount} kampanye dipilih ditampilkan di Flow. Kalau kosong, semua kampanye akan ditampilkan.`;
    } else {
      filterStatus.style.display = "block";
      filterStatus.textContent = "Belum ada kampanye dipilih — saat ini SEMUA kampanye ditampilkan di Flow (default).";
    }
    render();
  } catch (e) {
    document.getElementById("kampanyeAktifGrid").innerHTML = `<div style="color:#dc2626;grid-column:1/-1;">Error: ${e.message}</div>`;
  }
}

function render() {
  renderAktif();
  renderList();
}

function renderAktif() {
  const aktif = allCampaigns.filter(c => c.aktif);
  const grid = document.getElementById("kampanyeAktifGrid");
  document.getElementById("aktifCount").textContent = `(${aktif.length})`;

  if (!aktif.length) {
    grid.innerHTML = `<div style="color:#9ca3af;grid-column:1/-1;text-align:center;padding:20px;font-size:14px;">Belum ada kampanye aktif — centang dari daftar di bawah</div>`;
    return;
  }
  grid.innerHTML = aktif.map(c => {
    const pct = c.target > 0 ? Math.min(100, Math.round(c.total_donasi / c.target * 100)) : 0;
    return `
      <label style="border:2px solid #5b3df0;border-radius:10px;padding:12px;cursor:pointer;background:#fff;display:block;">
        <div style="display:flex;gap:12px;align-items:flex-start;">
          <input type="checkbox" checked onchange="toggleAktif('${c.id}', this.checked)" style="width:18px;height:18px;margin-top:2px;cursor:pointer;">
          ${c.image ? `<img src="${c.image}" style="width:60px;height:60px;object-fit:cover;border-radius:6px;flex-shrink:0;" onerror="this.style.display='none'">` : ''}
          <div style="flex:1;min-width:0;">
            <div style="font-weight:600;font-size:14px;color:#111827;margin-bottom:4px;line-height:1.3;">${(c.name || '').replace(/</g,'&lt;')}</div>
            <div style="font-size:12px;color:#6b7280;">ID: ${c.id}</div>
            <div style="font-size:12px;color:#374151;margin-top:4px;">${formatRupiah(c.total_donasi)} <span style="color:#9ca3af;">/ ${formatRupiah(c.target)} (${pct}%)</span></div>
            <div style="height:4px;background:#e5e7eb;border-radius:2px;margin-top:4px;overflow:hidden;"><div style="height:100%;background:#5b3df0;width:${pct}%;"></div></div>
          </div>
        </div>
      </label>
    `;
  }).join("");
}

function renderList() {
  const nonAktif = allCampaigns.filter(c => !c.aktif);
  const q = (document.getElementById("searchKampanye").value || "").toLowerCase().trim();
  const filtered = q ? nonAktif.filter(c =>
    (c.name || "").toLowerCase().includes(q) || (c.id || "").toLowerCase().includes(q)
  ) : nonAktif;

  document.getElementById("totalCount").textContent = `(${filtered.length}${q ? ` dari ${nonAktif.length}` : ''})`;

  const listEl = document.getElementById("kampanyeList");
  if (!filtered.length) {
    listEl.innerHTML = `<div style="color:#9ca3af;text-align:center;padding:30px;font-size:14px;">${q ? 'Tidak ada hasil untuk "' + q + '"' : 'Semua kampanye sudah aktif'}</div>`;
    return;
  }
  listEl.innerHTML = filtered.map((c, i) => `
    <label style="display:flex;align-items:center;gap:12px;padding:10px 14px;cursor:pointer;${i < filtered.length-1 ? 'border-bottom:1px solid #f3f4f6;' : ''}" onmouseover="this.style.background='#f9fafb'" onmouseout="this.style.background='#fff'">
      <input type="checkbox" onchange="toggleAktif('${c.id}', this.checked)" style="width:16px;height:16px;cursor:pointer;flex-shrink:0;">
      <span style="font-size:13px;color:#6b7280;font-family:monospace;min-width:60px;">${c.id}</span>
      <span style="font-size:14px;color:#111827;flex:1;">${(c.name || '').replace(/</g,'&lt;')}</span>
    </label>
  `).join("");
  document.getElementById("totalCount").textContent = `(${filtered.length}${q ? ` dari ${nonAktif.length}` : ''})`;
}

function toggleAktif(id, checked) {
  const c = allCampaigns.find(x => x.id === id);
  if (c) c.aktif = checked;
  render();
}

function selectAll() {
  allCampaigns.forEach(c => c.aktif = true);
  render();
}

function clearAll() {
  allCampaigns.forEach(c => c.aktif = false);
  render();
}

async function saveSelection() {
  const btn = document.getElementById("btnSimpan");
  btn.disabled = true;
  btn.textContent = "Menyimpan...";
  try {
    const ids = allCampaigns.filter(c => c.aktif).map(c => c.id);
    const res = await fetch("/api/settings/kampanye-aktif", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({kampanye_aktif: ids})
    });
    const json = await res.json();
    if (json.status === "ok") {
      alert(`✓ Tersimpan: ${ids.length} kampanye akan ditampilkan di Flow.`);
      loadKampanye();
    } else {
      alert("Gagal: " + (json.error || "unknown"));
    }
  } catch (e) {
    alert("Error: " + e.message);
  } finally {
    btn.disabled = false;
    btn.textContent = "Simpan";
  }
}

loadKampanye();
</script>
"""
    return Response(render_page("kampanye", "Kelola Kampanye", "Pilih kampanye yang muncul di Flow donasi", body), mimetype="text/html")


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
