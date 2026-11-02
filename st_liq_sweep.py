# -*- coding: utf-8 -*-
"""
st_liq_sweep.py  -  BOT LIQUIDITY SWEEP: LuxAlgo "Buyside & Sellside Liquidity" + dot Squeeze Momentum  (alert saja)
File ini BERDIRI SENDIRI. Hanya candle yang sudah TUTUP. Level/zona diport dari kode Pine LuxAlgo
(Detection Length 7, Margin 6.9, margin zona 2.3, 3 level per sisi, 500 candle terakhir). Tanpa EMA200.

BUY (dari ZONA MERAH = sellside liquidity diambil):
  Candle 1 : low masuk area zona, minimal 50% BODY berada di atas batas atas zona (garis level), menutup di atas
             batas atas zona, dan dot squeeze PUTIH (squeeze lepas; boleh sudah putih sebelumnya).
  Candle 2 : persis sesudah candle 1, menutup di atas batas atas zona (body bebas), dot squeeze PUTIH.
  Entry di penutupan candle 2. Stop tepat di tepi bawah kotak merah.
  Filter ke-3: BULLISH DIVERGENCE (low terendah zona->candle 2 lebih rendah dari pivot low 5-60 candle sebelumnya, dan LEMBAH RSI14 lebih tinggi).
  Target = yang terdekat antara buyside liquidity (belum diambil) dan swing high. RR minimal 1.5.
SELL (dari ZONA HIJAU) = kebalikannya: high masuk area zona, minimal 50% body di bawah batas bawah zona (garis level),
  menutup di bawah; candle 2 menutup di bawah. Stop di tepi atas kotak hijau. Filter ke-3: BEARISH DIVERGENCE (high lebih tinggi, dan PUNCAK RSI14 lebih rendah). Target sellside liquidity atau swing low.
Pakai: python st_liq_sweep.py [15 60 240] [buy|sell] [baru=2] [rr=1.5] [live]
       python st_liq_sweep.py cek BTC 60 recent=3
Hasil disimpan ke st_liq_sweep_scan_TANGGAL_JAM.csv. Strategi ini belum diuji historis.
"""
import csv
import re
import sys
import time
from datetime import datetime, timedelta, timezone

import requests

TOP_N = 200
URUTAN = "mcap"          # "mcap" = peringkat market cap CoinGecko, "volume" = volume 24 jam di bursa
RANK = {}                # label -> peringkat market cap (diisi saat berjalan)
N_CANDLES = 1000         # jumlah candle yang diambil per koin
PAKAI_CANDLE_BERJALAN = True   # True = candle yang sedang berjalan ikut dihitung, seperti chart TradingView
EMA_LEN = 200

STABLES = {"USDC", "USDE", "FDUSD", "TUSD", "DAI", "USDD", "USDT", "BUSD", "PYUSD", "USD1"}
LEVERAGED = ("3L", "3S", "5L", "5S")
TF_NAMES = {"5": "5m", "15": "15m", "60": "1h", "240": "4h", "D": "1d"}
TF_MS = {"5": 300_000, "15": 900_000, "60": 3_600_000, "240": 14_400_000, "D": 86_400_000}

# Token yang BUKAN kripto murni (saham, ETF, emas, token dolar). Dibuang dari scan.
STOCK_TOKENS = {
    # gStocks Gate.io (saham/ETF AS yang di-token-kan, akhiran G)
    "AAPLG", "ABBVG", "AMATG", "AMDG", "AMZNG", "ARMG", "AVGOG", "BACG", "CATG", "CBRSG",
    "COING", "COSTG", "CRCLG", "CRWVG", "CSCOG", "CVXG", "DELLG", "EWYG", "GEG", "GLWG",
    "GOOGLG", "GSG", "HDG", "HOODG", "IBMG", "INTCG", "IVVG", "JNJG", "JPMG", "KOG",
    "LITEG", "LLYG", "LRCXG", "METAG", "MSFTG", "MSTRG", "MUG", "NBISG", "NFLXG", "NVDAG",
    "ORCLG", "PGG", "PLTRG", "QCOMG", "QQQG", "SKHYG", "SNDKG", "SOXLG", "SPCXG", "SPYG",
    "TSLAG", "UNHG", "VG", "WDCG", "WMTG", "XOMG",
    "MRVLG", "SNXXG", "AXTIG", "KORUG", "TQQQG", "MVLLG", "MBG",
    # token saham gaya Ondo (akhiran ON)
    "SKHYON",
    # xStocks (akhiran X)
    "COINX", "NVDAX", "CRCLX", "AAPLX", "METAX", "HOODX", "TSLAX", "GOOGLX", "MSTRX", "SPYX", "QQQX",
    # emas dan token dolar berbunga
    "PAXG", "XAUT", "USDY",
}

TF_NAMES = {"5": "5m", "15": "15m", "60": "1h", "240": "4h", "D": "1d"}


def http_get(url, params=None, retries=3):
    last = None
    for _ in range(retries):
        try:
            r = requests.get(url, params=params, timeout=15,
                             headers={"User-Agent": "Mozilla/5.0"})
            r.raise_for_status()
            return r.json()
        except requests.exceptions.SSLError:
            raise
        except Exception as e:
            last = e
            time.sleep(1.5)
    raise last


