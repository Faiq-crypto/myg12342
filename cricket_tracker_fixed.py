#!/usr/bin/env python3
"""
╔══════════════════════════════════════════════════════════════╗
║   CRICKET ARBITRAGE TRACKER  —  MASTER EDITION              ║
║   Real-time prices · Whale detector · Activity monitor      ║
╠══════════════════════════════════════════════════════════════╣
║  Polymarket : Live via CLOB REST API (condition IDs)        ║
║  Yoso       : Network-intercepted XHR/WS (no page reloads) ║
║                                                              ║
║  ALERTS                                                      ║
║   • Arbitrage  — combo <= threshold ($0.95)                 ║
║   • Whale      — single Yoso buy > $2 detected              ║
║   • No-price   — 5 min without valid data                   ║
║   • Gap notice — combined gap from $1.00 >= $0.05           ║
╚══════════════════════════════════════════════════════════════╝
"""

# ── stdlib ─────────────────────────────────────────────────────────────────────
import time, json, re, logging, os, sys, threading, queue
from datetime import datetime
from urllib.parse import urlparse, urljoin
from collections import deque

# Silence noisy libs before any import
os.environ["WDM_LOG"]              = "0"
os.environ["WDM_PRINT_FIRST_LINE"] = "False"
logging.getLogger("WDM").setLevel(logging.CRITICAL)
logging.getLogger("selenium").setLevel(logging.CRITICAL)
logging.getLogger("urllib3").setLevel(logging.CRITICAL)

import requests

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from webdriver_manager.chrome import ChromeDriverManager

# ══════════════════════════════════════════════════════════════════════════════
#  CONFIG
# ══════════════════════════════════════════════════════════════════════════════
TELEGRAM_BOT_TOKEN    = "8698028991:AAHRq2T8v7Pip1jF8ghVeds5cIgD9Qvn3fs"
TELEGRAM_CHAT_ID      = "7900149909"

ALERT_THRESHOLD       = 0.95   # arbitrage trigger
GAP_NOTIFY_THRESHOLD  = 0.05   # gap from $1.00 worth notifying
WHALE_THRESHOLD       = 2.00   # single Yoso trade > $2 → whale alert

POLY_REFRESH_SEC      = 3      # Poly REST poll interval
YOSO_REFRESH_SEC      = 5      # Yoso interaction interval (2s per team + overhead)
ALERT_COOLDOWN        = 60     # seconds between repeated arb alerts
WHALE_COOLDOWN        = 30     # seconds between repeated whale alerts
GAP_COOLDOWN          = 120    # seconds between gap notifications

# ── Smart arbitrage alert config ─────────────────────────────────────────────
# A "band" = a $0.01-wide bucket e.g. $0.94xx or $0.93xx
# When arb stays in same band:  max 3 alerts, spaced MIN_ALERT_SPACING apart
# When arb moves to a new band: immediately alert + reset counter for new band
ARB_BAND_SIZE         = 0.01   # round total to nearest 0.01 to define "same arb"
ARB_MAX_ALERTS        = 3      # max alerts per band before going silent
MIN_ALERT_SPACING     = 60     # minimum seconds between any two arb alerts
BAND_MOVE_NOTIFY      = True   # send telegram when arb moves to a different band
NO_PRICE_TIMEOUT      = 300    # 5 min → "still trying" telegram
NO_ARB_HEARTBEAT      = 300    # 5 min → "still hunting" heartbeat when no arb
BROWSER_TIMEOUT       = 20     # selenium page load timeout
BROWSER_RESTART_EVERY = 600   # restart Yoso browser every N fetches

GAMMA_API   = "https://gamma-api.polymarket.com"
CLOB_API    = "https://clob.polymarket.com"

# ── Terminal colours (ANSI) ───────────────────────────────────────────────────
# New Zealand = white  |  South Africa = green
CLR_WI    = "[91m"    # bright red (NZ)
CLR_SL    = "[96m"    # sky blue (SA)
CLR_RST   = "[0m"     # reset
CLR_BOLD  = "[1m"
CLR_YLW   = "[93m"    # yellow  (whale)
CLR_GRN   = "[92m"    # green   (arb alert)
CLR_POLY  = "[94m"    # bright blue (Poly label)
CLR_YOSO  = "[93m"    # yellow (Yoso label)

# Runtime flag — set during startup
WHALE_ALERTS_ENABLED: bool = True

# ══════════════════════════════════════════════════════════════════════════════
#  CONFIG — Custom market only (no preset)
# ══════════════════════════════════════════════════════════════════════════════

# ══════════════════════════════════════════════════════════════════════════════
#  TELEGRAM
# ══════════════════════════════════════════════════════════════════════════════

_tg_queue: queue.Queue = queue.Queue()

def _tg_worker():
    """Background thread: sends Telegram messages one at a time."""
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    while True:
        msg = _tg_queue.get()
        if msg is None:
            break
        for attempt in range(3):
            try:
                r = requests.post(url, json={
                    "chat_id": TELEGRAM_CHAT_ID,
                    "text": msg,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": True,
                }, timeout=15)
                if r.status_code == 200:
                    print("  [TG✓]", flush=True)
                    break
                elif r.status_code == 429:
                    retry_after = int(r.json().get("parameters", {}).get("retry_after", 5))
                    time.sleep(retry_after)
                else:
                    print(f"  [TG✗] {r.status_code}: {r.text[:80]}", flush=True)
                    break
            except Exception as e:
                if attempt == 2:
                    print(f"  [TG✗] {e}", flush=True)
                time.sleep(2)
        _tg_queue.task_done()

_tg_thread = threading.Thread(target=_tg_worker, daemon=True)
_tg_thread.start()

def tg(msg: str):
    _tg_queue.put(msg)

def tg_arb(cfg, label, n1, v1, n2, v2, total):
    profit = cfg["threshold"] - total
    gap    = round(1.00 - total, 4)
    now    = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    tg(
        f"🚨 <b>ARBITRAGE ALERT!</b>\n\n"
        f"🏏 <b>{cfg['name']}</b>\n"
        f"📊 Combo: <b>{label}</b>\n\n"
        f"💰 {n1}: <b>${v1:.4f}</b>\n"
        f"💰 {n2}: <b>${v2:.4f}</b>\n"
        f"➕ Combined: <b>${total:.4f}</b>\n"
        f"📐 Gap from $1.00: <b>${gap:.4f}</b>\n"
        f"💵 Profit/dollar: <b>${profit:.4f}</b>\n\n"
        f"🔗 <a href='{cfg['poly_url']}'>Polymarket</a>  |  "
        f"<a href='{cfg['yoso_url']}'>Yoso</a>\n"
        f"🕐 {now}"
    )

def tg_arb_move(cfg, label, old_gap, new_gap, total, direction):
    """Alert when arbitrage opportunity moves to a different size band."""
    now    = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    arrow  = "📈 IMPROVED" if new_gap > old_gap else "📉 REDUCED"
    profit = cfg["threshold"] - total
    tg(
        f"🔄 <b>ARB MOVED \u2014 {arrow}</b>\n\n"
        f"🏏 <b>{cfg['name']}</b>\n"
        f"📊 Combo: <b>{label}</b>\n\n"
        f"📐 Gap before: <b>${old_gap:.4f}</b>\n"
        f"📐 Gap now:    <b>${new_gap:.4f}</b>\n"
        f"➕ Combined:   <b>${total:.4f}</b>\n"
        f"💵 Profit/dollar: <b>${profit:.4f}</b>\n\n"
        f"🔗 <a href='{cfg['poly_url']}'>Polymarket</a>  |  "
        f"<a href='{cfg['yoso_url']}'>Yoso</a>\n"
        f"🕐 {now}"
    )
def tg_arb_silenced(cfg, label, total, gap, alert_num):
    """Notify that max alerts reached — going silent until band moves."""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    tg(
        f"🔕 <b>ARB SILENCED (alert #{alert_num}/{ARB_MAX_ALERTS})</b>\n\n"
        f"🏏 <b>{cfg['name']}</b>\n"
        f"📊 <b>{label}</b>\n\n"
        f"➕ Combined: <b>${total:.4f}</b>  gap=<b>${gap:.4f}</b>\n"
        f"⚠️ Same opportunity still active — no more alerts until it moves.\n"
        f"Will notify again if gap changes.\n\n"
        f"🕐 {now}"
    )
def tg_whale(cfg, team, amount, price, side):
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    tg(
        f"🐋 <b>WHALE ALERT — {cfg['name']}</b>\n\n"
        f"💸 Someone bought <b>${amount:.2f}</b> of <b>{team}</b> ({side})\n"
        f"📈 Price: <b>${price:.4f}</b>\n"
        f"⚠️ Low liquidity — price may move fast!\n\n"
        f"🔗 <a href='{cfg['yoso_url']}'>Yoso Market</a>\n"
        f"🕐 {now}"
    )

def tg_gap(cfg, label, n1, v1, n2, v2, total, gap):
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    tg(
        f"📉 <b>Gap Notice — {cfg['name']}</b>\n\n"
        f"📊 <b>{label}</b>\n"
        f"💰 {n1}: <b>${v1:.4f}</b>\n"
        f"💰 {n2}: <b>${v2:.4f}</b>\n"
        f"➕ Combined: <b>${total:.4f}</b>\n"
        f"📐 Gap from $1.00: <b>${gap:.4f}</b>\n\n"
        f"🕐 {now}"
    )

# ══════════════════════════════════════════════════════════════════════════════
#  POLYMARKET — CLOB REST + Gamma API
#  Uses condition IDs for precise price lookup (no slug ambiguity)
# ══════════════════════════════════════════════════════════════════════════════

