import os
import json
import logging
import requests
from datetime import datetime
from flask import Flask, request, jsonify

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ─── ENV VARIABLES ───────────────────────────────────────────────
MIDTRANS_SERVER_KEY = os.environ.get("MIDTRANS_SERVER_KEY", "")
MIDTRANS_IS_PRODUCTION = os.environ.get("MIDTRANS_IS_PRODUCTION", "false").lower() == "true"
WA_PHONE_NUMBER_ID = os.environ.get("WA_PHONE_NUMBER_ID", "")
WA_ACCESS_TOKEN = os.environ.get("WA_ACCESS_TOKEN", "")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

MIDTRANS_BASE_URL = (
    "https://app.midtrans.com/snap/v1/transactions"
    if MIDTRANS_IS_PRODUCTION
    else "https://app.sandbox.midtrans.com/snap/v1/transactions"
)

RAIHMIMPI_API = "https://api.raihmimpi.id/campaign"

# ─── AMBIL DATA KAMPANYE ─────────────────────────────────────────
def get_campaigns():
    """Ambil daftar kampanye aktif dari API Raihmimpi"""
    try:
        resp = requests.get(RAIHMIMPI_API, timeout=10)
        resp.raise_for_status()
        campaigns = resp.json()
        return campaigns
    except Exception as e:
        logger.error(f"Error ambil kampanye: {e}")
        return []

def format_rupiah(amount):
    """Format angka jadi Rp xxx.xxx.xxx"""
    try:
        return f"Rp {int(amount):,}".replace(",", ".")
    except:
        return str(amount)

def format_campaigns_for_flow(campaigns, limit=10):
    """Format kampanye untuk data-source WhatsApp Flow RadioButtonsGroup"""
    result = []
    for c in campaigns[:limit]:
        campaign_id = str(c.get("ID_CAMPAIGN", ""))
        name = c.get("CAMPAIGN_NAME", "")[:72]  # max 72 chars
        target = format_rupiah(c.get("TARGET_DONASI_UANG", 0))
        terkumpul = format_rupiah(c.get("TOTAL_DONASI", 0))
        mitra = c.get("NAMA_LENGKAP", "")
        description = f"Terkumpul: {terkumpul} dari {target}"[:72]
        result.append({
            "id": campaign_id,
            "title": name,
            "description": description
        })
    return result

# ─── MIDTRANS ────────────────────────────────────────────────────
def create_midtrans_payment(order_id, amount, donatur_name, phone, campaign_name):
    import base64
    auth = base64.b64encode(f"{MIDTRANS_SERVER_KEY}:".encode()).decode()
    payload = {
        "transaction_details": {
            "order_id": order_id,
            "gross_amount": int(amount)
        },
        "customer_details": {
            "first_name": donatur_name,
            "phone": phone
        },
        "item_details": [{
            "id": "DONASI-001",
            "price": int(amount),
            "quantity": 1,
            "name": f"Donasi: {campaign_name[:50]}"
        }],
        "callbacks": {
            "finish": f"https://raihmimpi.id/donasi-sukses?order_id={order_id}"
        }
    }
    resp = requests.post(
        MIDTRANS_BASE_URL,
        json=payload,
        headers={"Authorization": f"Basic {auth}", "Content-Type": "application/json"},
        timeout=15
    )
    resp.raise_for_status()
    return resp.json().get("redirect_url")

# ─── WA CLOUD API ────────────────────────────────────────────────
def send_wa_message(to_phone, message):
    url = f"https://graph.facebook.com/v19.0/{WA_PHONE_NUMBER_ID}/messages"
    payload = {
        "messaging_product": "whatsapp",
        "to": to_phone,
        "type": "text",
        "text": {"body": message}
    }
    resp = requests.post(
        url,
        json=payload,
        headers={"Authorization": f"Bearer {WA_ACCESS_TOKEN}"},
        timeout=10
    )
    logger.info(f"WA send: {resp.status_code}")

# ─── TELEGRAM ────────────────────────────────────────────────────
def notify_telegram(text):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    requests.post(url, json={
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML"
    }, timeout=10)