# Kripto asli yang namanya kebetulan berakhiran G atau ON (jangan dibuang). Tambahkan di sini kalau perlu.
CRYPTO_G_OK = {"BTG", "STG", "0G"}
EXCLUDED = set()      # nama yang dibuang, ditampilkan di layar supaya Anda bisa memeriksanya


def ok_base(base):
    if base in STABLES or base.endswith(LEVERAGED):
        return False
    # Gate gStocks selalu berakhiran G dan daftarnya terus bertambah, jadi dibuang otomatis
    # Token saham gaya Ondo berakhiran ON (contoh TSLAON, AMZNON); nama pendek seperti TON/MON aman
    is_ondo = len(base) >= 5 and base.endswith("ON") and base not in CRYPTO_G_OK
    if base in STOCK_TOKENS or is_ondo or (len(base) >= 2 and base.endswith("G") and base not in CRYPTO_G_OK):
        EXCLUDED.add(base)
        return False
    return True


def now_ms():
    return int(time.time() * 1000)


# Setiap fungsi *_candles mengembalikan (candles, candle_terakhir_masih_berjalan)
# candles = list (open, high, low, close, waktu_ms, volume), LAMA -> BARU, termasuk candle yang sedang berjalan.

# ---------- BINANCE (spot) ----------
def binance_top():
    data = http_get("https://api.binance.com/api/v3/ticker/24hr", retries=2)
    return [(d["symbol"], d["symbol"], float(d["quoteVolume"]), float(d["lastPrice"]))
            for d in data
            if d["symbol"].endswith("USDT") and ok_base(d["symbol"][:-4])]


def binance_candles(sid, tf):
    iv = {"5": "5m", "15": "15m", "60": "1h", "240": "4h", "D": "1d"}[tf]
    k = http_get("https://api.binance.com/api/v3/klines",
                 {"symbol": sid, "interval": iv, "limit": min(N_CANDLES, 1000)})
    candles = [(float(x[1]), float(x[2]), float(x[3]), float(x[4]), int(x[0]), float(x[5])) for x in k]
    return candles, bool(k) and int(k[-1][6]) > now_ms()


# ---------- OKX (perpetual swap) ----------
def okx_top():
    data = http_get("https://www.okx.com/api/v5/market/tickers",
                    {"instType": "SWAP"}, retries=2)["data"]
    rows = []
    for d in data:
        iid = d["instId"]
        if not iid.endswith("-USDT-SWAP"):
            continue
        base = iid.split("-")[0]
        if not ok_base(base):
            continue
        last = float(d["last"])
        rows.append((iid, base + "USDT", last * float(d["volCcy24h"]), last))
    return rows


def okx_candles(sid, tf):
    bar = {"5": "5m", "15": "15m", "60": "1H", "240": "4H", "D": "1D"}[tf]
    k = http_get("https://www.okx.com/api/v5/market/candles",
                 {"instId": sid, "bar": bar, "limit": 300})["data"]
    k = list(reversed(k))          # OKX kirim terbaru dulu (maksimal 300 candle)
    candles = [(float(x[1]), float(x[2]), float(x[3]), float(x[4]), int(x[0]), float(x[5])) for x in k]
    return candles, bool(k) and k[-1][8] == "0"


# ---------- BYBIT (perpetual) ----------
def bybit_top():
    r = http_get("https://api.bybit.com/v5/market/tickers", {"category": "linear"}, retries=2)
    return [(d["symbol"], d["symbol"], float(d["turnover24h"]), float(d["lastPrice"]))
            for d in r["result"]["list"]
            if d["symbol"].endswith("USDT") and ok_base(d["symbol"][:-4])]


def bybit_candles(sid, tf):
    r = http_get("https://api.bybit.com/v5/market/kline",
                 {"category": "linear", "symbol": sid, "interval": tf, "limit": min(N_CANDLES, 1000)})
    k = list(reversed(r["result"]["list"]))
    candles = [(float(x[1]), float(x[2]), float(x[3]), float(x[4]), int(x[0]), float(x[5])) for x in k]
    return candles, bool(k) and int(k[-1][0]) + TF_MS[tf] > now_ms()


# ---------- GATE.IO (spot) ----------
def gate_top():
    data = http_get("https://api.gateio.ws/api/v4/spot/tickers", retries=2)
    rows = []
    for d in data:
        pair = d["currency_pair"]
        if not pair.endswith("_USDT") or not ok_base(pair[:-5]):
            continue
        try:
            rows.append((pair, pair.replace("_", ""), float(d["quote_volume"]), float(d["last"])))
        except (TypeError, ValueError):
            continue
    return rows


def gate_candles(sid, tf):
    iv = {"5": "5m", "15": "15m", "60": "1h", "240": "4h", "D": "1d"}[tf]
    k = http_get("https://api.gateio.ws/api/v4/spot/candlesticks",
                 {"currency_pair": sid, "interval": iv, "limit": min(N_CANDLES, 1000)})
    # format Gate: [waktu_detik, vol_quote, close, high, low, open, vol_base, window_closed]
    candles = [(float(x[5]), float(x[3]), float(x[4]), float(x[2]), int(x[0]) * 1000, float(x[1])) for x in k]
    return candles, bool(k) and str(k[-1][7]).lower() != "true"


