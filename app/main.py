import os
import json
import requests
import schedule
import time
from datetime import datetime, timedelta
from anthropic import Anthropic

# ============================================================
# KONFIGURASI - ambil dari environment variables
# ============================================================
META_ACCESS_TOKEN = os.environ.get("META_ACCESS_TOKEN")
AD_ACCOUNT_ID = os.environ.get("AD_ACCOUNT_ID", "act_348374300397771")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")

anthropic = Anthropic(api_key=ANTHROPIC_API_KEY)

# ============================================================
# THRESHOLD SCALING
# ============================================================
ROAS_SCALE_UP = 3.0      # ROAS >= 3x → scale up 20%
ROAS_HOLD_MIN = 2.0      # ROAS 2x-3x → hold
ROAS_PAUSE_DAYS = 3      # ROAS < 2x selama 3 hari → pause
SCALE_UP_PERCENT = 0.20  # Naik 20%
MAX_BUDGET_DAILY = 500000  # Maksimum budget harian (Rp 500.000)

# ============================================================
# STEP 1: AMBIL DATA ROAS DARI META ADS API
# ============================================================
def get_campaigns():
    """Ambil semua campaign aktif dari Meta Ads API"""
    url = f"https://graph.facebook.com/v19.0/{AD_ACCOUNT_ID}/campaigns"
    params = {
        "access_token": META_ACCESS_TOKEN,
        "fields": "id,name,status,daily_budget,lifetime_budget",
        "filtering": '[{"field":"effective_status","operator":"IN","value":["ACTIVE"]}]',
    }
    response = requests.get(url, params=params)
    data = response.json()
    
    if "error" in data:
        print(f"[ERROR] Gagal ambil campaigns: {data['error']['message']}")
        return []
    
    return data.get("data", [])


def get_campaign_roas(campaign_id, days=7):
    """Ambil data ROAS campaign untuk N hari terakhir"""
    end_date = datetime.now().strftime("%Y-%m-%d")
    start_date = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    
    url = f"https://graph.facebook.com/v19.0/{campaign_id}/insights"
    params = {
        "access_token": META_ACCESS_TOKEN,
        "fields": "campaign_name,spend,purchase_roas,actions,action_values",
        "time_range": json.dumps({"since": start_date, "until": end_date}),
        "level": "campaign",
    }
    response = requests.get(url, params=params)
    data = response.json()
    
    if "error" in data:
        print(f"[ERROR] Gagal ambil insights campaign {campaign_id}: {data['error']['message']}")
        return None
    
    results = data.get("data", [])
    if not results:
        return None
    
    insight = results[0]
    
    # Ambil ROAS dari purchase_roas
    roas_value = 0
    purchase_roas = insight.get("purchase_roas", [])
    if purchase_roas:
        roas_value = float(purchase_roas[0].get("value", 0))
    
    # Ambil total donasi (action_values)
    total_donation = 0
    action_values = insight.get("action_values", [])
    for av in action_values:
        if av.get("action_type") == "offsite_conversion.fb_pixel_purchase":
            total_donation = float(av.get("value", 0))
    
    # Ambil jumlah donatur
    total_donors = 0
    actions = insight.get("actions", [])
    for action in actions:
        if action.get("action_type") == "offsite_conversion.fb_pixel_purchase":
            total_donors = int(action.get("value", 0))
    
    return {
        "campaign_id": campaign_id,
        "campaign_name": insight.get("campaign_name", ""),
        "spend": float(insight.get("spend", 0)),
        "roas_7d": roas_value,
        "total_donation": total_donation,
        "total_donors": total_donors,
        "date_range": f"{start_date} s/d {end_date}",
    }


def get_campaign_roas_3d(campaign_id):
    """Ambil ROAS 3 hari terakhir untuk deteksi trend"""
    end_date = datetime.now().strftime("%Y-%m-%d")
    start_date = (datetime.now() - timedelta(days=3)).strftime("%Y-%m-%d")
    
    url = f"https://graph.facebook.com/v19.0/{campaign_id}/insights"
    params = {
        "access_token": META_ACCESS_TOKEN,
        "fields": "purchase_roas,spend",
        "time_range": json.dumps({"since": start_date, "until": end_date}),
        "level": "campaign",
    }
    response = requests.get(url, params=params)
    data = response.json()
    results = data.get("data", [])
    
    if not results:
        return 0
    
    purchase_roas = results[0].get("purchase_roas", [])
    if purchase_roas:
        return float(purchase_roas[0].get("value", 0))
    return 0