# ─── SIMPAN TRANSAKSI ────────────────────────────────────────────
DATA_FILE = "/app/data/transaksi.json"

def load_data():
    os.makedirs(os.path.dirname(DATA_FILE), exist_ok=True)
    if not os.path.exists(DATA_FILE):
        return []
    with open(DATA_FILE, "r") as f:
        return json.load(f)

def save_data(data):
    os.makedirs(os.path.dirname(DATA_FILE), exist_ok=True)
    with open(DATA_FILE, "w") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

# ─── ENDPOINT: WA FLOW (DENGAN ENDPOINT) ─────────────────────────
@app.route("/wa-flow", methods=["POST"])
def wa_flow_endpoint():
    """
    Endpoint utama WhatsApp Flow.
    Meta memanggil endpoint ini di setiap perpindahan screen.
    Request berisi: action, screen, data dari user
    """
    try:
        body = request.get_json()
        logger.info(f"WA Flow request: {json.dumps(body)}")

        action = body.get("action")
        screen = body.get("screen")
        data = body.get("data", {})
        flow_token = body.get("flow_token", "")

        # ── INIT: screen pertama dibuka ──────────────────────────
        if action == "INIT":
            campaigns = get_campaigns()
            kampanye_list = format_campaigns_for_flow(campaigns, limit=10)

            return jsonify({
                "screen": "PILIH_TIPE",
                "data": {
                    "kampanye_list": kampanye_list
                }
            })

        # ── NAVIGATE: user pindah screen ────────────────────────
        if action == "data_exchange":

            # Screen: setelah pilih tipe → load kampanye
            if screen == "PILIH_KAMPANYE":
                campaigns = get_campaigns()
                kampanye_list = format_campaigns_for_flow(campaigns, limit=10)
                return jsonify({
                    "screen": "PILIH_KAMPANYE",
                    "data": {
                        "kampanye_list": kampanye_list,
                        "tipe_donasi": data.get("tipe_donasi", "sekali")
                    }
                })

            # Screen: konfirmasi → buat payment link
            if screen == "SELESAI":
                nama_donatur = data.get("nama_donatur", "Donatur")
                kampanye_id = data.get("kampanye_id", "")
                kampanye_nama = data.get("kampanye_nama", "Kampanye Raihmimpi")
                nominal = data.get("nominal", 50000)
                nominal_lain = data.get("nominal_lain", 0)
                atas_nama = data.get("atas_nama", nama_donatur)
                tipe = data.get("tipe_donasi", "sekali")

                # Pakai nominal_lain kalau ada
                final_nominal = int(nominal_lain) if nominal_lain and int(nominal_lain) > 0 else int(nominal)

                # Ambil nomor HP dari flow_token
                phone = flow_token.replace("phone_", "") if flow_token.startswith("phone_") else ""

                # Buat order ID
                order_id = f"RM-{datetime.now().strftime('%Y%m%d%H%M%S')}-{kampanye_id[-4:]}"

                # Buat Midtrans link
                payment_url = create_midtrans_payment(
                    order_id, final_nominal, nama_donatur, phone, kampanye_nama
                )

                # Simpan transaksi
                transaksi = load_data()
                transaksi.append({
                    "order_id": order_id,
                    "donatur": nama_donatur,
                    "atas_nama": atas_nama,
                    "phone": phone,
                    "kampanye_id": kampanye_id,
                    "kampanye": kampanye_nama,
                    "nominal": final_nominal,
                    "tipe": tipe,
                    "status": "pending",
                    "payment_url": payment_url,
                    "created_at": datetime.now().isoformat()
                })
                save_data(transaksi)

                # Kirim link ke donatur
                if phone:
                    pesan = (
                        f"Assalamu'alaikum *{nama_donatur}*! 🤲\n\n"
                        f"Terima kasih sudah berniat berdonasi untuk:\n"
                        f"*{kampanye_nama}*\n\n"
                        f"Nominal: *{format_rupiah(final_nominal)}*\n"
                        f"Atas nama: *{atas_nama}*\n"
                        f"Tipe: *Donasi {tipe.capitalize()}*\n\n"
                        f"Silakan selesaikan donasi melalui:\n"
                        f"{payment_url}\n\n"
                        f"_Link berlaku 24 jam. Semoga menjadi amal jariyah yang berkah._ 🙏"
                    )
                    send_wa_message(phone, pesan)

                # Notif Telegram
                notify_telegram(
                    f"🔔 <b>Donasi Baru!</b>\n"
                    f"👤 {nama_donatur} ({phone})\n"
                    f"📋 {kampanye_nama}\n"
                    f"💰 {format_rupiah(final_nominal)}\n"
                    f"🔄 {tipe.capitalize()}\n"
                    f"🆔 {order_id}\n"
                    f"⏳ Menunggu pembayaran"
                )

                return jsonify({
                    "screen": "SUCCESS",
                    "data": {
                        "payment_url": payment_url,
                        "order_id": order_id,
                        "extension_message_response": {
                            "params": {
                                "flow_token": flow_token
                            }
                        }
                    }
                })

        return jsonify({"screen": "PILIH_TIPE", "data": {}})

    except Exception as e:
        logger.error(f"Error wa-flow: {e}", exc_info=True)
        return jsonify({
            "screen": "ERROR",
            "data": {"error_message": "Terjadi kesalahan, silakan coba lagi."}
        }), 500