# ---------- KUCOIN (spot) ----------
def kucoin_top():
    data = http_get("https://api.kucoin.com/api/v1/market/allTickers", retries=2)["data"]["ticker"]
    rows = []
    for d in data:
        sym = d["symbol"]
        if not sym.endswith("-USDT") or not ok_base(sym[:-5]):
            continue
        try:
            rows.append((sym, sym.replace("-", ""), float(d["volValue"]), float(d["last"])))
        except (TypeError, ValueError):
            continue
    return rows


def kucoin_candles(sid, tf):
    t = {"5": "5min", "15": "15min", "60": "1hour", "240": "4hour", "D": "1day"}[tf]
    k = http_get("https://api.kucoin.com/api/v1/market/candles",
                 {"symbol": sid, "type": t})["data"]
    k = list(reversed(k[:N_CANDLES]))
    # format KuCoin: [waktu_detik, open, close, high, low, ...]
    candles = [(float(x[1]), float(x[3]), float(x[4]), float(x[2]), int(x[0]) * 1000, float(x[5])) for x in k]
    return candles, bool(k) and int(k[-1][0]) * 1000 + TF_MS[tf] > now_ms()


# ---------- GATE.IO (PERPETUAL, kontrak USDT) ----------
def gatef_top():
    data = http_get("https://api.gateio.ws/api/v4/futures/usdt/tickers", retries=2)
    rows = []
    for d in data:
        c = d["contract"]
        if not c.endswith("_USDT") or not ok_base(c[:-5]):
            continue
        try:
            vol = float(d.get("volume_24h_quote") or d.get("volume_24h_settle") or 0)
            rows.append((c, c.replace("_", ""), vol, float(d["last"])))
        except (TypeError, ValueError):
            continue
    return rows


def gatef_candles(sid, tf):
    iv = {"5": "5m", "15": "15m", "60": "1h", "240": "4h", "D": "1d"}[tf]
    k = http_get("https://api.gateio.ws/api/v4/futures/usdt/candlesticks",
                 {"contract": sid, "interval": iv, "limit": min(N_CANDLES, 1000)})
    candles = [(float(x["o"]), float(x["h"]), float(x["l"]), float(x["c"]), int(x["t"]) * 1000, float(x.get("v") or 0)) for x in k]
    return candles, bool(candles) and candles[-1][4] + TF_MS[tf] > now_ms()


EXCHANGES = {
    "gatef": ("Gate.io (perpetual)", gatef_top, gatef_candles),
    "okx": ("OKX (perpetual)", okx_top, okx_candles),
    "bybit": ("Bybit (perpetual)", bybit_top, bybit_candles),
    "gate": ("Gate.io (spot)", gate_top, gate_candles),
    "kucoin": ("KuCoin (spot)", kucoin_top, kucoin_candles),
    "binance": ("Binance (spot)", binance_top, binance_candles),
}



def norm_base(b):
    """Gate/Binance kadang memakai 1000PEPE, 1MBABYDOGE dst; ambil nama dasarnya."""
    m = re.match(r"^(1000000|10000|1000|1M)(?=[A-Z])", b)
    return b[m.end():] if m else b


def fetch_mcap_ranks(n=TOP_N):
    """Return {SIMBOL: peringkat} untuk n koin teratas menurut market cap (CoinGecko), atau {} bila gagal."""
    last = None
    for attempt in range(3):
        try:
            r = requests.get("https://api.coingecko.com/api/v3/coins/markets",
                             params={"vs_currency": "usd", "order": "market_cap_desc",
                                     "per_page": 250, "page": 1, "sparkline": "false"},
                             timeout=20, headers={"User-Agent": "Mozilla/5.0"})
            r.raise_for_status()
            data = r.json()
            ranks = {}
            for pos, d in enumerate(data[:n], 1):
                sym = str(d.get("symbol", "")).upper()
                if sym and sym not in ranks:            # simbol kembar: pakai yang peringkatnya lebih tinggi
                    ranks[sym] = d.get("market_cap_rank") or pos
            return ranks
        except Exception as e:
            last = e
            time.sleep(6)
    print(f"gagal mengambil peringkat CoinGecko ({type(last).__name__})", end=" ")
    return {}


def connect(only=None, ranked=True, limit=True):
    keys = [only] if only else list(EXCHANGES)
    errors = []
    for key in keys:
        name, top_fn, candles_fn = EXCHANGES[key]
        print(f"Mencoba {name} ...", end=" ", flush=True)
        try:
            rows = top_fn()
            if not rows:
                raise RuntimeError("data kosong")
            rows.sort(key=lambda x: x[2], reverse=True)
            print("BERHASIL")
            if "(spot)" in name:
                print("\n*** PERHATIAN: sumber ini SPOT, bukan perpetual. Zona order block bisa sedikit berbeda dari chart .P ***")
            if not ranked:
                return name, candles_fn, (rows[:TOP_N] if limit else rows)
            ranks = fetch_mcap_ranks() if URUTAN == "mcap" else {}
            if URUTAN == "mcap" and not ranks:
                print("\n*** PERHATIAN: peringkat market cap tidak tersedia, memakai urutan VOLUME 24 jam ***")
            if ranks:
                chosen, matched = [], set()
                for row in rows:
                    base = row[1][:-4]
                    key = base if base in ranks else norm_base(base)
                    if key in ranks:
                        chosen.append((ranks[key], row))
                        matched.add(key)
                chosen.sort(key=lambda x: x[0])
                RANK.clear()
                for rk_, row in chosen:
                    RANK[row[1]] = rk_
                missing = [k for k, v in sorted(ranks.items(), key=lambda kv: kv[1]) if k not in matched]
                print(f"\nPeringkat market cap (CoinGecko) top {TOP_N}: {len(chosen)} koin punya kontrak di {name}, "
                      f"{len(missing)} tidak punya.")
                if missing:
                    print("Tidak dipindai (tidak ada kontraknya): " + ", ".join(missing[:80]) + (" ..." if len(missing) > 80 else ""))
                return name, candles_fn, [row for _, row in chosen]
            return name, candles_fn, rows[:TOP_N]
        except Exception as e:
            print("gagal")
            errors.append((name, f"{type(e).__name__}: {str(e)[:90]}"))
    print("\nSemua bursa gagal dihubungi dari jaringan ini:")
    for name, msg in errors:
        print(f"  - {name}: {msg}")
    print("\nCoba ganti jaringan (hotspot HP) atau nyalakan VPN, lalu jalankan lagi.")
    return None


