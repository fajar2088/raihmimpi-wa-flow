import os
import json
import base64
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

def get_campaigns():
    try:
        resp = requests.get(RAIHMIMPI_API, timeout=10)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        logger.error(f"Error ambil kampanye: {e}")
        return []

def fetch_and_resize_image(url, max_size_kb=90, target_dim=150):
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

def format_campaigns_for_flow(campaigns, limit=10):
    result = []
    for c in campaigns[:limit]:
        campaign_id = str(c.get("ID_CAMPAIGN", ""))
        name = c.get("CAMPAIGN_NAME", "")[:72]
        terkumpul = format_rupiah(c.get("TOTAL_DONASI", 0))
        target = format_rupiah(c.get("TARGET_DONASI_UANG", 0))
        result.append({"id": campaign_id, "title": name, "description": f"Terkumpul: {terkumpul} dari {target}"[:72]})
    return result

def format_campaigns_with_images(campaigns, limit=3):
    """Format kampanye dengan gambar base64 untuk NavigationList (max 3 untuk performa)"""
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
    url = f"https://graph.facebook.com/v19.0/{WA_PHONE_NUMBER_ID}/messages"
    requests.post(url, json={"messaging_product": "whatsapp", "to": to_phone, "type": "text", "text": {"body": message}},
        headers={"Authorization": f"Bearer {WA_ACCESS_TOKEN}"}, timeout=10)

# ID Flow donasi (didapat dari WhatsApp Manager > Flows)
DONASI_FLOW_ID = os.environ.get("DONASI_FLOW_ID", "")

# Kata kunci yang men-trigger Flow donasi
DONASI_KEYWORDS = ["donasi", "infak", "infaq", "sedekah", "zakat", "wakaf", "berdonasi", "donatur"]

def send_wa_flow_message(to_phone, body_text="Yuk mulai donasi via Raihmimpi 🤲", cta_text="Mulai Donasi", screen="PILIH_TIPE"):
    """Kirim interactive Flow message langsung ke user (dalam window 24 jam, tanpa template)."""
    url = f"https://graph.facebook.com/v19.0/{WA_PHONE_NUMBER_ID}/messages"
    flow_token = f"phone_{to_phone}_{datetime.now().strftime('%Y%m%d%H%M%S')}"
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
                    "flow_action": "navigate",
                    "flow_action_payload": {"screen": screen, "data": {}}
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

DATA_FILE = os.environ.get("DATA_DIR", "/tmp") + "/transaksi.json"

def load_data():
    if not os.path.exists(DATA_FILE):
        return []
    with open(DATA_FILE, "r") as f:
        return json.load(f)

def save_data(data):
    with open(DATA_FILE, "w") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def handle_flow_request(decrypted_body):
    action = decrypted_body.get("action")
    screen = decrypted_body.get("screen")
    data = decrypted_body.get("data", {})
    flow_token = decrypted_body.get("flow_token", "")

    logger.info(f"Flow action={action} screen={screen}")

    if action == "ping":
        return {"version": "3.0", "data": {"status": "active"}}

    if action == "INIT":
        campaigns = get_campaigns()
        return {"screen": "PILIH_TIPE", "data": {"kampanye_list": format_campaigns_with_images(campaigns)}}

    if action == "data_exchange":
        if screen == "PILIH_KAMPANYE":
            campaigns = get_campaigns()
            return {"screen": "PILIH_KAMPANYE", "data": {"kampanye_list": format_campaigns_with_images(campaigns), "tipe_donasi": data.get("tipe_donasi", "sekali")}}

        if screen == "SELESAI":
            nama_donatur = data.get("nama_donatur", "Donatur")
            kampanye_id = data.get("kampanye_id", "")
            kampanye_nama = data.get("kampanye_nama", "Kampanye Raihmimpi")
            nominal = data.get("nominal", "50000")
            nominal_lain = data.get("nominal_lain", 0)
            atas_nama = data.get("atas_nama", nama_donatur)
            tipe = data.get("tipe_donasi", "sekali")
            final_nominal = int(nominal_lain) if nominal_lain and int(nominal_lain) > 0 else int(nominal)
            phone = flow_token.replace("phone_", "") if flow_token.startswith("phone_") else ""
            order_id = f"RM-{datetime.now().strftime('%Y%m%d%H%M%S')}-{kampanye_id[-4:]}"
            payment_url = create_midtrans_payment(order_id, final_nominal, nama_donatur, phone, kampanye_nama)
            transaksi = load_data()
            transaksi.append({"order_id": order_id, "donatur": nama_donatur, "atas_nama": atas_nama, "phone": phone,
                "kampanye_id": kampanye_id, "kampanye": kampanye_nama, "nominal": final_nominal, "tipe": tipe,
                "status": "pending", "payment_url": payment_url, "created_at": datetime.now().isoformat()})
            save_data(transaksi)
            if phone:
                send_wa_message(phone, f"Assalamu'alaikum *{nama_donatur}*! 🤲\n\nTerima kasih berniat berdonasi untuk:\n*{kampanye_nama}*\n\nNominal: *{format_rupiah(final_nominal)}*\n\nSelesaikan donasi di:\n{payment_url}\n\n_Link berlaku 24 jam. Semoga berkah._ 🙏")
            notify_telegram(f"🔔 <b>Donasi Baru!</b>\n👤 {nama_donatur} ({phone})\n📋 {kampanye_nama}\n💰 {format_rupiah(final_nominal)}\n🆔 {order_id}")
            return {"screen": "SUCCESS", "data": {"extension_message_response": {"params": {"flow_token": flow_token}}}}

    return {"screen": "PILIH_TIPE", "data": {}}

