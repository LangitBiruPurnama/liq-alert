# -*- coding: utf-8 -*-
"""
ALERT LIQUIDITY SWEEP -> Telegram
Menjalankan scan Liquidity Sweep otomatis setiap candle tertutup, lalu mengirim sinyal BARU ke Telegram.
Hanya alert, tidak membuka posisi. Logika sinyal sama dengan st_liq_sweep.py (file itu harus satu folder).

Jadwal (waktu bursa = UTC, dijalankan ~25 detik setelah candle tutup):
  15m : tiap 15 menit
  1h  : tiap jam
  4h  : tiap 4 jam (candle tutup 03/07/11/15/19/23 WIB)
Token dan chat id ada di config.txt (jangan dibagikan).
"""
import json
import os
import sys
import time
from datetime import datetime, timezone

import requests

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)
os.chdir(BASE)
import st_liq_sweep as sq  # noqa: E402

CONFIG = os.path.join(BASE, "config.txt")
SEEN = os.path.join(BASE, "liq_sweep_sudah_terkirim.json")
DELAY_DETIK = 25          # tunggu sesudah candle tutup supaya data bursa sudah lengkap
REFRESH_KOIN_JAM = 6      # daftar koin top mcap diperbarui tiap 6 jam
SIMPAN_HARI = 3

sq.PAKAI_CANDLE_BERJALAN = False
sq.SINYAL_BARU = 2


def log(msg):
    print(f"[{datetime.now():%d-%b %H:%M:%S}] {msg}", flush=True)


def baca_config():
    cfg = {}
    if os.path.exists(CONFIG):
        for ln in open(CONFIG, encoding="utf-8"):
            ln = ln.strip()
            if ln and not ln.startswith("#") and "=" in ln:
                k, v = ln.split("=", 1)
                cfg[k.strip().upper()] = v.strip()
    return cfg


def simpan_chat_id(chat_id):
    lines = []
    ada = False
    if os.path.exists(CONFIG):
        for ln in open(CONFIG, encoding="utf-8").read().splitlines():
            if ln.strip().upper().startswith("CHAT_ID"):
                lines.append(f"CHAT_ID={chat_id}")
                ada = True
            else:
                lines.append(ln)
    if not ada:
        lines.append(f"CHAT_ID={chat_id}")
    open(CONFIG, "w", encoding="utf-8").write("\n".join(lines) + "\n")


def api(token, method, **data):
    r = requests.post(f"https://api.telegram.org/bot{token}/{method}", data=data, timeout=20)
    return r.json()


def ambil_chat_id(token):
    j = api(token, "getUpdates")
    if not j.get("ok"):
        return None, f"Telegram menolak token ({j.get('description')}). Cek TOKEN di config.txt."
    for u in reversed(j.get("result", [])):
        m = u.get("message") or u.get("channel_post")
        if m and m.get("chat", {}).get("id"):
            return str(m["chat"]["id"]), None
    return None, "Belum ada pesan ke bot. Buka bot di Telegram, tekan START, kirim 'halo', lalu jalankan lagi."


def kirim(token, chat_id, teks):
    for coba in range(3):
        try:
            j = api(token, "sendMessage", chat_id=chat_id, text=teks[:4000], disable_web_page_preview="true")
            if j.get("ok"):
                return True
            log(f"Telegram gagal: {j.get('description')}")
        except Exception as e:
            log(f"Telegram error: {type(e).__name__}")
        time.sleep(3)
    return False


def fmt(x):
    return "n/a" if x is None else f"{x:.6g}"


def pesan(T, label, r):
    ikon = "🟢" if r["arah"] == "BUY" else "🔴"
    sisi = "zona MERAH" if r["arah"] == "BUY" else "zona HIJAU"
    out = [f"{ikon} {r['arah']} | LIQ SWEEP | {label} {T}"
           f"{'  #' + str(sq.RANK[label]) if label in sq.RANK else ''}"]
    out.append(f"{sisi} {fmt(r['z_bot'])} - {fmt(r['z_top'])} (terbentuk {r['t_zona']})")
    out.append(f"Candle 1: {r['t_c1']} WIB (body di luar zona {r['persen'] * 100:.0f}%)")
    out.append(f"Candle 2 (ENTRY): {r['t_sinyal']} WIB ({r['umur_sinyal']} candle lalu), tutup {fmt(r['tutup'])}")
    dn = "Bullish" if r["arah"] == "BUY" else "Bearish"
    out.append(f"{dn} divergence")
    out.append(f"Harga: {fmt(r['div_p1'])} ({r['div_t1']}) -> {fmt(r['div_p2'])} ({r['div_t2']})")
    out.append(f"RSI {'lembah' if r['arah'] == 'BUY' else 'puncak'}: {r['div_r1']:.1f} ({r['div_tr1']}) -> {r['div_r2']:.1f} ({r['div_tr2']})")
    out.append(f"Stop {fmt(r['stop'])} (tepi kotak, risiko {r['risk']:.2f}%)")
    out.append(f"Target {fmt(r['target'])} [{r['jenis']}] (jarak {r['gap_pct']:.1f}%, R:R {r['rr']:.2f})")
    out.append(f"Harga sekarang {fmt(r['px'])} ({r['jarak_px_pct']:+.2f}% dari tutup)")
    return "\n".join(out)