def fmt_time(ts_ms):
    t = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc)
    w = t + timedelta(hours=7)
    return f"{t:%d-%b %H:%M} UTC / {w:%d-%b %H:%M} WIB"


def get_candles(candles_fn, sid, tf):
    """Return (candles, candle_terakhir_masih_berjalan)."""
    candles, last_open = candles_fn(sid, tf)
    if not PAKAI_CANDLE_BERJALAN and last_open:
        candles = candles[:-1]
        last_open = False
    return candles, last_open


def rk(label):
    return f"#{RANK[label]}" if label in RANK else ""




# ======================= PENGATURAN STRATEGI =======================
LIQ_LEN = 7              # Detection Length LuxAlgo
LIQ_MAR = 10 / 6.9       # Margin 6.9 -> band = ATR(10) / (10/6.9)
MAR_ZONA = 2.3           # Margin zona buyside & sellside
VIS_LIQ = 3              # level yang ditampilkan per sisi
MAX_ZZ = 50
PRESENT_BARS = 500       # mode Present: hanya 500 candle terakhir untuk mendeteksi level baru
SQZ_LEN = 20
SQZ_BB_MULT = 1.5
SQZ_KC_MULT = 1.5
BODY_MIN = 0.5           # minimal porsi body candle 1 di luar zona
SINYAL_BARU = 2          # candle 2 (entry) maksimal sekian candle lalu (terakhir = 1)
RR_MIN = 1.5
PIV = 5
ATR_LEN = 14
ARAH = "dua"
DROP = {}
BLN = ["Jan", "Feb", "Mar", "Apr", "Mei", "Jun", "Jul", "Agu", "Sep", "Okt", "Nov", "Des"]
RSI_LEN = 14
DIV_MIN = 5              # jarak minimal dasar/puncak 1 ke dasar/puncak 2 (candle)
DIV_MAX = 60             # jarak maksimal
DIV_PIV = 3              # pivot = lebih rendah/tinggi dari 3 candle kiri dan kanan
def ema_series(values, n):
    out = [None] * len(values)
    if len(values) < n:
        return out
    a = 2 / (n + 1)
    e = sum(values[:n]) / n
    out[n - 1] = e
    for i in range(n, len(values)):
        e = a * values[i] + (1 - a) * e
        out[i] = e
    return out


def atr_series(candles, n=ATR_LEN):
    m = len(candles)
    tr = [candles[0][1] - candles[0][2]] + [
        max(candles[i][1] - candles[i][2], abs(candles[i][1] - candles[i - 1][3]), abs(candles[i][2] - candles[i - 1][3]))
        for i in range(1, m)]
    out = [None] * m
    if m < n:
        return out
    out[n - 1] = sum(tr[:n]) / n
    for i in range(n, m):
        out[i] = (out[i - 1] * (n - 1) + tr[i]) / n
    return out




def rsi_series(values, n=14):
    """RSI Wilder (seed SMA), sama seperti ta.rsi TradingView."""
    m = len(values)
    out = [None] * m
    if m <= n:
        return out
    g = [max(values[i] - values[i - 1], 0.0) for i in range(1, m)]
    l = [max(values[i - 1] - values[i], 0.0) for i in range(1, m)]
    ag, al = sum(g[:n]) / n, sum(l[:n]) / n
    out[n] = 100.0 if al == 0 else 100 - 100 / (1 + ag / al)
    for i in range(n + 1, m):
        ag = (ag * (n - 1) + g[i - 1]) / n
        al = (al * (n - 1) + l[i - 1]) / n
        out[i] = 100.0 if al == 0 else 100 - 100 / (1 + ag / al)
    return out