def _parse_outcomes(markets: list, t1k: list, t2k: list) -> dict:
    prices = {}
    for m in markets:
        outcomes = m.get("outcomes", [])
        oprices  = m.get("outcomePrices", [])
        if isinstance(outcomes, str):
            try:    outcomes = json.loads(outcomes)
            except: continue
        if isinstance(oprices, str):
            try:    oprices = json.loads(oprices)
            except: continue
        for i, o in enumerate(outcomes):
            ol = o.lower() if isinstance(o, str) else ""
            try:    p = round(float(oprices[i]), 4)
            except: continue
            if any(k in ol for k in t1k):
                prices["team1"] = p
            elif any(k in ol for k in t2k):
                prices["team2"] = p
    return prices


def _gamma_flat(endpoint: str, params: dict) -> list:
    try:
        r = requests.get(endpoint, params=params, timeout=12)
        if r.status_code != 200:
            return []
        data  = r.json()
        items = data if isinstance(data, list) else []
        if isinstance(data, dict):
            items = data.get("markets") or data.get("events") or [data]
        flat = []
        for item in items:
            if isinstance(item, dict) and "markets" in item:
                flat.extend(item["markets"])
            elif isinstance(item, dict):
                flat.append(item)
        return flat
    except Exception:
        return []


def resolve_condition_ids(cfg: dict) -> list[str]:
    """
    Resolve Polymarket condition IDs for CLOB price lookups.
    Returns list of conditionId strings, or [] on failure.
    """
    slug  = cfg.get("poly_slug", "")
    t1k   = cfg["team1_keys"]
    t2k   = cfg["team2_keys"]
    cids  = []
    for ep, params in [
        (f"{GAMMA_API}/markets", {"slug": slug}),
        (f"{GAMMA_API}/events",  {"slug": slug}),
    ]:
        for m in _gamma_flat(ep, params):
            cid = m.get("conditionId") or m.get("condition_id")
            if cid:
                outcomes = m.get("outcomes", [])
                if isinstance(outcomes, str):
                    try: outcomes = json.loads(outcomes)
                    except: outcomes = []
                for o in outcomes:
                    ol = o.lower() if isinstance(o, str) else ""
                    if any(k in ol for k in t1k + t2k):
                        if cid not in cids:
                            cids.append(cid)
        if cids:
            break
    return cids


def _poly_get(url, params=None, timeout=10, silent_404=False):
    """Requests wrapper with error printing for Poly API calls."""
    try:
        r = requests.get(url, params=params, timeout=timeout)
        if r.status_code == 200:
            return r.json()
        if r.status_code == 404 and silent_404:
            return None
        print(f"  [Poly] HTTP {r.status_code} → {url}  params={params}", flush=True)
    except Exception as e:
        print(f"  [Poly] Request failed: {str(e)[:80]}", flush=True)
    return None


def clob_mid_price(token_id: str) -> float | None:
    """
    Fetch the true real-time mid price for a single token from Polymarket CLOB.
    Tries all known endpoint formats in order.
    """
    # All known Polymarket CLOB endpoint formats for price lookup
    attempts = [
        # Current documented endpoints (query param style)
        lambda: _poly_get(f"{CLOB_API}/midpoint",       {"token_id": token_id}, silent_404=True),
        lambda: _poly_get(f"{CLOB_API}/book",            {"token_id": token_id}, silent_404=True),
        lambda: _poly_get(f"{CLOB_API}/price",           {"token_id": token_id, "side": "buy"}, silent_404=True),
        lambda: _poly_get(f"{CLOB_API}/last-trade-price",{"token_id": token_id}, silent_404=True),
        # Plural endpoints (some versions use these)
        lambda: _poly_get(f"{CLOB_API}/midpoints",      {"token_ids": f"[{token_id}]"}, silent_404=True),
        lambda: _poly_get(f"{CLOB_API}/prices",          {"token_ids": f"[{token_id}]"}, silent_404=True),
    ]

    for attempt in attempts:
        try:
            data = attempt()
            if not data:
                continue

            # {"mid": "0.59"} or {"mid": 0.59}
            if "mid" in data:
                p = round(float(data["mid"]), 4)
                if 0.01 <= p <= 0.99:
                    return p

            # {"price": "0.59"}
            if "price" in data:
                p = round(float(data["price"]), 4)
                if 0.01 <= p <= 0.99:
                    return p

            # {"<token_id>": "0.59"}  (bulk response)
            if token_id in data:
                p = round(float(data[token_id]), 4)
                if 0.01 <= p <= 0.99:
                    return p

            # {"bids": [...], "asks": [...]}
            bids = data.get("bids", [])
            asks = data.get("asks", [])
            if bids or asks:
                best_bid = max((float(b["price"]) for b in bids if b.get("price")), default=None)
                best_ask = min((float(a["price"]) for a in asks if a.get("price")), default=None)
                if best_bid is not None and best_ask is not None:
                    return round((best_bid + best_ask) / 2, 4)
                if best_ask is not None:
                    return round(best_ask, 4)
                if best_bid is not None:
                    return round(best_bid, 4)

        except Exception:
            continue

    return None


def clob_bulk_prices(token_ids: list[str]) -> dict:
    """
    Fetch prices for multiple tokens at once using CLOB bulk endpoints.
    Returns {token_id: price} dict.
    """
    if not token_ids:
        return {}

    result = {}

    # Try POST /prices-history or GET /prices with JSON body
    # Documented Polymarket bulk price endpoint
    try:
        r = requests.post(
            f"{CLOB_API}/prices",
            json={"token_ids": token_ids},
            timeout=10
        )
        if r.status_code == 200:
            data = r.json()
            for tid in token_ids:
                if tid in data:
                    try:
                        p = round(float(data[tid]), 4)
                        if 0.01 <= p <= 0.99:
                            result[tid] = p
                    except Exception:
                        pass
            if result:
                return result
    except Exception:
        pass

    # Try GET /midpoints with token_ids array
    try:
        ids_param = ",".join(token_ids)
        r = requests.get(
            f"{CLOB_API}/midpoints",
            params={"token_ids": ids_param},
            timeout=10
        )
        if r.status_code == 200:
            data = r.json()
            for tid in token_ids:
                if tid in data:
                    try:
                        p = round(float(data[tid]), 4)
                        if 0.01 <= p <= 0.99:
                            result[tid] = p
                    except Exception:
                        pass
            if result:
                return result
    except Exception:
        pass

    return result




def fetch_poly_prices_direct(cfg: dict) -> dict | None:
    """
    Main Polymarket price fetcher. Strategy:
      1. Gamma API → get token IDs for each outcome
      2. CLOB /orderbook/{token_id} → real-time mid price per token
      3. Fallback: outcomePrices from Gamma if CLOB fails
    """
    slug  = cfg.get("poly_slug", "")
    t1k   = cfg["team1_keys"]
    t2k   = cfg["team2_keys"]

    # ── Step 1: Get market data + token IDs from Gamma ───────────────────────
    markets_raw = []
    for ep, params in [
        (f"{GAMMA_API}/markets", {"slug": slug}),
        (f"{GAMMA_API}/events",  {"slug": slug}),
    ]:
        result = _poly_get(ep, params=params)
        if not result:
            continue
        items = result if isinstance(result, list) else []
        if isinstance(result, dict):
            items = result.get("markets") or result.get("events") or [result]
        for item in items:
            if isinstance(item, dict) and "markets" in item:
                markets_raw.extend(item["markets"])
            elif isinstance(item, dict):
                markets_raw.append(item)
        if markets_raw:
            break

    if not markets_raw:
        print("  [Poly] No markets found from Gamma API", flush=True)
        return None

    # ── Step 2: Build token_id → team mapping ────────────────────────────────
    # Polymarket stores token IDs in clobTokenIds (list matching outcomes order)
    token_map = {}   # token_id → "team1" | "team2"

    for m in markets_raw:
        outcomes = m.get("outcomes", [])
        if isinstance(outcomes, str):
            try:    outcomes = json.loads(outcomes)
            except: outcomes = []

        # clobTokenIds is the correct field for per-outcome token IDs
        ctids = m.get("clobTokenIds") or m.get("clob_token_ids") or []
        if isinstance(ctids, str):
            try:    ctids = json.loads(ctids)
            except: ctids = []

        # Also try tokens[] array from CLOB market endpoint
        if not ctids:
            cid = m.get("conditionId") or m.get("condition_id", "")
            if cid:
                clob_m = _poly_get(f"{CLOB_API}/markets/{cid}", timeout=8)
                if clob_m:
                    toks = clob_m.get("tokens", [])
                    ctids = [t.get("token_id", "") for t in toks]
                    # Align outcomes from CLOB market if missing
                    if not outcomes:
                        outcomes = [t.get("outcome", "") for t in toks]

        for i, tid in enumerate(ctids):
            if not tid:
                continue
            ol = outcomes[i].lower() if i < len(outcomes) and isinstance(outcomes[i], str) else ""
            if any(k in ol for k in t1k):
                token_map[tid] = "team1"
            elif any(k in ol for k in t2k):
                token_map[tid] = "team2"

        if len(token_map) >= 2:
            break

    if not token_map:
        print("  [Poly] Could not map token IDs to teams", flush=True)
        # Last resort: fall back to outcomePrices from Gamma
        prices = {}
        for m in markets_raw:
            outcomes = m.get("outcomes", [])
            oprices  = m.get("outcomePrices", [])
            if isinstance(outcomes, str):
                try: outcomes = json.loads(outcomes)
                except: outcomes = []
            if isinstance(oprices, str):
                try: oprices = json.loads(oprices)
                except: oprices = []
            for i, o in enumerate(outcomes):
                ol = o.lower() if isinstance(o, str) else ""
                try: p = round(float(oprices[i]), 4)
                except: continue
                if any(k in ol for k in t1k) and "team1" not in prices:
                    prices["team1"] = p
                elif any(k in ol for k in t2k) and "team2" not in prices:
                    prices["team2"] = p
            if len(prices) == 2:
                break
        return prices if prices else None

    # ── Step 3: Fetch real-time prices ───────────────────────────────────────
    prices = {}
    token_ids_list = list(token_map.keys())

    # Try bulk fetch first (faster, one round trip)
    bulk = clob_bulk_prices(token_ids_list)
    for tid, p in bulk.items():
        team_key = token_map.get(tid)
        if team_key and team_key not in prices:
            prices[team_key] = p

    # Fill any missing with per-token fetch
    for tid, team_key in token_map.items():
        if team_key in prices:
            continue
        p = clob_mid_price(tid)
        if p is not None:
            prices[team_key] = p

    if len(prices) == 2:
        return prices

    if prices:
        print(f"  [Poly] Partial CLOB prices: {prices}", flush=True)
    else:
        print("  [Poly] All CLOB endpoints failed — using Gamma outcomePrices", flush=True)

    # ── Gamma outcomePrices fallback ─────────────────────────────────────────
    for m in markets_raw:
        outcomes = m.get("outcomes", [])
        oprices  = m.get("outcomePrices", [])
        if isinstance(outcomes, str):
            try: outcomes = json.loads(outcomes)
            except: outcomes = []
        if isinstance(oprices, str):
            try: oprices = json.loads(oprices)
            except: oprices = []
        for i, o in enumerate(outcomes):
            ol = o.lower() if isinstance(o, str) else ""
            try: p = round(float(oprices[i]), 4)
            except: continue
            if any(k in ol for k in t1k) and "team1" not in prices:
                prices["team1"] = p
            elif any(k in ol for k in t2k) and "team2" not in prices:
                prices["team2"] = p
        if len(prices) == 2:
            break

    return prices if prices else None