@app.route("/wa-flow", methods=["POST"])
def wa_flow_endpoint():
    try:
        body = request.get_json()
        logger.info(f"RAW BODY KEYS: {list(body.keys()) if body else 'None'}")
        if "encrypted_aes_key" in body:
            decrypted_body, aes_key, iv = decrypt_request(body)
            response_data = handle_flow_request(decrypted_body)
            encrypted_response = encrypt_response(response_data, aes_key, iv)
            return Response(encrypted_response, mimetype="text/plain")
        else:
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
        donatur, kampanye, nominal, phone = "", "", 0, ""
        for t in transaksi:
            if t["order_id"] == order_id:
                t["status"] = final_status
                t["paid_at"] = datetime.now().isoformat()
                donatur, kampanye, nominal, phone = t.get("donatur",""), t.get("kampanye",""), t.get("nominal",0), t.get("phone","")
                break
        save_data(transaksi)
        if final_status == "lunas" and phone:
            send_wa_message(phone, f"✅ *Donasi Berhasil!*\n\nAlhamdulillah donasi Anda diterima.\n\n📋 *{kampanye}*\n💰 {format_rupiah(nominal)}\n🆔 {order_id}\n\nSemoga Allah melipatgandakan kebaikan Anda. 🤲\n_Raihmimpi.id_")
            notify_telegram(f"✅ <b>LUNAS!</b>\n👤 {donatur} ({phone})\n📋 {kampanye}\n💰 {format_rupiah(nominal)}\n🆔 {order_id}")
        return jsonify({"status": "ok"})
    except Exception as e:
        logger.error(f"Error midtrans-callback: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500

@app.route("/kampanye", methods=["GET"])
def list_kampanye():
    campaigns = get_campaigns()
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

@app.route("/dashboard", methods=["GET"])
def dashboard():
    """Dashboard donasi sederhana (mirip Halosis dashboard)."""
    html = """<!DOCTYPE html>
<html lang="id">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Dashboard Donasi - Raihmimpi</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
<style>
  * { margin:0; padding:0; box-sizing:border-box; font-family:-apple-system,Segoe UI,Roboto,sans-serif; }
  body { background:#f3f4f8; color:#1f2330; padding:24px; }
  h1 { font-size:22px; margin-bottom:4px; }
  .subtitle { color:#6b7280; margin-bottom:20px; font-size:14px; }
  .cards { display:grid; grid-template-columns:repeat(auto-fit,minmax(220px,1fr)); gap:16px; margin-bottom:24px; }
  .card { background:#fff; border-radius:12px; padding:20px; box-shadow:0 1px 3px rgba(0,0,0,.08); }
  .card .icon { font-size:28px; }
  .card .value { font-size:28px; font-weight:700; color:#5b3df0; margin:8px 0 4px; }
  .card .label { color:#6b7280; font-size:14px; }
  .grid2 { display:grid; grid-template-columns:1fr 1fr; gap:16px; }
  @media (max-width: 800px) { .grid2 { grid-template-columns:1fr; } }
  .panel { background:#fff; border-radius:12px; padding:20px; box-shadow:0 1px 3px rgba(0,0,0,.08); }
  .panel h2 { font-size:16px; margin-bottom:12px; }
  table { width:100%; border-collapse:collapse; font-size:13px; }
  th, td { text-align:left; padding:8px 6px; border-bottom:1px solid #eee; }
  th { color:#6b7280; font-weight:600; }
  .badge { padding:2px 8px; border-radius:6px; font-size:11px; font-weight:600; }
  .badge.lunas { background:#dcfce7; color:#16a34a; }
  .badge.pending { background:#fef3c7; color:#d97706; }
  .badge.gagal { background:#fee2e2; color:#dc2626; }
  .refresh { font-size:12px; color:#9ca3af; margin-top:16px; }
</style>
</head>
<body>
  <h1>Dashboard Donasi Raihmimpi</h1>
  <div class="subtitle">via WhatsApp Flow &middot; +62 851-1123-4962</div>

  <div class="cards">
    <div class="card"><div class="icon">💰</div><div class="value" id="total_nominal">-</div><div class="label">Total Nominal Donasi (Lunas)</div></div>
    <div class="card"><div class="icon">📋</div><div class="value" id="total_donasi">-</div><div class="label">Total Donasi (Lunas)</div></div>
    <div class="card"><div class="icon">👥</div><div class="value" id="total_donatur">-</div><div class="label">Total Donatur</div></div>
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
    <tr>
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
</body>
</html>"""
    return Response(html, mimetype="text/html")

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