def cari_divergence(candles, rsi, t0, c2, bull):
    """Harga: dasar/puncak ke-2 = low terendah / high tertinggi antara t0 dan c2; ke-1 = pivot (3 kiri/kanan) berjarak
    DIV_MIN..DIV_MAX candle sebelumnya. RSI dibandingkan pada LEMBAH/PUNCAK RSI di sekitar masing-masing titik
    (titik 1: +-DIV_PIV candle; titik 2: seluruh rentang t0..c2). Return dict atau None."""
    lo2 = max(t0, 0)
    idx = range(lo2, c2 + 1)
    if bull:
        j2 = min(idx, key=lambda k: (candles[k][2], -k))
        p2 = candles[j2][2]
    else:
        j2 = max(idx, key=lambda k: (candles[k][1], k))
        p2 = candles[j2][1]
    cand2 = [k for k in idx if rsi[k] is not None]
    if not cand2:
        return None
    i2 = (min if bull else max)(cand2, key=lambda k: (rsi[k], k))
    pv = DIV_PIV
    for k in range(j2 - DIV_MIN, max(j2 - DIV_MAX, pv) - 1, -1):
        if k - pv < 0 or k + pv >= len(candles):
            continue
        win = [d for d in range(k - pv, k + pv + 1) if rsi[d] is not None]
        if not win:
            continue
        i1 = (min if bull else max)(win, key=lambda d: (rsi[d], d))
        if bull:
            pk_ = candles[k][2]
            ok = all(pk_ <= candles[k + d][2] for d in range(-pv, pv + 1) if d)
            div = p2 < pk_ and rsi[i2] > rsi[i1]
        else:
            pk_ = candles[k][1]
            ok = all(pk_ >= candles[k + d][1] for d in range(-pv, pv + 1) if d)
            div = p2 > pk_ and rsi[i2] < rsi[i1]
        if ok and div:
            return {"j1": k, "j2": j2, "p1": pk_, "p2": p2, "r1": rsi[i1], "r2": rsi[i2], "i1": i1, "i2": i2}
    return None
def wib(ts_ms):
    t = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc) + timedelta(hours=7)
    return f"{t.day:02d}-{BLN[t.month - 1]} {t:%H:%M}"

def squeeze_series(candles):
    """Squeeze Momentum LazyBear per candle. Return (sqz_on, sqz_off, val), isi None bila belum ada data."""
    n = len(candles)
    L = SQZ_LEN
    highs = [c[1] for c in candles]
    lows = [c[2] for c in candles]
    closes = [c[3] for c in candles]
    sma = [None] * n
    sd = [None] * n
    for i in range(L - 1, n):
        w = closes[i - L + 1:i + 1]
        m = sum(w) / L
        sma[i] = m
        sd[i] = (sum((x - m) ** 2 for x in w) / L) ** 0.5            # simpangan baku populasi, seperti stdev Pine
    tr = [highs[0] - lows[0]] + [max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1]))
                                 for i in range(1, n)]
    sqz_on = [None] * n
    sqz_off = [None] * n
    for i in range(L - 1, n):
        dev = SQZ_BB_MULT * sd[i]
        ubb, lbb = sma[i] + dev, sma[i] - dev
        rma = sum(tr[i - L + 1:i + 1]) / L
        ukc, lkc = sma[i] + rma * SQZ_KC_MULT, sma[i] - rma * SQZ_KC_MULT
        sqz_on[i] = (lbb > lkc) and (ubb < ukc)
        sqz_off[i] = (lbb < lkc) and (ubb > ukc)
    x = [None] * n
    for i in range(L - 1, n):
        hh, ll = max(highs[i - L + 1:i + 1]), min(lows[i - L + 1:i + 1])
        x[i] = closes[i] - (((hh + ll) / 2) + sma[i]) / 2
    val = [None] * n
    mx = (L - 1) / 2
    sxx = sum((k - mx) ** 2 for k in range(L))
    for i in range(2 * L - 2, n):
        w = x[i - L + 1:i + 1]
        my = sum(w) / L
        slope = sum((k - mx) * (w[k] - my) for k in range(L)) / sxx
        val[i] = my + slope * (L - 1 - mx)
    return sqz_on, sqz_off, val




def target_swing(candles, bull, ref):
    """Swing high (BUY) / swing low (SELL) terdekat yang belum tertembus. Return harga atau None."""
    n = len(candles)
    H = [c[1] for c in candles]
    L = [c[2] for c in candles]
    cand = []
    for p in range(PIV, n - PIV):
        if bull and H[p] > ref and H[p] == max(H[p - PIV:p + PIV + 1]) and max(H[p + 1:]) <= H[p]:
            cand.append(H[p])
        if not bull and L[p] < ref and L[p] == min(L[p - PIV:p + PIV + 1]) and min(L[p + 1:]) >= L[p]:
            cand.append(L[p])
    if not cand:
        return None
    return min(cand) if bull else max(cand)


def lv_baru(left, top, bottom, y1, i, sisi):
    return {"dummy": False, "sisi": sisi, "left": left, "top": top, "bottom": bottom, "y1": y1, "dibuat": i,
            "brL": False, "brZ": False, "t0": None, "ztop": None, "zbot": None, "zend": None,
            "touched": False}


