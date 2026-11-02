# -*- coding: utf-8 -*-
"""
ALERT LIQUIDITY SWEEP (versi GitHub Actions) -> Telegram
Satu kali jalan = satu kali scan semua timeframe (15m, 1h, 4h), lalu kirim sinyal BARU ke Telegram.
GitHub menjalankan file ini tiap 15 menit (lihat .github/workflows/liq-sweep.yml).
TOKEN dan CHAT_ID diambil dari GitHub Secrets (bukan dari file). Riwayat sinyal disimpan di folder state/.
"""
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)
os.chdir(BASE)
import st_liq_sweep as sq  # noqa: E402
import alert_liq_sweep as al  # noqa: E402

STATE_DIR = os.path.join(BASE, "state")
SEEN = os.path.join(STATE_DIR, "seen.json")
TFS = ["15", "60", "240"]
WORKERS = 6
NOTICE_JAM = 6            # pemberitahuan gagal-terhubung paling sering tiap 6 jam

sq.PAKAI_CANDLE_BERJALAN = False
sq.SINYAL_BARU = 2


def log(msg):
    print(f"[{datetime.now(timezone.utc):%d-%b %H:%M:%S} UTC] {msg}", flush=True)


def muat_seen():
    try:
        d = json.load(open(SEEN, encoding="utf-8"))
        return d if isinstance(d, dict) else None
    except Exception:
        return None


def simpan_seen(d):
    os.makedirs(STATE_DIR, exist_ok=True)
    batas = time.time() - al.SIMPAN_HARI * 86400
    d = {k: v for k, v in d.items() if v >= batas}
    json.dump(d, open(SEEN, "w", encoding="utf-8"))


def scan_satu(args):
    fn, sid, label = args
    hasil = []
    for tf in TFS:
        try:
            candles, _ = sq.get_candles(fn, sid, tf)
            for r in sq.scan_candles(candles):
                hasil.append((sq.TF_NAMES[tf], label, r))
        except Exception:
            pass
        time.sleep(0.05)
    return hasil


def main():
    token = os.environ.get("TOKEN", "").strip()
    chat_id = os.environ.get("CHAT_ID", "").strip()
    manual = os.environ.get("GITHUB_EVENT_NAME", "") == "workflow_dispatch"
    if not token:
        print("TOKEN belum diisi di GitHub Secrets (Settings > Secrets and variables > Actions).")
        sys.exit(2)
    if not chat_id:
        chat_id, err = al.ambil_chat_id(token)
        if err:
            print("CHAT_ID belum diisi di Secrets dan tidak bisa diambil otomatis:", err)
            sys.exit(2)
        log("CHAT_ID diambil otomatis dari pesan terakhir ke bot. Sebaiknya isi CHAT_ID di Secrets.")

    seen = muat_seen()
    pertama = seen is None
    seen = seen or {}
    t0 = time.time()

    conn = sq.connect(None, ranked=True, limit=True)
    if conn is None:
        log("Semua bursa gagal dihubungi dari server GitHub.")
        terakhir = seen.get("_notice_gagal", 0)
        if manual or pertama or time.time() - terakhir > NOTICE_JAM * 3600:
            al.kirim(token, chat_id, "⚠️ Bot Liquidity Sweep (GitHub): semua bursa (Gate.io, OKX, Bybit, KuCoin, Binance) gagal dihubungi dari server GitHub. "
                                     "Kemungkinan alamat server diblokir. Pindah ke VPS atau jalankan di laptop.")
            seen["_notice_gagal"] = time.time()
            simpan_seen(seen)
        sys.exit(0)
    ex_name, fn, syms = conn
    log(f"terhubung ke {ex_name}, {len(syms)} koin")

    tugas = [(fn, sid, label) for sid, label, _, _ in syms]
    hasil = []
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        for h in ex.map(scan_satu, tugas):
            hasil.extend(h)

    baru = []
    for T, label, r in hasil:
        key = f"{label}|{T}|{r['arah']}|{r['zona_key']}|{r['t_sinyal']}"
        if key not in seen:
            seen[key] = time.time()
            baru.append((T, label, r))
    baru.sort(key=lambda x: (x[2]["umur_sinyal"], x[0]))
    dur = time.time() - t0
    log(f"scan selesai: {len(hasil)} sinyal aktif, {len(baru)} baru ({dur:.0f} detik)")

    if pertama:
        teks = ("✅ Bot Liquidity Sweep (GitHub) aktif.\nScan tiap 15 menit untuk 15m, 1h, 4h.\n"
                "Syarat: zona liquidity, candle 1 (body 50% di luar zona + dot putih), candle 2 menutup di luar zona + dot putih, divergence RSI, RR min 1.5.")
        if baru:
            teks += f"\n\nSinyal aktif saat dinyalakan ({len(baru)}), tidak dikirim satu-satu:\n" + "\n".join(
                f"{r['arah']} {label} {T} ({r['t_sinyal']})" for T, label, r in baru)
        al.kirim(token, chat_id, teks)
    else:
        for T, label, r in baru:
            al.kirim(token, chat_id, al.pesan(T, label, r))
            time.sleep(0.4)
    if manual:
        al.kirim(token, chat_id, f"🧪 Uji manual selesai. Bursa: {ex_name}, {len(syms)} koin, {dur:.0f} detik. "
                                 f"Sinyal aktif: {len(hasil)}, baru: {len(baru)}.")
    simpan_seen(seen)


if __name__ == "__main__":
    main()