# ─── ENDPOINT: MIDTRANS CALLBACK ─────────────────────────────────
@app.route("/midtrans-callback", methods=["POST"])
def midtrans_callback():
    try:
        data = request.get_json()
        order_id = data.get("order_id")
        status = data.get("transaction_status")
        fraud = data.get("fraud_status", "accept")

        logger.info(f"Midtrans callback: {order_id} → {status}")

        if status in ("capture", "settlement") and fraud == "accept":
            final_status = "lunas"
        elif status in ("cancel", "deny", "expire"):
            final_status = "gagal"
        else:
            final_status = status

        transaksi = load_data()
        donatur, kampanye, nominal, phone = "", "", 0, ""
        for t in transaksi:
            if t["order_id"] == order_id:
                t["status"] = final_status
                t["paid_at"] = datetime.now().isoformat()
                donatur = t.get("donatur", "")
                kampanye = t.get("kampanye", "")
                nominal = t.get("nominal", 0)
                phone = t.get("phone", "")
                break
        save_data(transaksi)

        if final_status == "lunas" and phone:
            send_wa_message(phone, (
                f"✅ *Donasi Berhasil!*\n\n"
                f"Alhamdulillah, donasi Anda telah diterima.\n\n"
                f"📋 *{kampanye}*\n"
                f"💰 {format_rupiah(nominal)}\n"
                f"🆔 {order_id}\n\n"
                f"Semoga Allah melipatgandakan kebaikan Anda. 🤲\n"
                f"_Raihmimpi.id_"
            ))
            notify_telegram(
                f"✅ <b>LUNAS!</b>\n"
                f"👤 {donatur} ({phone})\n"
                f"📋 {kampanye}\n"
                f"💰 {format_rupiah(nominal)}\n"
                f"🆔 {order_id}"
            )

        return jsonify({"status": "ok"})

    except Exception as e:
        logger.error(f"Error midtrans-callback: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500


# ─── ENDPOINT: LIST KAMPANYE (untuk debug) ───────────────────────
@app.route("/kampanye", methods=["GET"])
def list_kampanye():
    """Endpoint debug untuk cek data kampanye dari API Raihmimpi"""
    campaigns = get_campaigns()
    formatted = format_campaigns_for_flow(campaigns)
    return jsonify({
        "total": len(campaigns),
        "formatted": formatted
    })


# ─── HEALTH CHECK ────────────────────────────────────────────────
@app.route("/", methods=["GET"])
def health():
    return jsonify({
        "status": "ok",
        "service": "Raihmimpi WA Flow Backend",
        "version": "2.0.0"
    })


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