def simulasi(candles, diag_n=0):
    """Port Pine LuxAlgo Buyside & Sellside Liquidity + sinyal sweep. Return (sinyal, levels_B, levels_S, alasan_log)."""
    n = len(candles)
    O = [c[0] for c in candles]
    H = [c[1] for c in candles]
    L = [c[2] for c in candles]
    C = [c[3] for c in candles]
    atr = atr_series(candles, 10)
    sqz_on, sqz_off, _val = squeeze_series(candles)
    rsi = rsi_series(C, RSI_LEN)
    d = [0] * MAX_ZZ
    xs = [0] * MAX_ZZ
    ys = [None] * MAX_ZZ

    def in_out(dd, xx, yy):
        d.insert(0, dd)
        xs.insert(0, xx)
        ys.insert(0, yy)
        d.pop()
        xs.pop()
        ys.pop()

    dummy = {"dummy": True, "left": None, "brL": False}
    lev_B, lev_S = [dummy], [dummy]
    sinyal = []
    alasan_log = []

    def hitung(pivot, sisi, i, band):
        cnt, st_P, st_B, mx, mn = 0, 0.0, 0, 0.0, 10e6
        for k in range(MAX_ZZ):
            if sisi == "B":
                if d[k] == 1:
                    if ys[k] > pivot + band:
                        break
                    if pivot - band < ys[k] < pivot + band:
                        cnt += 1
                        st_B, st_P = xs[k], ys[k]
                        mx = max(mx, ys[k])
                        mn = min(mn, ys[k])
            else:
                if d[k] == -1:
                    if ys[k] < pivot - band:
                        break
                    if pivot - band < ys[k] < pivot + band:
                        cnt += 1
                        st_B, st_P = xs[k], ys[k]
                        mx = max(mx, ys[k])
                        mn = min(mn, ys[k])
        return cnt, st_P, st_B, mx, mn

    def tambah(levels, cnt, st_P, st_B, mx, mn, i, band, sisi):
        if cnt <= 2:
            return
        mid = (mx + mn) / 2
        top, bot = mid + band, mid - band
        g = levels[0]
        if g["left"] is not None and st_B == g["left"]:
            g["top"], g["bottom"] = top, bot
        else:
            levels.insert(0, lv_baru(st_B, top, bot, st_P, i, sisi))
            while len(levels) > VIS_LIQ:
                levels.pop()

    for i in range(n):
        if atr[i] is None or i < LIQ_LEN + 1:
            continue
        a = atr[i]
        band = a / LIQ_MAR
        per = (n - 1 - i) <= PRESENT_BARS
        c = i - 1
        ph = H[c] > max(H[c - LIQ_LEN:c]) and H[c] >= H[i]
        pl = L[c] < min(L[c - LIQ_LEN:c]) and L[c] <= L[i]
        if ph:
            if d[0] < 1:
                in_out(1, c, H[c])
            elif d[0] == 1 and H[c] > ys[0]:
                xs[0], ys[0] = c, H[c]
            if per:
                cnt, st_P, st_B, mx, mn = hitung(H[c], "B", i, band)
                tambah(lev_B, cnt, st_P, st_B, mx, mn, i, band, "B")
        if pl:
            if d[0] > -1:
                in_out(-1, c, L[c])
            elif d[0] == -1 and L[c] < ys[0]:
                xs[0], ys[0] = c, L[c]
            if per:
                cnt, st_P, st_B, mx, mn = hitung(L[c], "S", i, band)
                tambah(lev_S, cnt, st_P, st_B, mx, mn, i, band, "S")

        # --- breach & zona buyside
        for lv in lev_B:
            if lv["dummy"]:
                continue
            if not lv["brL"]:
                if H[i] > lv["top"]:
                    lv["brL"] = lv["brZ"] = True
                    lv["t0"] = i
                    lv["zbot"] = lv["y1"]
                    lv["ztop"] = min(lv["y1"] + MAR_ZONA * a, H[i])
            elif lv["brZ"]:
                if L[i] > lv["y1"] - MAR_ZONA * a and H[i] < lv["y1"] + MAR_ZONA * a:
                    lv["ztop"] = max(H[i], lv["ztop"])
                else:
                    lv["brZ"] = False
                    lv["zend"] = i
        # --- breach & zona sellside
        for lv in lev_S:
            if lv["dummy"]:
                continue
            if not lv["brL"]:
                if L[i] < lv["bottom"]:
                    lv["brL"] = lv["brZ"] = True
                    lv["t0"] = i
                    lv["ztop"] = lv["y1"]
                    lv["zbot"] = max(lv["y1"] - MAR_ZONA * a, L[i])
            elif lv["brZ"]:
                if L[i] > lv["y1"] - MAR_ZONA * a and H[i] < lv["y1"] + MAR_ZONA * a:
                    lv["zbot"] = min(L[i], lv["zbot"])
                else:
                    lv["brZ"] = False
                    lv["zend"] = i

        # --- sinyal: candle 1 = i-1, candle 2 = i (entry). S = zona merah -> BUY, B = zona hijau -> SELL
        if i >= n - max(SINYAL_BARU, diag_n) and i >= 2:
            c1, c2 = i - 1, i
            for sisi, levels in (("S", lev_S), ("B", lev_B)):
                bull = sisi == "S"
                if ARAH == "buy" and not bull or ARAH == "sell" and bull:
                    continue
                for lv in levels:
                    if lv["dummy"] or not lv["brL"] or lv["t0"] >= c1:
                        continue
                    if lv["zend"] is not None and c1 > lv["zend"]:      # candle 1 harus di dalam kotak (batas kanan = zend)
                        continue
                    tahap = 0
                    alasan = None
                    garis = lv["y1"]                                   # batas atas kotak merah / batas bawah kotak hijau
                    bl, bh = min(O[c1], C[c1]), max(O[c1], C[c1])
                    if not (sqz_off[c1] and sqz_off[c2]):
                        alasan = "dot squeeze tidak putih di candle 1 dan 2"
                    elif bull and not (C[c1] > garis and L[c1] <= garis) or (not bull) and not (C[c1] < garis and H[c1] >= garis):
                        alasan = "candle 1 tidak masuk area zona / tidak menutup di luar zona"
                        tahap = 1
                    else:
                        if bh > bl:
                            persen = (bh - max(bl, garis)) / (bh - bl) if bull else (min(bh, garis) - bl) / (bh - bl)
                        else:
                            persen = 0.0
                        if persen < BODY_MIN:
                            alasan = f"body candle 1 di luar zona kurang dari {BODY_MIN * 100:.0f}%"
                            tahap = 2
                        elif not ((C[c2] > garis) if bull else (C[c2] < garis)):
                            alasan = "candle 2 tidak menutup di luar zona"
                            tahap = 3
                        else:
                            dv = cari_divergence(candles, rsi, lv["t0"], c2, bull)
                            if dv is None:
                                alasan = "tidak ada " + ("bullish" if bull else "bearish") + " divergence (harga vs RSI14)"
                                tahap = 3
                        if alasan is None:
                            entry = C[c2]
                            stop = lv["zbot"] if bull else lv["ztop"]
                            risk = abs(entry - stop)
                            kand = []
                            if bull:
                                kand += [(x["y1"], "buyside liquidity") for x in lev_B if not x["dummy"] and not x["brL"] and x["y1"] > entry]
                            else:
                                kand += [(x["y1"], "sellside liquidity") for x in lev_S if not x["dummy"] and not x["brL"] and x["y1"] < entry]
                            sw = target_swing(candles, bull, entry)
                            if sw is not None:
                                kand.append((sw, "swing"))
                            if risk <= 0 or not kand:
                                alasan = "tidak ada target / risiko nol"
                                tahap = 4
                            else:
                                tgt, jenis = min(kand, key=lambda z: abs(z[0] - entry))
                                rr = abs(tgt - entry) / risk
                                if rr < RR_MIN:
                                    alasan = f"RR {rr:.2f} di bawah minimal"
                                    tahap = 4
                                else:
                                    px = C[-1]
                                    if i >= n - SINYAL_BARU:
                                        sinyal.append({"arah": "BUY" if bull else "SELL", "i": i, "level": garis, "z_top": lv["ztop"], "z_bot": lv["zbot"],
                                                       "t_zona": wib(candles[lv["t0"]][4]), "umur_zona": n - lv["t0"], "t_c1": wib(candles[c1][4]), "persen": persen,
                                                       "umur_sinyal": n - i, "t_sinyal": wib(candles[c2][4]), "tutup": entry, "stop": stop, "risk": risk / entry * 100,
                                                       "target": tgt, "jenis": jenis, "gap_pct": abs(tgt - entry) / entry * 100, "rr": rr,
                                                       "px": px, "jarak_px_pct": (px - entry) / entry * 100, "zona_key": f"{sisi}{candles[lv['t0']][4]}",
                                                       "div_t1": wib(candles[dv["j1"]][4]), "div_t2": wib(candles[dv["j2"]][4]), "div_p1": dv["p1"], "div_p2": dv["p2"], "div_r1": dv["r1"], "div_r2": dv["r2"],
                                                       "div_tr1": wib(candles[dv["i1"]][4]), "div_tr2": wib(candles[dv["i2"]][4])})
                                    continue
                    if alasan:
                        alasan_log.append((i, bull, garis, tahap, alasan))
    return sinyal, lev_B, lev_S, alasan_log