# ============================================================
# STEP 2: ANALISIS DENGAN CLAUDE AI
# ============================================================
def analyze_with_claude(campaigns_data):
    """Kirim data ke Claude untuk analisis dan rekomendasi scaling"""
    
    campaigns_text = ""
    for c in campaigns_data:
        campaigns_text += f"""
Campaign: {c['campaign_name']} (ID: {c['campaign_id']})
- Budget harian saat ini: Rp {c.get('daily_budget', 0):,.0f}
- Total spend 7 hari: Rp {c['spend'] * 15000:,.0f} (≈ USD {c['spend']:.2f})
- ROAS 7 hari: {c['roas_7d']:.2f}x
- ROAS 3 hari: {c['roas_3d']:.2f}x
- Total donasi masuk: Rp {c['total_donation'] * 15000:,.0f}
- Jumlah donatur: {c['total_donors']} orang
- Periode: {c['date_range']}
"""

    prompt = f"""Kamu adalah analis iklan digital untuk lembaga crowdfunding sosial Raihmimpi.id yang fokus pada penggalangan donasi.

Berikut data performa campaign Meta Ads hari ini:
{campaigns_text}

Aturan scaling yang berlaku:
- ROAS >= 3.0x → SCALE UP budget 20% (maksimal Rp {MAX_BUDGET_DAILY:,.0f}/hari)
- ROAS 2.0x - 2.9x → HOLD (tidak diubah)
- ROAS < 2.0x selama 3 hari berturut-turut → PAUSE campaign
- Jika ROAS 3 hari lebih tinggi dari 7 hari → trending positif, pertimbangkan scale up meski ROAS 7 hari belum 3x

Untuk setiap campaign, berikan:
1. KEPUTUSAN: SCALE_UP / HOLD / PAUSE
2. ALASAN: singkat dan jelas
3. BUDGET_BARU: nominal budget harian baru dalam Rupiah (jika SCALE_UP)
4. CATATAN: insight tambahan jika ada

Format respons dalam JSON seperti ini:
{{
  "analisis_tanggal": "{datetime.now().strftime('%Y-%m-%d')}",
  "campaigns": [
    {{
      "campaign_id": "xxx",
      "campaign_name": "xxx",
      "keputusan": "SCALE_UP/HOLD/PAUSE",
      "alasan": "...",
      "budget_lama": 0,
      "budget_baru": 0,
      "catatan": "..."
    }}
  ],
  "ringkasan": "ringkasan singkat kondisi keseluruhan campaign Raihmimpi hari ini"
}}"""

    response = anthropic.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=2000,
        messages=[{"role": "user", "content": prompt}]
    )
    
    raw = response.content[0].text
    # Bersihkan markdown jika ada
    clean = raw.replace("```json", "").replace("```", "").strip()
    
    try:
        return json.loads(clean)
    except:
        print(f"[ERROR] Gagal parse JSON dari Claude: {raw}")
        return None


# ============================================================
# STEP 3: EKSEKUSI PERUBAHAN BUDGET KE META
# ============================================================
def update_campaign_budget(campaign_id, new_budget_idr):
    """Update budget campaign di Meta Ads API"""
    # Konversi IDR ke unit Meta (dalam sen USD, tapi Meta pakai currency akun)
    # Meta menyimpan budget dalam currency terkecil (sen/rupiah)
    new_budget_meta = int(new_budget_idr * 100)  # dalam sen Rupiah
    
    url = f"https://graph.facebook.com/v19.0/{campaign_id}"
    params = {
        "access_token": META_ACCESS_TOKEN,
        "daily_budget": new_budget_meta,
    }
    response = requests.post(url, params=params)
    data = response.json()
    
    if data.get("success"):
        print(f"[OK] Budget campaign {campaign_id} diupdate ke Rp {new_budget_idr:,.0f}")
        return True
    else:
        print(f"[ERROR] Gagal update budget: {data}")
        return False


def pause_campaign(campaign_id):
    """Pause campaign di Meta Ads"""
    url = f"https://graph.facebook.com/v19.0/{campaign_id}"
    params = {
        "access_token": META_ACCESS_TOKEN,
        "status": "PAUSED",
    }
    response = requests.post(url, params=params)
    data = response.json()
    
    if data.get("success"):
        print(f"[OK] Campaign {campaign_id} di-PAUSE")
        return True
    else:
        print(f"[ERROR] Gagal pause campaign: {data}")
        return False