class PolyPriceFetcher:
    """
    Fetches real-time Polymarket prices via CLOB orderbook mid prices.
    Token IDs are resolved on every fetch via Gamma API, then orderbook
    is queried for live bid/ask mid.
    """
    def __init__(self, cfg: dict):
        self.cfg = cfg
        print("  [Poly] Initialising price fetcher (CLOB orderbook mid)...", flush=True)
        # Pre-discover tokens so first fetch is fast
        self._token_map: dict = {}
        self._discover_tokens()

    def _discover_tokens(self):
        """Resolve token_id → team key mapping once. Cached for speed."""
        slug = self.cfg.get("poly_slug", "")
        t1k  = self.cfg["team1_keys"]
        t2k  = self.cfg["team2_keys"]
        markets_raw = []
        for ep, params in [
            (f"{GAMMA_API}/markets", {"slug": slug}),
            (f"{GAMMA_API}/events",  {"slug": slug}),
        ]:
            result = _poly_get(ep, params=params)
            if not result:
                continue
            items = result if isinstance(result, list) else []
            if isinstance(result, dict):
                items = result.get("markets") or result.get("events") or [result]
            for item in items:
                if isinstance(item, dict) and "markets" in item:
                    markets_raw.extend(item["markets"])
                elif isinstance(item, dict):
                    markets_raw.append(item)
            if markets_raw:
                break

        for m in markets_raw:
            outcomes = m.get("outcomes", [])
            if isinstance(outcomes, str):
                try:    outcomes = json.loads(outcomes)
                except: outcomes = []
            ctids = m.get("clobTokenIds") or m.get("clob_token_ids") or []
            if isinstance(ctids, str):
                try:    ctids = json.loads(ctids)
                except: ctids = []
            if not ctids:
                cid = m.get("conditionId") or m.get("condition_id", "")
                if cid:
                    clob_m = _poly_get(f"{CLOB_API}/markets/{cid}", timeout=8)
                    if clob_m:
                        toks  = clob_m.get("tokens", [])
                        ctids = [t.get("token_id", "") for t in toks]
                        if not outcomes:
                            outcomes = [t.get("outcome", "") for t in toks]
            for i, tid in enumerate(ctids):
                if not tid:
                    continue
                ol = outcomes[i].lower() if i < len(outcomes) and isinstance(outcomes[i], str) else ""
                if any(k in ol for k in t1k):
                    self._token_map[tid] = ("team1", outcomes[i] if i < len(outcomes) else "team1")
                elif any(k in ol for k in t2k):
                    self._token_map[tid] = ("team2", outcomes[i] if i < len(outcomes) else "team2")
            if len(self._token_map) >= 2:
                break

        if self._token_map:
            for tid, (key, label) in self._token_map.items():
                print(f"  [Poly] Token found: {label} ({key}) = {tid[:20]}...", flush=True)
        else:
            print("  [Poly] WARNING: No token IDs found — will use Gamma outcomePrices fallback", flush=True)

    def fetch(self) -> dict | None:
        # If we have cached token IDs, try fast path first
        if self._token_map:
            prices = {}
            token_ids_list = list(self._token_map.keys())

            # Bulk fetch (single request)
            bulk = clob_bulk_prices(token_ids_list)
            for tid, p in bulk.items():
                key, label = self._token_map.get(tid, (None, None))
                if key and key not in prices:
                    prices[key] = p

            # Per-token fallback for any missing
            for tid, (team_key, label) in self._token_map.items():
                if team_key in prices:
                    continue
                p = clob_mid_price(tid)
                if p is not None:
                    prices[team_key] = p

            if len(prices) == 2:
                return prices

            # Partial or zero — re-discover tokens then full fetch
            print("  [Poly] CLOB fetch incomplete, re-discovering tokens...", flush=True)
            self._discover_tokens()

        # Full discovery + fetch
        return fetch_poly_prices_direct(self.cfg)
# ══════════════════════════════════════════════════════════════════════════════
#  YOSO — Selenium with network request interception
#  Intercepts XHR/fetch calls to Yoso's own backend so we read the JSON
#  directly instead of parsing rendered HTML each time.
# ══════════════════════════════════════════════════════════════════════════════

def build_driver() -> webdriver.Chrome:
    """
    Stable headless Chrome — retries 3x.
    CDP setRequestInterception is NOT used: it holds every network request
    waiting for continueInterceptedRequest which never comes, causing the
    'Timed out receiving message from renderer' crash.
    JS fetch/XHR interception is injected AFTER page load instead.
    """
    for attempt in range(3):
        driver = None
        try:
            opts = Options()
            opts.add_argument("--headless=new")
            opts.add_argument("--no-sandbox")
            opts.add_argument("--disable-dev-shm-usage")
            opts.add_argument("--disable-gpu")
            opts.add_argument("--disable-extensions")
            opts.add_argument("--disable-default-apps")
            opts.add_argument("--mute-audio")
            opts.add_argument("--window-size=1280,900")
            opts.add_argument("--log-level=3")
            opts.add_argument("--silent")
            opts.add_argument("--disable-logging")
            # ── Local PC performance flags ───────────────────────────────
            opts.add_argument("--disable-hang-monitor")
            opts.add_argument("--disable-ipc-flooding-protection")
            opts.add_argument("--force-fieldtrials=NetworkQualityEstimator/Enabled")
            opts.add_experimental_option("excludeSwitches", ["enable-logging"])
            opts.add_experimental_option("useAutomationExtension", False)
            opts.add_argument(
                "--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
            )
            svc          = Service(ChromeDriverManager().install())
            svc.log_path = os.devnull
            driver       = webdriver.Chrome(service=svc, options=opts)
            driver.set_page_load_timeout(BROWSER_TIMEOUT)
            driver.set_script_timeout(BROWSER_TIMEOUT)
            # Warm-up: confirm renderer alive before navigating to Yoso
            driver.get("about:blank")
            # CI environments need more time for Chrome to fully initialize
            wait_time = 3.0 if os.getenv("AUTO_MODE") == "1" else 0.3
            time.sleep(wait_time)
            return driver
        except Exception as e:
            msg = str(e).split("\n")[0][:60]
            print(f"\n  [Browser] Attempt {attempt+1} failed: {msg}")
            if driver:
                try: driver.quit()
                except Exception: pass
            time.sleep(3)
    raise RuntimeError("Chrome failed to start after 3 attempts")