def scan_candles(candles):
    if len(candles) < 80:
        return []
    return simulasi(candles)[0]


def ringkas(candles, recent=0):
    """Mode cek: level, zona, dan alasan kandidat gugur."""
    n = len(candles)
    sinyal, lb, ls, alasan = simulasi(candles, diag_n=max(recent, SINYAL_BARU))
    for nama, levels in (("BUYSIDE (hijau)", lb), ("SELLSIDE (merah)", ls)):
        print(f"  {nama}:")
        ada = False
        for lv in levels:
            if lv["dummy"]:
                continue
            ada = True
            if not lv["brL"]:
                print(f"    level {lv['y1']:.6g} (dibuat {wib(candles[lv['dibuat']][4])}) - BELUM diambil")
            else:
                st = "zona MASIH berjalan" if lv["zend"] is None else f"zona selesai {wib(candles[lv['zend']][4])}"
                print(f"    level {lv['y1']:.6g} diambil {wib(candles[lv['t0']][4])} ({n - lv['t0']} candle lalu) | zona {lv['zbot']:.6g} - {lv['ztop']:.6g} | {st}")
        if not ada:
            print("    (tidak ada)")
    if recent:
        print(f"  Kandidat yang gugur pada {recent} candle terakhir (candle 2 = candle ke-i):")
        for i, bull, garis, tahap, a in alasan[-30:]:
            if i >= n - recent:
                print(f"    {wib(candles[i][4])} {'BUY ' if bull else 'SELL'} garis {garis:.6g}: {a}")
    print(f"  Sinyal pada {SINYAL_BARU} candle terakhir: {len(sinyal)}")
    for r in sinyal:
        print("   ", line("", r))


def fmt(x):
    return "n/a" if x is None else f"{x:.6g}"


def line(label, r):
    return (f"  {label:<14}{rk(label):>5}  {r['arah']:<4} zona {fmt(r['z_bot'])}-{fmt(r['z_top'])} (terbentuk {r['t_zona']}) | candle1 {r['t_c1']} (body di luar zona {r['persen'] * 100:.0f}%) | "
            f"entry {r['umur_sinyal']} candle lalu ({r['t_sinyal']}), tutup {fmt(r['tutup'])} | stop {fmt(r['stop'])} (risiko {r['risk']:.2f}%) | "
            f"target {fmt(r['target'])} [{r['jenis']}] (jarak {r['gap_pct']:.1f}%, R:R {r['rr']:.2f}) | divergence harga {fmt(r['div_p1'])} ({r['div_t1']}) -> {fmt(r['div_p2'])} ({r['div_t2']}); RSI {r['div_r1']:.1f} ({r['div_tr1']}) -> {r['div_r2']:.1f} ({r['div_tr2']})")