# ============================================================
# STEP 4: SIMPAN LOG
# ============================================================
def save_log(analysis_result, execution_results):
    """Simpan log ke file JSON"""
    log_entry = {
        "timestamp": datetime.now().isoformat(),
        "analysis": analysis_result,
        "execution": execution_results,
    }
    
    log_file = "scaling_log.json"
    logs = []
    
    if os.path.exists(log_file):
        with open(log_file, "r") as f:
            try:
                logs = json.load(f)
            except:
                logs = []
    
    logs.append(log_entry)
    
    # Simpan hanya 90 hari terakhir
    if len(logs) > 90:
        logs = logs[-90:]
    
    with open(log_file, "w") as f:
        json.dump(logs, f, indent=2, ensure_ascii=False)
    
    print(f"[LOG] Disimpan ke {log_file}")


# ============================================================
# MAIN: JALANKAN SISTEM SCALING
# ============================================================
def run_scaling():
    print(f"\n{'='*60}")
    print(f"[START] Raihmimpi AI Scaling - {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"{'='*60}")
    
    # Step 1: Ambil data campaigns
    print("\n[1] Mengambil data campaigns dari Meta Ads API...")
    campaigns = get_campaigns()
    
    if not campaigns:
        print("[STOP] Tidak ada campaign aktif ditemukan.")
        return
    
    print(f"[OK] Ditemukan {len(campaigns)} campaign aktif")
    
    # Step 2: Ambil data ROAS per campaign
    print("\n[2] Mengambil data ROAS...")
    campaigns_data = []
    for campaign in campaigns:
        campaign_id = campaign["id"]
        roas_data = get_campaign_roas(campaign_id, days=7)
        
        if roas_data:
            roas_data["roas_3d"] = get_campaign_roas_3d(campaign_id)
            
            # Ambil budget harian
            daily_budget = campaign.get("daily_budget", 0)
            roas_data["daily_budget"] = int(daily_budget) / 100 if daily_budget else 0
            
            campaigns_data.append(roas_data)
            print(f"  - {roas_data['campaign_name']}: ROAS {roas_data['roas_7d']:.2f}x (7d), {roas_data['roas_3d']:.2f}x (3d)")
    
    if not campaigns_data:
        print("[STOP] Tidak ada data ROAS tersedia.")
        return
    
    # Step 3: Analisis dengan Claude
    print("\n[3] Menganalisis dengan Claude AI...")
    analysis = analyze_with_claude(campaigns_data)
    
    if not analysis:
        print("[STOP] Gagal mendapat analisis dari Claude.")
        return
    
    print(f"\n[RINGKASAN CLAUDE] {analysis.get('ringkasan', '')}")
    
    # Step 4: Eksekusi keputusan
    print("\n[4] Mengeksekusi keputusan scaling...")
    execution_results = []
    
    for rec in analysis.get("campaigns", []):
        campaign_id = rec["campaign_id"]
        keputusan = rec["keputusan"]
        
        print(f"\n  Campaign: {rec['campaign_name']}")
        print(f"  Keputusan: {keputusan}")
        print(f"  Alasan: {rec['alasan']}")
        
        result = {"campaign_id": campaign_id, "keputusan": keputusan, "success": False}
        
        if keputusan == "SCALE_UP":
            budget_baru = rec.get("budget_baru", 0)
            if budget_baru > 0:
                success = update_campaign_budget(campaign_id, budget_baru)
                result["budget_baru"] = budget_baru
                result["success"] = success
                print(f"  Budget baru: Rp {budget_baru:,.0f}")
        
        elif keputusan == "PAUSE":
            success = pause_campaign(campaign_id)
            result["success"] = success
        
        elif keputusan == "HOLD":
            print(f"  Tidak ada perubahan budget.")
            result["success"] = True
        
        execution_results.append(result)
    
    # Step 5: Simpan log
    print("\n[5] Menyimpan log...")
    save_log(analysis, execution_results)
    
    print(f"\n{'='*60}")
    print(f"[SELESAI] Scaling selesai dijalankan!")
    print(f"{'='*60}\n")


# ============================================================
# SCHEDULER - Jalankan setiap hari jam 23.00 WIB (16.00 UTC)
# ============================================================
if __name__ == "__main__":
    print("🚀 Raihmimpi AI Scaling System aktif...")
    print("⏰ Dijadwalkan setiap hari jam 23.00 WIB")
    
    # Jalankan sekali saat startup untuk testing
    run_scaling()
    
    # Schedule harian jam 16:00 UTC = 23:00 WIB
    schedule.every().day.at("16:00").do(run_scaling)
    
    while True:
        schedule.run_pending()
        time.sleep(60)
