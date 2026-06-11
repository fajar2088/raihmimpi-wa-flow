import os
import json
import base64
import logging
import requests
from datetime import datetime
from flask import Flask, request, jsonify, Response
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.backends import default_backend

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

def get_private_key():
    pem = FLOW_PRIVATE_KEY_PEM.replace("\\n", "\n")
    return serialization.load_pem_private_key(pem.encode(), password=None, backend=default_backend())

def decrypt_request(body):
    encrypted_aes_key = base64.b64decode(body["encrypted_aes_key"])
    encrypted_flow_data = base64.b64decode(body["encrypted_flow_data"])
    initial_vector = base64.b64decode(body["initial_vector"])

    private_key = get_private_key()
    aes_key = private_key.decrypt(
        encrypted_aes_key,
        padding.OAEP(mgf=padding.MGF1(algorithm=hashes.SHA256()), algorithm=hashes.SHA256(), label=None)
    )

    iv_bytes = bytearray(initial_vector)
    iv_bytes[-1] ^= 0xFF
    flipped_iv = bytes(iv_bytes)

    tag = encrypted_flow_data[-16:]
    ciphertext = encrypted_flow_data[:-16]

    decryptor = Cipher(algorithms.AES(aes_key), modes.GCM(flipped_iv, tag), backend=default_backend()).decryptor()
    decrypted = decryptor.update(ciphertext) + decryptor.finalize()
    return json.loads(decrypted), aes_key, initial_vector

def encrypt_response(response_data, aes_key, initial_vector):
    response_bytes = json.dumps(response_data).encode("utf-8")
    encryptor = Cipher(algorithms.AES(aes_key), modes.GCM(initial_vector), backend=default_backend()).encryptor()
    encrypted = encryptor.update(response_bytes) + encryptor.finalize()
    return base64.b64encode(encrypted + encryptor.tag).decode("utf-8")

def get_campaigns():
    try:
        resp = requests.get(RAIHMIMPI_API, timeout=10)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        logger.error(f"Error ambil kampanye: {e}")
        return []

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

def notify_telegram(text):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    requests.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
        json={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"}, timeout=10)

DATA_FILE = "/tmp/transaksi.json"

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
        return {"data": {"status": "active"}}

    if action == "INIT":
        campaigns = get_campaigns()
        return {"screen": "PILIH_TIPE", "data": {"kampanye_list": format_campaigns_for_flow(campaigns)}}

    if action == "data_exchange":
        if screen == "PILIH_KAMPANYE":
            campaigns = get_campaigns()
            return {"screen": "PILIH_KAMPANYE", "data": {"kampanye_list": format_campaigns_for_flow(campaigns), "tipe_donasi": data.get("tipe_donasi", "sekali")}}

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
    return jsonify({"status": "ok", "service": "Raihmimpi WA Flow Backend", "version": "3.1.0"})

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