def main():
    global SINYAL_BARU, RR_MIN, ARAH, PAKAI_CANDLE_BERJALAN
    args = sys.argv[1:]
    low = [a.lower() for a in args]
    cek = bool(low) and low[0] in ("cek", "check")
    recent = 0
    for a in low:
        try:
            if a.startswith("baru="):
                SINYAL_BARU = int(a[5:])
            elif a.startswith("rr="):
                RR_MIN = float(a[3:])
            elif a.startswith("recent="):
                recent = int(a[7:])
        except ValueError:
            pass
    ARAH = "buy" if "buy" in low else ("sell" if "sell" in low else "dua")
    only = next((a for a in low if a in EXCHANGES), None)
    PAKAI_CANDLE_BERJALAN = "live" in low
    tfs = [a for a in args if a in ("5", "15", "60", "240")] or ["15", "60", "240"]
    if cek and len(args) < 2:
        print("Pakai: python st_liq_sweep.py cek NAMAKOIN 60 recent=3")
        return
    conn = connect(only, ranked=not cek, limit=not cek)
    if conn is None:
        sys.exit(1)
    ex_name, fn, syms = conn
    if cek:
        q = args[1].upper().replace("USDT", "").replace("_", "")
        syms = [s for s in syms if s[1].upper().replace("USDT", "").replace("_", "") == q]
        if not syms:
            print("Koin tidak ditemukan.")
            return
    print(f"\nScan {len(syms)} koin di {ex_name} | TF {', '.join(TF_NAMES[t] for t in tfs)} | Liquidity Sweep (LuxAlgo) + dot putih Squeeze + divergence RSI14 | "
          f"RR min {RR_MIN:g} | entry maks {SINYAL_BARU} candle lalu | candle tertutup | arah {ARAH}\n")
    rows = []
    for idx, (sid, label, _, _) in enumerate(syms, 1):
        for tf in tfs:
            try:
                candles, _ = get_candles(fn, sid, tf)
                if cek:
                    print(f"\n--- {label} {TF_NAMES[tf]}: {len(candles)} candle ---")
                    ringkas(candles, recent)
                    continue
                for r in scan_candles(candles):
                    rows.append((TF_NAMES[tf], label, r))
            except Exception:
                pass
            time.sleep(0.05)
        print(f"\r  progress {idx}/{len(syms)}", end="", flush=True)
    print("\n")
    if cek:
        return
    for tf in tfs:
        T = TF_NAMES[tf]
        print("#" * 78)
        print(f"TIMEFRAME {T}")
        print("#" * 78)
        for arah in ("SELL", "BUY"):
            if ARAH == "buy" and arah == "SELL" or ARAH == "sell" and arah == "BUY":
                continue
            sub = sorted([x for x in rows if x[0] == T and x[2]["arah"] == arah], key=lambda x: (x[2]["umur_sinyal"], -x[2]["rr"]))
            print(f"[{T}] {arah} ({len(sub)})")
            for _, label, r in sub:
                print(line(label, r))
            if not sub:
                print("  - tidak ada")
            print()
    fname = f"st_liq_sweep_scan_{datetime.now():%Y%m%d_%H%M}.csv"
    with open(fname, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["timeframe", "symbol", "peringkat_mcap", "arah", "level_zona", "zona_bawah", "zona_atas", "waktu_zona_WIB", "waktu_candle1_WIB", "body_di_luar_zona_pct",
                    "umur_sinyal_candle", "waktu_candle2_WIB", "entry_tutup", "stop", "risiko_stop_pct", "target", "jenis_target", "jarak_ke_target_pct", "RR",
                    "harga_sekarang", "jarak_harga_dari_entry_pct", "div_waktu_1", "div_harga_1", "div_RSI_1", "div_waktu_RSI_1", "div_waktu_2", "div_harga_2", "div_RSI_2", "div_waktu_RSI_2"])
        for T, label, r in sorted(rows, key=lambda x: (x[0], x[2]["umur_sinyal"], -x[2]["rr"])):
            w.writerow([T, label, RANK.get(label, ""), r["arah"], f"{r['level']:.8g}", f"{r['z_bot']:.8g}", f"{r['z_top']:.8g}", r["t_zona"], r["t_c1"], round(r["persen"] * 100),
                        r["umur_sinyal"], r["t_sinyal"], f"{r['tutup']:.8g}", f"{r['stop']:.8g}", round(r["risk"], 2), f"{r['target']:.8g}", r["jenis"],
                        round(r["gap_pct"], 2), round(r["rr"], 2), f"{r['px']:.8g}", round(r["jarak_px_pct"], 2),
                        r["div_t1"], f"{r['div_p1']:.8g}", round(r["div_r1"], 1), r["div_tr1"], r["div_t2"], f"{r['div_p2']:.8g}", round(r["div_r2"], 1), r["div_tr2"]])
    print(f"Sumber data: {ex_name}\nTersimpan: {fname}")


if __name__ == "__main__":
    main()