# JavaScript injected into Yoso page
# - Intercepts ALL fetch + XHR calls and stores responses
# - Captures activity/trade data from ANY endpoint
# - Re-injection safe (checks __yoso_intercepted flag)
_INTERCEPT_JS = """
(function() {
    if (!window.__yoso_intercepted) {
        window.__yoso_intercepted = true;
        window.__yoso_api_data    = {};
        window.__yoso_activity    = [];
        window.__yoso_all_trades  = [];
        window.__yoso_seen_urls   = [];

        function _storeActivity(data) {
            var arr = [];
            if (Array.isArray(data))             arr = data;
            else if (data && Array.isArray(data.data))    arr = data.data;
            else if (data && Array.isArray(data.trades))  arr = data.trades;
            else if (data && Array.isArray(data.items))   arr = data.items;
            else if (data && Array.isArray(data.results)) arr = data.results;
            if (arr.length > 0) {
                window.__yoso_activity = arr.slice(0, 100);
                window.__yoso_all_trades = window.__yoso_all_trades.concat(arr).slice(-200);
            }
        }

        function _isActivity(url) {
            return url.includes('trade')    || url.includes('activity') ||
                   url.includes('order')    || url.includes('history')  ||
                   url.includes('position') || url.includes('event')    ||
                   url.includes('fill')     || url.includes('bet')      ||
                   url.includes('purchase') || url.includes('buy');
        }

        function _storeUrl(url) {
            if (url && window.__yoso_seen_urls.indexOf(url) === -1) {
                window.__yoso_seen_urls.push(url);
                if (window.__yoso_seen_urls.length > 200)
                    window.__yoso_seen_urls = window.__yoso_seen_urls.slice(-100);
            }
        }

        // ── Intercept fetch ──────────────────────────────────────────────
        var _origFetch = window.fetch;
        window.fetch = async function() {
            var args = Array.prototype.slice.call(arguments);
            var resp = await _origFetch.apply(this, args);
            try {
                var url = (typeof args[0] === 'string') ? args[0] : (args[0] && args[0].url ? args[0].url : '');
                _storeUrl(url);
                var clone = resp.clone();
                clone.json().then(function(data) {
                    window.__yoso_api_data[url] = data;
                    if (_isActivity(url)) { _storeActivity(data); }
                    window.__yoso_all_trades.push({_url: url, _data: data});
                    window.__yoso_all_trades = window.__yoso_all_trades.slice(-300);
                }).catch(function(){});
            } catch(e) {}
            return resp;
        };

        // ── Intercept XHR ────────────────────────────────────────────────
        var _OrigXHR = window.XMLHttpRequest;
        window.XMLHttpRequest = function() {
            var xhr = new _OrigXHR();
            var _open = xhr.open.bind(xhr);
            xhr.open = function(method, url) {
                xhr._captureUrl = url;
                _storeUrl(url);
                return _open.apply(xhr, arguments);
            };
            xhr.addEventListener('load', function() {
                try {
                    var data = JSON.parse(xhr.responseText);
                    window.__yoso_api_data[xhr._captureUrl] = data;
                    if (_isActivity(xhr._captureUrl)) { _storeActivity(data); }
                } catch(e) {}
            });
            return xhr;
        };
    }

    return JSON.stringify({
        api:        window.__yoso_api_data,
        activity:   window.__yoso_activity,
        all_trades: window.__yoso_all_trades
    });
})();
"""

# JS to actively re-fetch all seen API URLs (called on subsequent fetches)
_REFRESH_JS = """
(function() {
    if (!window.__yoso_intercepted) { return '{"api":{},"activity":[],"all_trades":[]}'; }

    // Re-call every market/price API URL we have seen so far
    var urls = window.__yoso_seen_urls || [];
    var priceUrls = urls.filter(function(u) {
        return (u.includes('market') || u.includes('price') || u.includes('odds') ||
                u.includes('pool') || u.includes('outcome') || u.includes('token') ||
                u.includes('contract') || u.includes('liquidity') || u.includes('bet')) &&
               !u.includes('trade') && !u.includes('activity');
    });
    var actUrls = urls.filter(function(u) {
        return u.includes('trade') || u.includes('activity') || u.includes('order') ||
               u.includes('history') || u.includes('fill') || u.includes('bet') ||
               u.includes('purchase');
    });

    function refetch(url) {
        window.fetch(url).catch(function(){});
    }
    priceUrls.concat(actUrls).forEach(refetch);

    return JSON.stringify({
        api:        window.__yoso_api_data,
        activity:   window.__yoso_activity,
        all_trades: window.__yoso_all_trades,
        seen_count: urls.length
    });
})();
"""