def muat_seen():
    try:
        d = json.load(open(SEEN, encoding="utf-8"))
    except Exception:
        return None
    return d if isinstance(d, dict) else None


def simpan_seen(d):
    batas = time.time() - SIMPAN_HARI * 86400
    d = {k: v for k, v in d.items() if v >= batas}
    json.dump(d, open(SEEN, "w", encoding="utf-8"))
    return d


def tf_due(ts):
    """TF yang candlenya baru tutup pada waktu ts (epoch detik)."""
    t = datetime.fromtimestamp(ts, timezone.utc)
    due = ["15"]
    if t.minute == 0:
        due.append("60")
        if t.hour % 4 == 0:
            due.append("240")
    return due


def scan(tfs, conn):
    ex_name, fn, syms = conn
    hasil = []
    for sid, label, _, _ in syms:
        for tf in tfs:
            try:
                candles, _ = sq.get_candles(fn, sid, tf)
                for r in sq.scan_candles(candles):
                    hasil.append((sq.TF_NAMES[tf], label, r))
            except Exception:
                pass
            time.sleep(0.05)
    return hasil


def jalan_scan(tfs, conn, token, chat_id, seen, diam):
    t0 = time.time()
    hasil = scan(tfs, conn)
    baru = []
    for T, label, r in hasil:
        key = f"{label}|{T}|{r['arah']}|{r['zona_key']}|{r['t_sinyal']}"
        if key not in seen:
            seen[key] = time.time()
            baru.append((T, label, r))
    baru.sort(key=lambda x: (x[2]["umur_sinyal"], x[0]))
    log(f"scan {','.join(sq.TF_NAMES[t] for t in tfs)}: {len(hasil)} sinyal aktif, {len(baru)} baru ({time.time() - t0:.0f} detik)")
    if baru and not diam:
        for T, label, r in baru:
            kirim(token, chat_id, pesan(T, label, r))
            time.sleep(0.4)
    return baru, seen


def main():
    cfg = baca_config()
    token = cfg.get("TOKEN", "")
    if not token or "PASTE" in token.upper():
        print("Isi dulu TOKEN di config.txt (buka dengan Notepad), lalu jalankan lagi.")
        sys.exit(2)
    chat_id = cfg.get("CHAT_ID", "")
    try:
        if not chat_id:
            chat_id, err = ambil_chat_id(token)
            if err:
                print(err)
                sys.exit(2)
            simpan_chat_id(chat_id)
            log("Chat ID ditemukan dan disimpan di config.txt")
    except Exception as e:
        print(f"Tidak bisa menghubungi Telegram ({type(e).__name__}). Cek internet.")
        sys.exit(2)

    seen = muat_seen()
    pertama = seen is None
    seen = seen or {}

    conn = None
    kapan_koin = 0
    log("Bot alert LIQUIDITY SWEEP mulai. Jangan tutup jendela ini.")
    kirim(token, chat_id, "✅ Bot alert LIQUIDITY SWEEP aktif.\nScan: 15m tiap 15 menit, 1h tiap jam, 4h tiap 4 jam.\nSyarat: zona liquidity terbentuk, candle 1 (body 50% di luar zona + dot putih) lalu candle 2 menutup di luar zona + dot putih, ada divergence RSI, RR min 1.5.")

    mulai = True
    while True:
        try:
            if conn is None or time.time() - kapan_koin > REFRESH_KOIN_JAM * 3600:
                c = sq.connect(None, ranked=True, limit=True)
                if c is not None:
                    conn, kapan_koin = c, time.time()
                elif conn is None:
                    log("Belum bisa terhubung ke bursa, coba lagi 60 detik.")
                    time.sleep(60)
                    continue
            if mulai:
                mulai = False
                baru, seen = jalan_scan(["15", "60", "240"], conn, token, chat_id, seen, diam=pertama)
                if pertama and baru:
                    baris = [f"Sinyal aktif saat bot dinyalakan ({len(baru)}), tidak dikirim satu-satu:"]
                    for T, label, r in baru:
                        baris.append(f"{r['arah']} {label} {T} ({r['t_sinyal']})")
                    kirim(token, chat_id, "\n".join(baris))
                seen = simpan_seen(seen)
            now = time.time()
            nxt = (int(now // 900) + 1) * 900
            time.sleep(max(1, nxt + DELAY_DETIK - now))
            tfs = tf_due(nxt)
            baru, seen = jalan_scan(tfs, conn, token, chat_id, seen, diam=False)
            seen = simpan_seen(seen)
        except KeyboardInterrupt:
            log("Dihentikan.")
            return
        except Exception as e:
            log(f"Error: {type(e).__name__}: {str(e)[:100]} - lanjut putaran berikutnya")
            time.sleep(30)


if __name__ == "__main__":
    main()