class YosoPriceFetcher:
    """
    Fetches Yoso prices + activity using headless Chrome.
    - First load: full page load + JS injection
    - Subsequent: re-execute JS to get updated intercepted data, fallback to DOM parse
    - Auto-restarts browser on timeout/crash
    """
    def __init__(self, cfg: dict):
        self.cfg          = cfg
        self.driver       = None
        self.fetch_count  = 0
        self.page_loaded  = False
        self._known_trades: set = set()
        self._last_price_change = time.time()
        self._last_prices: dict = {}
        self._stale_reload_sec  = 90   # force reload if prices unchanged for 90s
        self._build()

    def _build(self):
        print("  [Yoso] Starting browser...", end=" ", flush=True)
        if self.driver:
            try: self.driver.quit()
            except Exception: pass
        self.driver     = build_driver()
        self.page_loaded = False
        print("ready ✓")

    def _load_page(self):
        """
        Load Yoso page ONCE. On first call does a full driver.get().
        On subsequent calls (page already loaded) this is a no-op.
        Only reloads if page_loaded is False (e.g. after browser restart).
        """
        if self.page_loaded:
            return  # ← KEY FIX: don't reload on every fetch

        url = self.cfg["yoso_url"]
        for attempt in range(3):
            try:
                self.driver.get(url)
                WebDriverWait(self.driver, BROWSER_TIMEOUT).until(
                    EC.presence_of_element_located((By.TAG_NAME, "body"))
                )
                # Wait for React to finish rendering and fire initial API calls
                # CI environments need more time for JavaScript to execute
                wait_time = 5.0 if os.getenv("AUTO_MODE") == "1" else 1.5
                time.sleep(wait_time)
                try:
                    self.driver.execute_script(_INTERCEPT_JS)
                except Exception:
                    pass
                self.page_loaded = True
                return
            except Exception as e:
                msg = str(e).split("\n")[0][:60]
                print(f"\n  [Yoso] Load attempt {attempt+1} failed: {msg}")
                if attempt < 2:
                    self._build()   # fresh browser, then retry
                else:
                    raise

    def _read_intercepted(self) -> dict:
        """Fire JS refresh and read intercepted data in one pass — no sleep."""
        try:
            self.driver.execute_script(_INTERCEPT_JS)
            raw = self.driver.execute_script(_REFRESH_JS)
            return json.loads(raw) if raw else {}
        except Exception:
            return {}

    def _prices_from_api(self, api_data: dict) -> dict:
        """
        Try to extract prices from intercepted API responses.
        Yoso typically calls /api/markets/<id> or similar.
        """
        prices = {}
        t1k = [self.cfg["yoso_team1"].lower(), self.cfg["team1_label"].lower().split()[0]]
        t2k = [self.cfg["yoso_team2"].lower(), self.cfg["team2_label"].lower().split()[0]]

        for url, data in api_data.items():
            if not isinstance(data, dict):
                continue
            blob = json.dumps(data).lower()
            # Must contain team references
            has_t1 = any(k in blob for k in t1k)
            has_t2 = any(k in blob for k in t2k)
            if not (has_t1 or has_t2):
                continue

            # Try to find price fields
            def scan(obj, depth=0):
                if depth > 8: return
                if isinstance(obj, dict):
                    for k, v in obj.items():
                        kl = k.lower()
                        if isinstance(v, (int, float)):
                            p = round(float(v), 4)
                            if 0.01 <= p <= 0.99:
                                parent_str = json.dumps(obj).lower()
                                if any(kk in parent_str for kk in t1k) and "team1" not in prices:
                                    prices["team1"] = p
                                elif any(kk in parent_str for kk in t2k) and "team2" not in prices:
                                    prices["team2"] = p
                        elif isinstance(v, str):
                            try:
                                p = round(float(v), 4)
                                if 0.01 <= p <= 0.99:
                                    parent_str = json.dumps(obj).lower()
                                    if any(kk in parent_str for kk in t1k) and "team1" not in prices:
                                        prices["team1"] = p
                                    elif any(kk in parent_str for kk in t2k) and "team2" not in prices:
                                        prices["team2"] = p
                            except Exception:
                                pass
                        else:
                            scan(v, depth+1)
                elif isinstance(obj, list):
                    for item in obj:
                        scan(item, depth+1)
            scan(data)

        return prices

    def _prices_from_buy_interaction(self) -> dict:
        """
        SPEED-OPTIMISED Yoso price fetch via buy interaction.
        - Buttons cached after first find (no repeated full DOM scan)
        - Input set via JS React setter (instant, no key-by-key)
        - AVG read via JS innerText (faster than Selenium .text)
        - Sleeps cut to React minimum: 0.5s panel open + 0.7s calc
        - Total per cycle: ~2.5s for both teams
        """
        from selenium.webdriver.common.keys import Keys
        from selenium.webdriver.common.action_chains import ActionChains

        prices = {}
        yt1    = self.cfg["yoso_team1"]
        yt2    = self.cfg["yoso_team2"]

        _JS_SET = """
            var el=arguments[0],v=arguments[1];
            var s=Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype,'value').set;
            s.call(el,v);
            el.dispatchEvent(new Event('input',{bubbles:true}));
            el.dispatchEvent(new Event('change',{bubbles:true}));
        """

        def _escape():
            try: ActionChains(self.driver).send_keys(Keys.ESCAPE).perform()
            except Exception: pass

        def _btn(team_str):
            """Return cached button or re-find it."""
            key = "_btn_" + team_str
            c = getattr(self, key, None)
            if c is not None:
                try:
                    if c.is_displayed(): return c
                except Exception: pass
            tu = team_str.upper()
            for el in self.driver.find_elements(By.XPATH, "//button | //*[@role='button']"):
                try:
                    t = el.text.upper().strip()
                    if tu in t and "BUY" in t and el.is_displayed():
                        setattr(self, key, el); return el
                except Exception: continue
            return None

        def _inp():
            for sel in ("input[type='number']","input[inputmode='decimal']","input"):
                for el in self.driver.find_elements(By.CSS_SELECTOR, sel):
                    try:
                        if el.is_displayed() and el.is_enabled(): return el
                    except Exception: continue
            return None

        def _read(t1, t2):
            js = (
                "var tx=document.body.innerText;"
                "var m=tx.match(/AVG\\s+\\$([0-9]+\\.[0-9]+)/i); if(m) return m[1];"
                "var m2=tx.match(/([0-9]+\\.[0-9]+)\\s+" + t1 + "\\s+Share/i); if(m2) return \'sh:\'+m2[1];"
                "var m3=tx.match(/([0-9]+\\.[0-9]+)\\s+" + t2 + "\\s+Share/i); if(m3) return \'sh:\'+m3[1];"
                "return null;"
            )
            raw = self.driver.execute_script(js)
            if not raw: return None
            try:
                if raw.startswith("sh:"):
                    sh = float(raw[3:])
                    return round(1/sh, 4) if sh > 0 else None
                p = round(float(raw), 4)
                return p if 0.01 <= p <= 0.99 else None
            except Exception: return None

        try:
            _escape()
            for team_str, price_key in [(yt1,"team1"),(yt2,"team2")]:
                b = _btn(team_str)
                if b is None: continue
                self.driver.execute_script("arguments[0].click();", b)
                time.sleep(0.5)
                i = _inp()
                if i is None: _escape(); continue
                self.driver.execute_script(_JS_SET, i, "1")   # $1 input for fast, accurate price
                time.sleep(0.7)
                p = _read(yt1, yt2)
                if p is not None:
                    prices[price_key] = p
                    team_display = (self.cfg["team1_label"] if price_key == "team1"
                                    else self.cfg["team2_label"])
                    print(f"  [Yoso] {team_display} real price → ${p:.4f} ✓", flush=True)
                _escape()
                time.sleep(0.1)
        except Exception as e:
            print(f"\n  [Yoso-interact] {str(e).split(chr(10))[0][:80]}", flush=True)
            _escape()

        return prices

    def _prices_from_dom(self) -> dict:
        """
        Parse visible DOM text for prices.
        Primary:  'Buy WI $0.53'  button labels
        Fallback: 'WI 52%'  chart percentages
        """
        prices = {}
        try:
            text = self.driver.find_element(By.TAG_NAME, "body").text
        except Exception:
            return prices

        yt1 = self.cfg["yoso_team1"]
        yt2 = self.cfg["yoso_team2"]

        # Buy button pattern: "Buy WI $0.53"
        for team_str, key in [(yt1, "team1"), (yt2, "team2")]:
            m = re.search(
                rf'Buy\s+{re.escape(team_str)}\s+\$(\d+\.\d+)',
                text, re.IGNORECASE
            )
            if m:
                prices[key] = round(float(m.group(1)), 4)

        # Fallback: percentage chart "WI 52%"
        if "team1" not in prices:
            m = re.search(rf'\b{re.escape(yt1)}\s+(\d+)%', text, re.IGNORECASE)
            if m:
                prices["team1"] = round(int(m.group(1)) / 100, 4)
        if "team2" not in prices:
            m = re.search(rf'\b{re.escape(yt2)}\s+(\d+)%', text, re.IGNORECASE)
            if m:
                prices["team2"] = round(int(m.group(1)) / 100, 4)

        # AVG price fallback for team1
        if "team1" not in prices:
            m = re.search(r'AVG\s+\$(\d+\.\d+)', text, re.IGNORECASE)
            if m:
                prices["team1"] = round(float(m.group(1)), 4)

        return prices

    def _parse_activity(self, activity_data: list) -> list[dict]:
        """
        Parse raw activity/trade list from Yoso API.
        Returns list of {team, amount, price, side, ts}.
        """
        trades = []
        for item in activity_data:
            if not isinstance(item, dict):
                continue
            blob = json.dumps(item).lower()

            # Amount: look for 'amount', 'size', 'value', 'usdcAmount'
            amount = None
            for field in ["usdcamount", "amount", "size", "value", "cost", "spent"]:
                for k, v in item.items():
                    if k.lower() == field:
                        try:
                            amount = float(v)
                            break
                        except Exception:
                            pass
                if amount is not None:
                    break

            # Price
            price = None
            for field in ["price", "avgprice", "averageprice", "executionprice"]:
                for k, v in item.items():
                    if k.lower() == field:
                        try:
                            price = round(float(v), 4)
                            break
                        except Exception:
                            pass
                if price is not None:
                    break

            # Side / outcome
            side = "YES"
            for k, v in item.items():
                kl = k.lower()
                if kl in ("side", "outcome", "position", "type"):
                    side = str(v).upper()

            # Team
            team = None
            yt1 = self.cfg["yoso_team1"].lower()
            yt2 = self.cfg["yoso_team2"].lower()
            if yt1 in blob:
                team = self.cfg["team1_label"]
            elif yt2 in blob:
                team = self.cfg["team2_label"]

            # Timestamp for dedup
            ts_raw = item.get("timestamp") or item.get("createdAt") or item.get("time") or ""
            trade_id = f"{ts_raw}_{amount}_{price}_{team}"

            if amount is not None and price is not None and team is not None:
                trades.append({
                    "team"  : team,
                    "amount": amount,
                    "price" : price,
                    "side"  : side,
                    "id"    : trade_id,
                })

        return trades

    def _parse_activity_from_dom(self) -> list[dict]:
        """
        Parse Yoso Activity section from rendered page text.

        Yoso activity rows render in multiple formats:
          "WI  $0.5300  $3.20"         (inline)
          "WI Share\n$0.5300\n$3.20"   (multiline block)
          "Bought WI at $0.53 for $3.20"

        We handle all formats and smart-detect price vs amount.
        """
        trades = []
        try:
            text = self.driver.find_element(By.TAG_NAME, "body").text
        except Exception:
            return trades

        yt1 = self.cfg["yoso_team1"]
        yt2 = self.cfg["yoso_team2"]
        t1l = self.cfg["team1_label"]
        t2l = self.cfg["team2_label"]
        seen: set = set()

        def _add(team_label, price, amount):
            if not (0.01 <= price <= 0.99 and amount > 0):
                return
            tid = f"dom_{team_label}_{price:.4f}_{amount:.4f}"
            if tid not in seen:
                seen.add(tid)
                trades.append({
                    "team":   team_label,
                    "amount": amount,
                    "price":  price,
                    "side":   "BUY",
                    "id":     tid,
                })

        # ── Pattern A: inline single line ────────────────────────────────────
        for abbr, label in [(yt1, t1l), (yt2, t2l)]:
            # "WI $0.5300 $3.20"  or  "WI $0.53 for $3.20"
            pat = (r'\b' + re.escape(abbr) + r'\b'
                   r'[^\n$]{0,25}\$(\d+\.\d+)[^\n$]{0,15}\$(\d+\.\d+)')
            for m in re.finditer(pat, text, re.IGNORECASE):
                v1, v2 = float(m.group(1)), float(m.group(2))
                if 0.01 <= v1 <= 0.99:
                    _add(label, round(v1, 4), round(v2, 4))
                elif 0.01 <= v2 <= 0.99:
                    _add(label, round(v2, 4), round(v1, 4))

            # "Buy/Bought WI $0.53 $3.20"
            pat2 = (r'(?:Buy|Bought|Purchased)\s+' + re.escape(abbr)
                    + r'\s+\$(\d+\.\d+)\s+\$(\d+\.\d+)')
            for m in re.finditer(pat2, text, re.IGNORECASE):
                v1, v2 = float(m.group(1)), float(m.group(2))
                if 0.01 <= v1 <= 0.99:
                    _add(label, round(v1, 4), round(v2, 4))

        # ── Pattern B: multiline block ───────────────────────────────────────
        #   "WI Share"          or just "WI"
        #   "$0.5300"
        #   "$3.20"
        #   "2m ago"
        lines = text.split("\n")
        for i, line in enumerate(lines):
            label = None
            ls = line.strip()
            if re.search(r'\b' + re.escape(yt1) + r'\b', ls, re.IGNORECASE):
                label = t1l
            elif re.search(r'\b' + re.escape(yt2) + r'\b', ls, re.IGNORECASE):
                label = t2l
            if not label:
                continue

            dollar_vals = []
            for j in range(i + 1, min(i + 8, len(lines))):
                nxt = lines[j].strip()
                m = re.search(r'\$(\d+\.\d+)', nxt)
                if m:
                    dollar_vals.append(float(m.group(1)))
                elif re.search(r'\d+\s*[mhds]\s*ago', nxt, re.IGNORECASE):
                    break

            if len(dollar_vals) >= 2:
                price_cands  = [v for v in dollar_vals if 0.01 <= v <= 0.99]
                amount_cands = [v for v in dollar_vals if v > 0.99]
                if price_cands and amount_cands:
                    _add(label, round(price_cands[0], 4), round(amount_cands[0], 4))

        return trades

    def fetch(self) -> tuple[dict | None, list[dict]]:
        """
        Returns (prices_dict, new_trades_list).
        prices_dict: {'team1': float, 'team2': float} or None
        new_trades_list: list of new trade dicts since last call

        KEY FIX: page is loaded ONCE. On each subsequent call we:
          1. Re-fire known API URLs via JS (no page reload)
          2. Read latest intercepted data
          3. Fall back to DOM parse if API data thin
        """
        self.fetch_count += 1

        # Restart browser periodically (but much less often now)
        if self.fetch_count % BROWSER_RESTART_EVERY == 0:
            print(f"\n  [Yoso] Scheduled browser restart (fetch #{self.fetch_count})...", end=" ")
            self._build()
            print("done")

        # ── Stale price detection: force page reload if prices frozen ────
        # This handles cases where Yoso's React stops polling its own API
        _now_stale = time.time()
        if self.page_loaded and (_now_stale - self._last_price_change) > self._stale_reload_sec:
            print(f"\n  [Yoso] Prices stale >{self._stale_reload_sec}s — forcing page refresh...", end=" ", flush=True)
            self.page_loaded = False   # triggers _load_page() to re-navigate
            print("reload queued")

        try:
            # ── Load page only once (or after restart) ────────────────────
            self._load_page()

            # ── Extra wait for first few fetches in CI environment ────────
            # This gives Yoso's JavaScript more time to initialize
            if self.fetch_count <= 3 and os.getenv("AUTO_MODE") == "1":
                time.sleep(2.0)

            # ── Keep React alive: tiny scroll so browser doesn't throttle ─
            try:
                self.driver.execute_script("window.scrollBy(0, 1); window.scrollBy(0, -1);")
            except Exception:
                pass

            # ── Read intercepted API data (re-fires known URLs) ───────────
            intercepted = self._read_intercepted()
            api_data    = intercepted.get("api", {})
            activity    = intercepted.get("activity", [])

            # ── Get prices ────────────────────────────────────────────────
            # Priority:
            #   1. Buy-button interaction (most accurate — real market price)
            #   2. Intercepted API data
            #   3. DOM text parse (least accurate — may show stale label price)
            prices = self._prices_from_buy_interaction()

            if len(prices) < 2:
                api_prices = self._prices_from_api(api_data) if api_data else {}
                for k, v in api_prices.items():
                    if k not in prices:
                        prices[k] = v

            if len(prices) < 2:
                dom_prices = self._prices_from_dom()
                for k, v in dom_prices.items():
                    if k not in prices:
                        prices[k] = v

            # Get activity
            all_trades_raw = intercepted.get("all_trades", [])
            trades = self._parse_activity(activity) if activity else []
            if not trades and all_trades_raw:
                trades = self._parse_activity_from_all_trades(all_trades_raw)
            if not trades:
                trades = self._parse_activity_from_dom()

            # Filter only NEW trades (dedup by ID)
            new_trades = []
            for t in trades:
                if t["id"] not in self._known_trades:
                    self._known_trades.add(t["id"])
                    new_trades.append(t)
            if len(self._known_trades) > 500:
                self._known_trades = set(list(self._known_trades)[-200:])

            return_prices = prices if prices else None

            # ── Update stale-detection tracker ───────────────────────────
            if return_prices and return_prices != self._last_prices:
                self._last_prices       = dict(return_prices)
                self._last_price_change = time.time()

            return return_prices, new_trades

        except Exception as e:
            err_str = str(e).split("\n")[0][:80]
            print(f"\n  [Yoso] Error: {err_str}")
            print("  [Yoso] Restarting browser...", end=" ", flush=True)
            self._build()
            print("done")
            return None, []


# ══════════════════════════════════════════════════════════════════════════════
#  MAIN TRACKING LOOP
# ══════════════════════════════════════════════════════════════════════════════

def run_tracker(cfg: dict):
    t1      = cfg["team1_label"]
    t2      = cfg["team2_label"]
    yt1     = cfg["yoso_team1"]
    yt2     = cfg["yoso_team2"]
    thresh  = cfg["threshold"]

    sep = "═" * 62
    print(f"\n{sep}")
    print(f"  🏏  {cfg['name']}")
    print(f"  Combos  : {t1}[Yoso]+{t2}[Poly]  |  {t1}[Poly]+{t2}[Yoso]")
    print(f"  Arb     : <= ${thresh}  |  Whale: > ${WHALE_THRESHOLD}")
    print(f"  Refresh : Poly every {POLY_REFRESH_SEC}s · Yoso every {YOSO_REFRESH_SEC}s")
    print(f"  Ctrl+C  : stop")
    print(f"{sep}\n")

    tg(
        f"🏏 <b>Tracker Started — {cfg['name']}</b>\n\n"
        f"Combo A: <b>{t1}[Yoso] + {t2}[Poly]</b>\n"
        f"Combo B: <b>{t1}[Poly] + {t2}[Yoso]</b>\n\n"
        f"🚨 Arb alert ≤ <b>${thresh}</b>\n"
        f"🐋 Whale alert > <b>${WHALE_THRESHOLD}</b>\n"
        f"Checking every {YOSO_REFRESH_SEC}s"
    )

    poly_fetcher = PolyPriceFetcher(cfg)
    yoso_fetcher = YosoPriceFetcher(cfg)

    last_gap_alert   = {}    # keyed by combo label
    last_whale_alert = 0.0
    last_price_ok    = time.time()
    last_trying_tg   = 0.0
    check_count      = 0
    tracker_start    = time.time()   # when tracking began
    last_no_arb_hb   = time.time()   # last "still hunting" heartbeat
    arb_active       = False          # True while any combo is in arb

    # ── Smart arb state (per combo label) ─────────────────────────────────────
    # arb_band[lbl]          = current band string e.g. "0.94" (rounded total)
    # arb_alert_count[lbl]   = how many alerts sent for current band
    # arb_last_alert[lbl]    = timestamp of last alert for this combo
    # arb_silenced[lbl]      = True if we hit max alerts and went silent
    arb_band        = {}   # lbl -> band str
    arb_alert_count = {}   # lbl -> int
    arb_last_alert  = {}   # lbl -> float timestamp
    arb_silenced    = {}   # lbl -> bool

    # Timing: fetch Poly and Yoso at their own cadences
    last_poly_fetch = 0.0
    last_yoso_fetch = 0.0
    cached_poly     = None
    cached_yoso     = None

    while True:
        check_count += 1
        now = time.time()
        ts  = datetime.now().strftime("%H:%M:%S")

        need_poly = (now - last_poly_fetch) >= POLY_REFRESH_SEC
        need_yoso = (now - last_yoso_fetch) >= YOSO_REFRESH_SEC

        new_poly   = None
        new_trades = []

        # ── Fetch Poly + Yoso in PARALLEL when both are due ───────────────
        if need_poly and need_yoso:
            poly_result_box  = [None]
            yoso_result_box  = [None, []]

            def _fetch_poly():
                poly_result_box[0] = poly_fetcher.fetch()

            def _fetch_yoso():
                r, t = yoso_fetcher.fetch()
                yoso_result_box[0] = r
                yoso_result_box[1] = t

            poly_thread = threading.Thread(target=_fetch_poly, daemon=True)
            yoso_thread = threading.Thread(target=_fetch_yoso, daemon=True)
            poly_thread.start(); yoso_thread.start()
            poly_thread.join();  yoso_thread.join()

            new_poly       = poly_result_box[0]
            yoso_result    = yoso_result_box[0]
            new_trades     = yoso_result_box[1]

            if new_poly:
                cached_poly     = new_poly
            last_poly_fetch = now

            if yoso_result:
                cached_yoso     = yoso_result
            last_yoso_fetch = now

        else:
            # ── Fetch Polymarket only ──────────────────────────────────────
            if need_poly:
                new_poly = poly_fetcher.fetch()
                if new_poly:
                    cached_poly    = new_poly
                last_poly_fetch = now

            # ── Fetch Yoso only ────────────────────────────────────────────
            if need_yoso:
                yoso_result, new_trades = yoso_fetcher.fetch()
                if yoso_result:
                    cached_yoso    = yoso_result
                last_yoso_fetch = now

        # ── Whale detection (runs after any fetch path) ────────────────────
        if WHALE_ALERTS_ENABLED and new_trades:
            for trade in new_trades:
                if trade["amount"] >= WHALE_THRESHOLD:
                    whale_ok = (now - last_whale_alert) >= WHALE_COOLDOWN
                    team_clr = CLR_WI if trade["team"] == cfg["team1_label"] else CLR_SL
                    print(
                        f"\n  {CLR_YLW}{CLR_BOLD}🐋 WHALE{CLR_RST}  "
                        f"{team_clr}{CLR_BOLD}{trade['team']}{CLR_RST}  "
                        f"${trade['amount']:.2f} @ ${trade['price']:.4f}"
                    )
                    if whale_ok:
                        tg_whale(cfg, trade["team"], trade["amount"],
                                 trade["price"], trade["side"])
                        last_whale_alert = now

        # ── Extract prices ─────────────────────────────────────────────────
        wp = cached_poly.get("team1") if cached_poly else None
        sp = cached_poly.get("team2") if cached_poly else None
        wy = cached_yoso.get("team1") if cached_yoso else None
        sy = cached_yoso.get("team2") if cached_yoso else None

        # ── Print status ───────────────────────────────────────────────────
        pp1 = f"${wp:.4f}" if wp is not None else "N/A "
        pp2 = f"${sp:.4f}" if sp is not None else "N/A "
        yy1 = f"${wy:.4f}" if wy is not None else "N/A "
        yy2 = f"${sy:.4f}" if sy is not None else "N/A "
        print(
            f"[{ts}] #{check_count:04d}  "
            f"{CLR_POLY}{CLR_BOLD}Poly{CLR_RST} {CLR_WI}{CLR_BOLD}{yt1}{CLR_RST}={CLR_WI}{pp1}{CLR_RST} "
            f"{CLR_SL}{CLR_BOLD}{yt2}{CLR_RST}={CLR_SL}{pp2}{CLR_RST}  │  "
            f"{CLR_YOSO}{CLR_BOLD}Yoso{CLR_RST} {CLR_WI}{CLR_BOLD}{yt1}{CLR_RST}={CLR_WI}{yy1}{CLR_RST} "
            f"{CLR_SL}{CLR_BOLD}{yt2}{CLR_RST}={CLR_SL}{yy2}{CLR_RST}",
            flush=True
        )

        # ── Combos ─────────────────────────────────────────────────────────
        combo_a = round(wy + sp, 4) if (wy is not None and sp is not None) else None
        combo_b = round(wp + sy, 4) if (wp is not None and sy is not None) else None

        got_any = combo_a is not None or combo_b is not None
        if got_any:
            last_price_ok = now

        # ── "Still trying" ─────────────────────────────────────────────────
        if not got_any:
            since = now - last_price_ok
            if since >= NO_PRICE_TIMEOUT and (now - last_trying_tg) >= NO_PRICE_TIMEOUT:
                mins = int(since // 60)
                tg(
                    f"⏳ <b>Still Trying — {cfg['name']}</b>\n\n"
                    f"No prices for <b>{mins} min</b>.\n"
                    f"Poly: {t1}={pp1} {t2}={pp2}\n"
                    f"Yoso: {t1}={yy1} {t2}={yy2}\n\n"
                    f"Tracker running. Retrying every {YOSO_REFRESH_SEC}s."
                )
                last_trying_tg = now

        # ── No-arb heartbeat (every 10 min while prices exist but no arb) ──
        # Fires only when we HAVE prices but gap is not profitable yet.
        # Stops firing once arb is found. Resumes after arb closes.
        if got_any and not arb_active:
            if (now - last_no_arb_hb) >= NO_ARB_HEARTBEAT:
                mins_run  = int((now - tracker_start) // 60)
                best_c    = None
                if combo_a is not None: best_c = combo_a
                if combo_b is not None:
                    best_c = combo_b if best_c is None else min(best_c, combo_b)
                best_str  = f"Best combo: <b>${best_c:.4f}</b> (need ≤ ${thresh})" if best_c else "Prices partial"
                now_str   = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                tg(
                    f"🔍 <b>Hunting Arbitrage — {cfg['name']}</b>\n\n"
                    f"⏱ Running for <b>{mins_run} min</b> — no arb yet.\n"
                    f"💪 Working hard, will alert the moment one appears!\n\n"
                    f"📊 Current prices:\n"
                    f"  Poly  {yt1}={pp1}  {yt2}={pp2}\n"
                    f"  Yoso  {yt1}={yy1}  {yt2}={yy2}\n\n"
                    f"{best_str}\n\n"
                    f"🕐 {now_str}"
                )
                last_no_arb_hb = now

        # ── Evaluate combos ────────────────────────────────────────────────
        combos = []
        if combo_a is not None:
            combos.append((
                f"{t1}[Yoso]+{t2}[Poly]",
                f"{t1} [Yoso]", wy, f"{t2} [Poly]", sp, combo_a
            ))
        if combo_b is not None:
            combos.append((
                f"{t1}[Poly]+{t2}[Yoso]",
                f"{t1} [Poly]", wp, f"{t2} [Yoso]", sy, combo_b
            ))

        for (lbl, n1, v1, n2, v2, total) in combos:
            gap = round(1.00 - total, 4)

            if total <= thresh:
                # ── SMART ARBITRAGE ALERT LOGIC ──────────────────────────
                #
                # Band = total rounded to ARB_BAND_SIZE (0.01)
                # e.g. $0.9432 → band "0.94", $0.9378 → band "0.93"
                # Rules:
                #   • New band    → always alert (reset counter)
                #   • Same band   → alert max ARB_MAX_ALERTS times,
                #                   spaced MIN_ALERT_SPACING seconds apart
                #   • After max   → silent, send one final "silenced" msg,
                #                   wake up only when band changes
                #   • Band moves  → send "arb moved" notice + new band alerts
                # ─────────────────────────────────────────────────────────
                band     = f"{(int(total / ARB_BAND_SIZE) * ARB_BAND_SIZE):.2f}"
                prev_band     = arb_band.get(lbl)
                alert_count   = arb_alert_count.get(lbl, 0)
                last_alert_ts = arb_last_alert.get(lbl, 0.0)
                silenced      = arb_silenced.get(lbl, False)
                time_since    = now - last_alert_ts
                spacing_ok    = time_since >= MIN_ALERT_SPACING

                band_changed = (prev_band is not None and band != prev_band)

                if band_changed:
                    # ── Band moved → notify + reset ───────────────────────
                    old_gap = round(1.00 - float(prev_band) - ARB_BAND_SIZE/2, 4)
                    if BAND_MOVE_NOTIFY:
                        tg_arb_move(cfg, lbl, old_gap, gap, total,
                                    "up" if gap > old_gap else "down")
                    # Reset state for new band
                    arb_band[lbl]        = band
                    arb_alert_count[lbl] = 0
                    arb_last_alert[lbl]  = 0.0
                    arb_silenced[lbl]    = False
                    alert_count          = 0
                    silenced             = False
                    spacing_ok           = True
                    last_alert_ts        = 0.0
                    time_since           = now

                elif prev_band is None:
                    # First time seeing arb — initialise
                    arb_band[lbl]        = band
                    arb_alert_count[lbl] = 0
                    arb_last_alert[lbl]  = 0.0
                    arb_silenced[lbl]    = False
                    alert_count          = 0
                    silenced             = False
                    spacing_ok           = True

                # Decide whether to send alert
                can_alert = (
                    not silenced and
                    spacing_ok and
                    alert_count < ARB_MAX_ALERTS
                )

                if can_alert:
                    alert_count += 1
                    arb_alert_count[lbl] = alert_count
                    arb_last_alert[lbl]  = now
                    arb_active           = True   # suppress heartbeat during arb
                    last_no_arb_hb       = now    # reset so heartbeat doesn't pile up
                    tg_arb(cfg, lbl, n1, v1, n2, v2, total)
                    # If this was the last allowed alert, send a "going silent" msg
                    if alert_count >= ARB_MAX_ALERTS:
                        arb_silenced[lbl] = True
                        tg_arb_silenced(cfg, lbl, total, gap, alert_count)
                    marker = f"🚨 ALERT #{alert_count}/{ARB_MAX_ALERTS} SENT"
                elif silenced:
                    marker = f"🔕 silent (#{alert_count}/{ARB_MAX_ALERTS}, band={band})"
                elif not spacing_ok:
                    rem    = int(MIN_ALERT_SPACING - time_since)
                    marker = f"⏳ spacing {rem}s  #{alert_count}/{ARB_MAX_ALERTS}"
                else:
                    marker = "🚨 ALERT"

                print(f"  {CLR_GRN}{CLR_BOLD}{lbl} = ${total:.4f}  gap=${gap:.4f}  ◄◄◄ {marker}{CLR_RST}")

            else:
                # ── No arb: clear arb state for this combo ────────────────
                if lbl in arb_band:
                    # Arb just ended — notify if it was active
                    prev_count = arb_alert_count.get(lbl, 0)
                    if prev_count > 0:
                        prev_gap = round(1.00 - float(arb_band[lbl]) - ARB_BAND_SIZE/2, 4)
                        tg(
                            "✅ <b>ARB CLOSED \u2014 " + cfg['name'] + "</b>\n\n"
                            "📊 <b>" + lbl + "</b>\n"
                            f"➕ Combined now: <b>${total:.4f}</b>  (was \u2264${thresh})\n"
                            f"📐 Gap was: ${prev_gap:.4f} \u2014 now gone.\n\n"
                            "🕐 " + datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                        )
                    del arb_band[lbl]
                    arb_alert_count.pop(lbl, None)
                    arb_last_alert.pop(lbl, None)
                    arb_silenced.pop(lbl, None)
                    if not arb_band:          # all combos closed
                        arb_active     = False
                        last_no_arb_hb = now  # restart heartbeat clock fresh

                if gap >= GAP_NOTIFY_THRESHOLD:
                    # ── GAP NOTICE ────────────────────────────────────────
                    last_gap = last_gap_alert.get(lbl, 0.0)
                    if (now - last_gap) >= GAP_COOLDOWN:
                        tg_gap(cfg, lbl, n1, v1, n2, v2, total, gap)
                        last_gap_alert[lbl] = now
                    print(f"  {lbl} = ${total:.4f}  gap=${gap:.4f}  ◄ gap notice")
                else:
                    print(f"  {lbl} = ${total:.4f}  gap=${gap:.4f}")

        # Sleep minimally — the per-source timers above control actual fetch rate
        time.sleep(0.3)


# ══════════════════════════════════════════════════════════════════════════════
#  SETUP HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def discover_yoso_teams(driver, yoso_url: str) -> tuple[str, str] | None:
    try:
        driver.get(yoso_url)
        WebDriverWait(driver, BROWSER_TIMEOUT).until(
            EC.presence_of_element_located((By.TAG_NAME, "body"))
        )
        time.sleep(3)
        text    = driver.find_element(By.TAG_NAME, "body").text
        matches = re.findall(r'Buy\s+([A-Z]{2,6})\s+\$\d+\.\d+', text, re.IGNORECASE)
        if len(matches) >= 2:
            return matches[0].upper(), matches[1].upper()
        matches = re.findall(r'\b([A-Z]{2,5})\s+\d+%', text, re.IGNORECASE)
        if len(matches) >= 2:
            return matches[0].upper(), matches[1].upper()
    except Exception:
        pass
    return None


def setup_custom(tmp_driver) -> dict | None:
    line = "─" * 60
    print(f"\n{line}\n  CUSTOM MARKET SETUP\n{line}")

    while True:
        poly_url = input("\n  Paste Polymarket URL:\n  > ").strip()
        if "polymarket.com" in poly_url:
            break
        print("  Must be a polymarket.com link.")

    while True:
        yoso_url = input("\n  Paste Yoso URL:\n  > ").strip()
        if "yoso.fun" in yoso_url:
            break
        print("  Must be a yoso.fun link.")

    # Auto-discover from Polymarket
    slug  = urlparse(poly_url).path.strip("/").split("/")[-1]
    print(f"\n  [~] Querying Polymarket API for slug: {slug}...", end=" ", flush=True)
    _, teams_raw = (slug, [])
    flat = _gamma_flat(f"{GAMMA_API}/markets", {"slug": slug})
    if not flat:
        flat = _gamma_flat(f"{GAMMA_API}/events", {"slug": slug})

    outcomes_raw = []
    for m in flat:
        oc = m.get("outcomes", [])
        if isinstance(oc, str):
            try: oc = json.loads(oc)
            except: oc = []
        for o in oc:
            if isinstance(o, str) and o not in outcomes_raw:
                outcomes_raw.append(o)
        if len(outcomes_raw) >= 2:
            break

    if len(outcomes_raw) >= 2:
        t1_label = outcomes_raw[0].strip()
        t2_label = outcomes_raw[1].strip()
        print(f"found: {t1_label}  vs  {t2_label}")
    else:
        print("not found.")
        t1_label = input("  Team 1 full name (e.g. New Zealand): ").strip()
        t2_label = input("  Team 2 full name (e.g. South Africa): ").strip()

    def make_keys(name: str) -> list:
        words = name.lower().split()
        keys  = [name.lower(), words[0]]
        if len(words) > 1:
            keys.append(words[-1])
        keys.append("".join(w[0] for w in words))
        return list(set(keys))

    t1_keys = make_keys(t1_label)
    t2_keys = make_keys(t2_label)

    # Auto-discover from Yoso
    print(f"  [~] Loading Yoso page to detect team labels...", end=" ", flush=True)
    yoso_teams = discover_yoso_teams(tmp_driver, yoso_url)
    if yoso_teams:
        yt1_raw, yt2_raw = yoso_teams
        print(f"found raw: {yt1_raw}  vs  {yt2_raw}")

        # ── CRITICAL: Align Yoso labels with Polymarket team1/team2 ordering ──
        # Yoso discovers buttons in DOM order which may NOT match Polymarket's
        # outcome order.  We must verify yt1 → team1_label, yt2 → team2_label.
        def _abbr_matches_team(abbr: str, keys: list, full_label: str) -> bool:
            a = abbr.lower()
            if any(k in a or a in k for k in keys):
                return True
            for w in full_label.lower().split():
                if len(w) >= 2 and (w[:3] in a or a in w[:4]):
                    return True
            return False

        t1_match = _abbr_matches_team(yt1_raw, t1_keys, t1_label)
        t2_match = _abbr_matches_team(yt2_raw, t1_keys, t1_label)

        if not t1_match and t2_match:
            # yt2_raw better matches team1 → swap so yt1 ≡ team1
            yt1, yt2 = yt2_raw, yt1_raw
            print(f"  [~] Yoso labels swapped to match Poly ordering: {yt1} (team1={t1_label})  {yt2} (team2={t2_label})")
        else:
            yt1, yt2 = yt1_raw, yt2_raw
            print(f"  [~] Yoso labels aligned: {yt1} (team1={t1_label})  {yt2} (team2={t2_label})")
    else:
        print("not found.")
        yt1 = input(f"  Yoso button label for {t1_label} (e.g. IND): ").strip().upper()
        yt2 = input(f"  Yoso button label for {t2_label} (e.g. NZ): ").strip().upper()

    raw = input(f"\n  Alert threshold (Enter = ${ALERT_THRESHOLD}): ").strip()
    try:    threshold = float(raw) if raw else ALERT_THRESHOLD
    except: threshold = ALERT_THRESHOLD

    default_name = f"{t1_label} vs {t2_label}"
    raw = input(f"  Market name (Enter = '{default_name}'): ").strip()
    market_name  = raw if raw else default_name

    cfg = {
        "name"        : market_name,
        "poly_url"    : poly_url,
        "yoso_url"    : yoso_url,
        "poly_slug"   : slug,
        "team1_keys"  : t1_keys,
        "team2_keys"  : t2_keys,
        "team1_label" : t1_label,
        "team2_label" : t2_label,
        "yoso_team1"  : yt1,
        "yoso_team2"  : yt2,
        "threshold"   : threshold,
    }

    print(f"\n  ✓ {market_name}")
    print(f"  T1: {t1_label} [{yt1}]   T2: {t2_label} [{yt2}]")
    print(f"  Arb alert ≤ ${threshold}  |  Whale alert > ${WHALE_THRESHOLD}")

    ok = input("\n  Start tracking? [y/n]: ").strip().lower()
    return cfg if ok == "y" else None


# ══════════════════════════════════════════════════════════════════════════════
#  AUTO-CONFIGURATION (for GitHub Actions)
# ══════════════════════════════════════════════════════════════════════════════

# Set AUTO_MODE=1 environment variable to skip all prompts and use these defaults
AUTO_CONFIG = {
    "poly_url": "https://polymarket.com/sports/crint/crint-ind-nzl-2026-03-08",
    "yoso_url": "https://yoso.fun/markets/0x9c9334c0f5c07ace2aa1f1f5fbfe95a79c0e03f6",
    "team1_label": "India",
    "team2_label": "New Zealand",
    "yoso_team1": "IND",
    "yoso_team2": "NZ",
    "market_name": "India vs New Zealand",
    "threshold": ALERT_THRESHOLD,
    "whale_alerts": True,
}

def get_auto_config() -> dict:
    """Create config from hardcoded defaults - no user interaction needed."""
    slug = urlparse(AUTO_CONFIG["poly_url"]).path.strip("/").split("/")[-1]
    
    # Build team keys for matching
    def make_keys(name: str) -> list:
        words = name.lower().split()
        keys  = [name.lower(), words[0]]
        if len(words) > 1:
            keys.append(words[-1])
        keys.append("".join(w[0] for w in words))
        return list(set(keys))
    
    t1_keys = make_keys(AUTO_CONFIG["team1_label"])
    t2_keys = make_keys(AUTO_CONFIG["team2_label"])
    
    cfg = {
        "name"        : AUTO_CONFIG["market_name"],
        "poly_url"    : AUTO_CONFIG["poly_url"],
        "yoso_url"    : AUTO_CONFIG["yoso_url"],
        "poly_slug"   : slug,
        "team1_keys"  : t1_keys,
        "team2_keys"  : t2_keys,
        "team1_label" : AUTO_CONFIG["team1_label"],
        "team2_label" : AUTO_CONFIG["team2_label"],
        "yoso_team1"  : AUTO_CONFIG["yoso_team1"],
        "yoso_team2"  : AUTO_CONFIG["yoso_team2"],
        "threshold"   : AUTO_CONFIG["threshold"],
    }
    
    print(f"\n  ✓ {cfg['name']}")
    print(f"  T1: {cfg['team1_label']} [{cfg['yoso_team1']}]   T2: {cfg['team2_label']} [{cfg['yoso_team2']}]")
    print(f"  Arb alert ≤ ${cfg['threshold']}  |  Whale alert > ${WHALE_THRESHOLD}")
    print()
    
    return cfg

# ══════════════════════════════════════════════════════════════════════════════
#  ENTRY POINT
# ══════════════════════════════════════════════════════════════════════════════

def main():
    sep = "═" * 62
    print(sep)
    print("  🏏  CRICKET ARBITRAGE TRACKER  —  MASTER EDITION")
    print(sep)
    print(f"  Arb threshold : <= ${ALERT_THRESHOLD}")
    print(f"  Whale alert   : buy > ${WHALE_THRESHOLD} on Yoso")
    print(f"  Gap notice    : gap from $1.00 >= ${GAP_NOTIFY_THRESHOLD}")
    print(f"  Refresh rate  : Poly {POLY_REFRESH_SEC}s · Yoso {YOSO_REFRESH_SEC}s")
    print(sep)
    print()

    # Check if running in AUTO_MODE (for GitHub Actions)
    AUTO_MODE = os.getenv("AUTO_MODE", "0") == "1"
    
    global WHALE_ALERTS_ENABLED
    
    if AUTO_MODE:
        # Use hardcoded configuration - no prompts!
        print("  [AUTO MODE] Using built-in configuration")
        WHALE_ALERTS_ENABLED = AUTO_CONFIG["whale_alerts"]
        whale_status = f"{CLR_YLW}ON{CLR_RST}" if WHALE_ALERTS_ENABLED else "OFF"
        print(f"  Whale alerts: {whale_status}\n")
        
        cfg = get_auto_config()
    else:
        # Interactive mode - ask user for everything
        wa = input("  Enable whale alerts? (buy > $2 on Yoso) [y/n, Enter=y]: ").strip().lower()
        WHALE_ALERTS_ENABLED = (wa != "n")
        whale_status = f"{CLR_YLW}ON{CLR_RST}" if WHALE_ALERTS_ENABLED else "OFF"
        print(f"  Whale alerts: {whale_status}\n")

        # Need a temporary browser for Yoso team discovery
        print("  Starting temp browser for market discovery...")
        tmp = build_driver()
        try:
            cfg = setup_custom(tmp)
        finally:
            try: tmp.quit()
            except Exception: pass
        if cfg is None:
            print("  Cancelled.")
            sys.exit(0)

    try:
        run_tracker(cfg)
    except KeyboardInterrupt:
        print("\n\n[Stopped] Ctrl+C")
        tg("⛔ <b>Cricket Tracker stopped.</b>")
        _tg_queue.join()   # flush pending messages
    finally:
        print("[Done] Goodbye.\n")


if __name__ == "__main__":
    main()
