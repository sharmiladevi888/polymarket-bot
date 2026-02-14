"""
Polymarket 5-Minute BTC Probability Trading Bot
================================================
Temporal arbitrage + order flow + Kelly Criterion + compounding.
All ASCII-safe for Windows. No LLM dependency.

Run setup_keys.py FIRST, then test_connection.py, then this.
Usage: python bot.py [--live] [--fund 150] [--min-edge 0.03]
"""

from __future__ import annotations

import os
import sys
import time
import json
import math
import signal as sig_module
import logging
import argparse
import requests
import numpy as np
from collections import deque
from datetime import datetime, timezone, timedelta
from dataclasses import dataclass, field
from typing import Optional, List, Dict, Tuple

# --- Fix Windows console encoding ---
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# --- Polymarket SDK ---
HAS_CLOB = False
try:
    from py_clob_client.client import ClobClient
    from py_clob_client.clob_types import (
        OrderArgs, MarketOrderArgs, OrderType, ApiCreds
    )
    from py_clob_client.order_builder.constants import BUY, SELL
    from dotenv import load_dotenv
    HAS_CLOB = True
except ImportError:
    ClobClient = None
    print("[WARN] Missing packages. Run:")
    print("  pip install py-clob-client==0.34.5 web3==6.14.0 python-dotenv requests numpy")


# ====================================================================== 
# CONFIG 
# ====================================================================== 
@dataclass
class Config:
    # Wallet
    pk: str = ""
    funder: str = ""
    sig_type: int = 1
    api_key: str = ""
    api_secret: str = ""
    api_passphrase: str = ""

    # Fund
    initial_fund: float = 150.0
    current_fund: float = 150.0
    risk_pct: float = 0.10
    min_trade: float = 1.0

    # Edge
    min_edge: float = 0.03
    min_prob: float = 0.53
    max_buy_price: float = 0.70

    # Temporal arbitrage
    min_move_pct: float = 0.06
    confirm_candles: int = 3

    # Risk
    max_daily_loss_pct: float = 0.20
    max_trades_day: int = 50
    cooldown_secs: int = 120
    max_streak_loss: int = 4
    max_drawdown_pct: float = 0.30

    # API
    clob_host: str = "https://clob.polymarket.com"
    gamma_host: str = "https://gamma-api.polymarket.com"
    chain_id: int = 137

    # Runtime
    dry_run: bool = True
    scan_interval: int = 15
    log_level: str = "INFO"

    @property
    def trade_size(self) -> float:
        return max(self.min_trade, round(self.current_fund * self.risk_pct, 2))

    @property
    def max_daily_loss(self) -> float:
        return self.initial_fund * self.max_daily_loss_pct

    @classmethod
    def from_env(cls) -> Config:
        if HAS_CLOB:
            load_dotenv("keys.env")
        c = cls()
        c.pk = os.getenv("PK", "")
        c.funder = os.getenv("FUNDER", "")
        c.sig_type = int(os.getenv("SIG_TYPE", "1"))
        c.api_key = os.getenv("CLOB_API_KEY", "")
        c.api_secret = os.getenv("CLOB_API_SECRET", "")
        c.api_passphrase = os.getenv("CLOB_API_PASSPHRASE", "")
        c.initial_fund = float(os.getenv("FUND", "150.0"))
        c.current_fund = c.initial_fund
        c.risk_pct = float(os.getenv("RISK_PCT", "0.10"))
        c.dry_run = os.getenv("DRY_RUN", "true").lower() == "true"
        c.log_level = os.getenv("LOG_LEVEL", "INFO")
        if c.pk.startswith("0x"):
            c.pk = c.pk[2:]
        return c


# ====================================================================== 
# LOGGING (all ASCII) 
# ====================================================================== 
def make_logger(level: str = "INFO") -> logging.Logger:
    log = logging.getLogger("polybot")
    log.setLevel(getattr(logging, level))
    if log.handlers:
        return log

    fmt = logging.Formatter(
        "%(asctime)s | %(levelname)-5s | %(message)s", datefmt="%H:%M:%S"
    )
    ch = logging.StreamHandler(stream=sys.stdout)
    ch.setFormatter(fmt)
    log.addHandler(ch)

    fh = logging.FileHandler("trade_log.txt", encoding="utf-8")
    fh.setFormatter(fmt)
    log.addHandler(fh)
    return log


# ====================================================================== 
# POLYMARKET CLIENT 
# ====================================================================== 
class PolyClient:
    """Wraps py-clob-client with proper auth for both PK and API key modes."""

    def __init__(self, cfg: Config, log: logging.Logger):
        self.cfg = cfg
        self.log = log
        self.clob: Optional[ClobClient] = None
        self._connect()

    def _connect(self):
        if not HAS_CLOB or not self.cfg.pk:
            self.log.warning("[WARN] No CLOB SDK or private key -> read-only mode")
            return

        try:
            # Initialize with private key
            kwargs = {
                "host": self.cfg.clob_host,
                "key": self.cfg.pk,
                "chain_id": self.cfg.chain_id,
            }
            if self.cfg.sig_type in (1, 2) and self.cfg.funder:
                kwargs["signature_type"] = self.cfg.sig_type
                kwargs["funder"] = self.cfg.funder

            self.clob = ClobClient(**kwargs)

            # Set API credentials
            if self.cfg.api_key and self.cfg.api_secret and self.cfg.api_passphrase:
                self.clob.set_api_creds(ApiCreds(
                    api_key=self.cfg.api_key,
                    api_secret=self.cfg.api_secret,
                    api_passphrase=self.cfg.api_passphrase,
                ))
                self.log.info("[OK] CLOB client ready (saved API creds)")
            else:
                creds = self.clob.create_or_derive_api_creds()
                self.clob.set_api_creds(creds)
                self.log.info(f"[OK] CLOB client ready (derived key: {creds.api_key})")

            # Verify connection
            ok = self.clob.get_ok()
            self.log.info(f"[OK] Server: {ok}")

        except Exception as e:
            self.log.error(f"[FAIL] CLOB init: {e}")
            self.clob = None

    def get_price(self, token_id: str, side: str = "BUY") -> float:
        if not self.clob:
            return 0.50
        try:
            p = self.clob.get_price(token_id, side=side)
            if isinstance(p, dict):
                return float(p.get("price", 0.50))
            return float(p)
        except Exception:
            return 0.50

    def get_midpoint(self, token_id: str) -> float:
        if not self.clob:
            return 0.50
        try:
            m = self.clob.get_midpoint(token_id)
            if isinstance(m, dict):
                return float(m.get("mid", 0.50))
            return float(m)
        except Exception:
            return 0.50

    def buy_market_order(self, token_id: str, amount: float) -> Optional[dict]:
        """Place a Fill-or-Kill market buy order."""
        if not self.clob:
            return None
        try:
            mo = MarketOrderArgs(
                token_id=token_id,
                amount=amount,
                side=BUY,
            )
            signed = self.clob.create_market_order(mo)
            resp = self.clob.post_order(signed, OrderType.FOK)
            return resp
        except Exception as e:
            self.log.error(f"[FAIL] Market order: {e}")
            return None

    def buy_limit_order(self, token_id: str, price: float, size: float) -> Optional[dict]:
        """Place a GTC limit buy order."""
        if not self.clob:
            return None
        try:
            order = OrderArgs(
                token_id=token_id,
                price=price,
                size=size,
                side=BUY,
            )
            signed = self.clob.create_order(order)
            resp = self.clob.post_order(signed, OrderType.GTC)
            return resp
        except Exception as e:
            self.log.error(f"[FAIL] Limit order: {e}")
            return None


# ====================================================================== 
# BINANCE FEED 
# ====================================================================== 
class BinanceFeed:
    BASE = "https://api.binance.com"

    def __init__(self, log: logging.Logger):
        self.log = log
        self.s = requests.Session()

    def price(self) -> Optional[float]:
        try:
            r = self.s.get(f"{self.BASE}/api/v3/ticker/price",
                           params={"symbol": "BTCUSDT"}, timeout=3)
            return float(r.json()["price"])
        except Exception:
            return None

    def klines(self, interval="1m", limit=60) -> List[Dict]:
        try:
            r = self.s.get(f"{self.BASE}/api/v3/klines",
                           params={"symbol": "BTCUSDT", "interval": interval,
                                   "limit": limit}, timeout=5)
            return [{"o": float(k[1]), "h": float(k[2]), "l": float(k[3]),
                     "c": float(k[4]), "v": float(k[5]),
                     "tbv": float(k[9])} for k in r.json()]
        except Exception:
            return []

    def depth(self, limit=20) -> Dict:
        try:
            r = self.s.get(f"{self.BASE}/api/v3/depth",
                           params={"symbol": "BTCUSDT", "limit": limit}, timeout=3)
            d = r.json()
            bv = sum(float(b[1]) for b in d["bids"])
            av = sum(float(a[1]) for a in d["asks"])
            t = bv + av
            return {"bid_pct": bv / t if t else 0.5}
        except Exception:
            return {"bid_pct": 0.5}


# ====================================================================== 
# PROBABILITY ENGINE 
# ====================================================================== 
class ProbEngine:
    """
    Computes true probability of BTC going UP in next 5 min.
    Primary edge: temporal arbitrage (Binance moves before Polymarket reprices).
    """

    def __init__(self, cfg: Config, log: logging.Logger):
        self.cfg = cfg
        self.log = log
        self.feed = BinanceFeed(log)

    def estimate(self, mkt_price_yes: float, mkt_price_no: float
                 ) -> Tuple[str, float, float, float, List[str]]:
        """
        Returns: (direction, our_prob, edge, kelly, reasons)
        direction: "UP", "DOWN", or "NONE"
        """
        klines = self.feed.klines("1m", 60)
        if len(klines) < 20:
            return "NONE", 0.5, 0.0, 0.0, ["Not enough data"]

        closes = [k["c"] for k in klines]
        volumes = [k["v"] for k in klines]
        tbvols = [k["tbv"] for k in klines]

        # --- Signal 1: Temporal Arbitrage (40% weight) ---
        t_dir, t_str = self._temporal(klines)

        # --- Signal 2: Order Flow (25% weight) ---
        f_dir, f_str = self._orderflow(tbvols, volumes)

        # --- Signal 3: Book Pressure (15% weight) ---
        b_dir, b_str = self._book_pressure()

        # --- Signal 4: Technical (20% weight) ---
        k_dir, k_str = self._technicals(closes)

        # Composite score: -1 (DOWN) to +1 (UP)
        score = (t_dir * t_str * 0.40 +
                 f_dir * f_str * 0.25 +
                 b_dir * b_str * 0.15 +
                 k_dir * k_str * 0.20)

        # Score -> probability
        prob_up = 0.50 + score * 0.20
        prob_up = max(0.30, min(0.70, prob_up))

        # Direction and edge
        reasons = []
        if t_str > 0.1:
            reasons.append(f"Temporal:{'UP' if t_dir>0 else 'DN'}({t_str:.2f})")
        if f_str > 0.1:
            reasons.append(f"Flow:{'BUY' if f_dir>0 else 'SELL'}({f_str:.2f})")
        if b_str > 0.1:
            reasons.append(f"Book:{'BID' if b_dir>0 else 'ASK'}({b_str:.2f})")
        reasons.append(f"Tech:{'UP' if k_dir>0 else 'DN'}({k_str:.2f})")
        reasons.append(f"BTC=${closes[-1]:,.0f}")

        if prob_up > 0.50:
            direction = "UP"
            our_prob = prob_up
            mkt_prob = mkt_price_yes
        elif prob_up < 0.50:
            direction = "DOWN"
            our_prob = 1.0 - prob_up
            mkt_prob = mkt_price_no
        else:
            return "NONE", 0.5, 0.0, 0.0, reasons

        edge = our_prob - mkt_prob

        # Kelly: f* = (p*b - q) / b, half-Kelly
        if 0 < mkt_prob < 1:
            b = (1.0 / mkt_prob) - 1.0
            q = 1.0 - our_prob
            kelly = max(0, (our_prob * b - q) / b * 0.5)
            kelly = min(kelly, self.cfg.risk_pct)
        else:
            kelly = 0

        return direction, round(our_prob, 4), round(edge, 4), round(kelly, 4), reasons

    def _temporal(self, klines):
        if len(klines) < 5:
            return 0.0, 0.0
        recent = klines[-5:]
        dirs = [1 if k["c"] > k["o"] else (-1 if k["c"] < k["o"] else 0) for k in recent]

        count = 0
        last = dirs[-1]
        for d in reversed(dirs):
            if d == last and d != 0:
                count += 1
            else:
                break

        if count < self.cfg.confirm_candles:
            return 0.0, 0.0

        move = abs(recent[-1]["c"] - recent[0]["o"]) / recent[0]["o"] * 100
        if move < self.cfg.min_move_pct:
            return 0.0, 0.0

        d = 1.0 if recent[-1]["c"] > recent[0]["o"] else -1.0
        s = min(1.0, move / 0.15)
        return d, s

    def _orderflow(self, tbvols, vols):
        if len(tbvols) < 10:
            return 0.0, 0.0
        buy = sum(tbvols[-10:])
        total = sum(vols[-10:])
        if total == 0:
            return 0.0, 0.0
        ratio = buy / total
        if ratio > 0.58:
            return 1.0, min(1.0, (ratio - 0.50) / 0.20)
        elif ratio < 0.42:
            return -1.0, min(1.0, (0.50 - ratio) / 0.20)
        return 0.0, 0.0

    def _book_pressure(self):
        bp = self.feed.depth()["bid_pct"]
        if bp > 0.56:
            return 1.0, min(1.0, (bp - 0.50) / 0.20)
        elif bp < 0.44:
            return -1.0, min(1.0, (0.50 - bp) / 0.20)
        return 0.0, 0.0

    def _technicals(self, closes):
        score = 0.0
        n = 0
        # RSI
        rsi = self._rsi(closes)
        if rsi < 35:
            score += 1.0; n += 1
        elif rsi > 65:
            score -= 1.0; n += 1
        # EMA
        ef = self._ema(closes, 9)
        es = self._ema(closes, 21)
        if ef > es * 1.0001:
            score += 0.5; n += 1
        elif ef < es * 0.9999:
            score -= 0.5; n += 1
        if n == 0:
            return 0.0, 0.0
        d = 1.0 if score > 0 else (-1.0 if score < 0 else 0.0)
        return d, min(1.0, abs(score) / 1.5)

    @staticmethod
    def _rsi(c, p=14):
        if len(c) < p + 1:
            return 50.0
        d = np.diff(c[-(p+1):])
        g = np.mean(np.where(d > 0, d, 0))
        l = np.mean(np.where(d < 0, -d, 0))
        if l == 0: return 100.0
        return 100.0 - 100.0 / (1 + g / l)

    @staticmethod
    def _ema(c, p):
        if len(c) < p:
            return c[-1]
        m = 2.0 / (p + 1)
        e = c[0]
        for v in c[1:]:
            e = (v - e) * m + e
        return e


# ====================================================================== 
# MARKET DISCOVERY 
# ====================================================================== 
class Markets:
    def __init__(self, cfg: Config, log: logging.Logger):
        self.cfg = cfg
        self.log = log
        self.s = requests.Session()

    def find_5m_btc(self) -> Optional[Dict]:
        """Returns dict with slug, question, yes_token, no_token or None."""
        # Method 1: events search
        m = self._search_events()
        if m:
            return m

        # Method 2: generated slugs
        m = self._try_slugs()
        if m:
            return m

        # Method 3: markets endpoint
        return self._search_markets()

    def _search_events(self):
        try:
            r = self.s.get(f"{self.cfg.gamma_host}/events", params={
                "active": "true", "closed": "false", "limit": 50,
            }, timeout=10)
            for e in r.json():
                slug = e.get("slug", "").lower()
                title = e.get("title", "").lower()
                if ("btc" in slug or "bitcoin" in title) and "5m" in slug:
                    return self._parse(e)
        except Exception:
            pass
        return None

    def _try_slugs(self):
        now = int(datetime.now(timezone.utc).timestamp())
        aligned = (now // 300) * 300
        for off in [0, 300, -300, 600, -600]:
            slug = f"btc-updown-5m-{aligned + off}"
            try:
                r = self.s.get(
                    f"{self.cfg.gamma_host}/events/slug/{slug}", timeout=5
                )
                if r.status_code == 200:
                    data = r.json()
                    if data and not data.get("closed"):
                        m = self._parse(data)
                        if m:
                            return m
            except Exception:
                continue
        return None

    def _search_markets(self):
        try:
            r = self.s.get(f"{self.cfg.gamma_host}/markets", params={
                "active": "true", "closed": "false", "limit": 100,
            }, timeout=10)
            for m in r.json():
                q = m.get("question", "").lower()
                s = m.get("market_slug", m.get("slug", "")).lower()
                if ("bitcoin" in q or "btc" in s) and "5m" in s:
                    ids = m.get("clobTokenIds", [])
                    if len(ids) >= 2:
                        return {
                            "slug": s, "question": m.get("question", ""),
                            "yes_token": ids[0], "no_token": ids[1],
                        }
        except Exception:
            pass
        return None

    def _parse(self, event):
        ms = event.get("markets", [])
        if not ms:
            return None
        m = ms[0]
        ids = m.get("clobTokenIds", [])
        if len(ids) < 2:
            return None
        return {
            "slug": event.get("slug", ""),
            "question": m.get("question", event.get("title", "")),
            "yes_token": ids[0],
            "no_token": ids[1],
        }


# ====================================================================== 
# TRADE RECORD 
# ====================================================================== 
@dataclass
class Trade:
    ts: str
    slug: str
    direction: str
    price: float
    size: float
    shares: float
    est_prob: float
    edge: float
    kelly: float
    fund: float
    outcome: str = ""
    pnl: float = 0.0


# ====================================================================== 
# MAIN BOT 
# ====================================================================== 
class Bot:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.log = make_logger(cfg.log_level)
        self.client = PolyClient(cfg, self.log)
        self.engine = ProbEngine(cfg, self.log)
        self.markets = Markets(cfg, self.log)

        # State
        self.running = True
        self.daily_pnl = 0.0
        self.trades_today = 0
        self.streak_loss = 0
        self.peak_fund = cfg.initial_fund
        self.last_loss_t = 0.0
        self.open: List[Trade] = []
        self.history: List[Trade] = []

        sig_module.signal(sig_module.SIGINT, self._stop)
        sig_module.signal(sig_module.SIGTERM, self._stop)

    def _stop(self, *_):
        self.log.info("[STOP] Shutting down...")
        self.running = False

    def run(self):
        self._banner()
        while self.running:
            try:
                self._cycle()
                for _ in range(self.cfg.scan_interval):
                    if not self.running:
                        break
                    time.sleep(1)
            except KeyboardInterrupt:
                break
            except Exception as e:
                self.log.error(f"[ERR] {e}")
                time.sleep(5)
        self._summary()

    def _cycle(self):
        # Risk check
        ok, reason = self._risk_check()
        if not ok:
            self.log.info(f"[PAUSE] {reason}")
            return

        # Find market
        mkt = self.markets.find_5m_btc()
        if not mkt:
            self.log.info("[WAIT] No active 5M BTC market found")
            return

        # Get prices
        p_yes = self.client.get_price(mkt["yes_token"], "BUY")
        p_no = self.client.get_price(mkt["no_token"], "BUY")
        self.log.info(
            f"[MKT] {mkt['question'][:55]} | "
            f"UP={{p_yes:.4f}} DN={{p_no:.4f}}"
        )

        # Probability estimation
        direction, prob, edge, kelly, reasons = self.engine.estimate(p_yes, p_no)
        self.log.info(
            f"[CALC] Dir={{direction}} Prob={{prob:.2%}} Edge={{edge:+.2%}} "
            f"Kelly={{kelly:.2%}}"
        )
        for r in reasons:
            self.log.info(f"  |- {{r}}")

        # Trade filters
        if direction == "NONE":
            self.log.info("[SKIP] No directional signal")
            return
        if edge < self.cfg.min_edge:
            self.log.info(
                f"[SKIP] Edge {{edge:.2%}} < min {{self.cfg.min_edge:.2%}}"
            )
            return
        if prob < self.cfg.min_prob:
            self.log.info(f"[SKIP] Prob {{prob:.2%}} < min {{self.cfg.min_prob:.2%}}")
            return

        # Pick token and price
        if direction == "UP":
            token = mkt["yes_token"]
            price = p_yes
        else:
            token = mkt["no_token"]
            price = p_no

        if price > self.cfg.max_buy_price or price <= 0.01:
            self.log.info(f"[SKIP] Price {{price:.4f}} out of range")
            return

        # Size (10% of current fund, Kelly-adjusted)
        kelly_size = self.cfg.current_fund * kelly
        base_size = self.cfg.trade_size
        trade_size = min(base_size, kelly_size) if kelly_size > 0 else base_size
        trade_size = max(self.cfg.min_trade, round(trade_size, 2))
        shares = round(trade_size / price, 2)

        self.log.info(
            f"[>>] {{direction}} | ${{trade_size}} -> {{shares}} shares @ {{price:.4f}} "
            f"| Fund: ${{self.cfg.current_fund:.2f}}"
        )

        # Execute
        if self.cfg.dry_run:
            self.log.info(f"[TEST] DRY RUN - would buy {{shares}} {{direction}} @ {{price:.4f}}")
            resp = {"dry_run": True}
        else:
            self.log.info(f"[LIVE] Placing FOK market order...")
            resp = self.client.buy_market_order(token, trade_size)
            if resp:
                self.log.info(f"[OK] Order response: {{resp}}")
            else:
                self.log.error("[FAIL] Order not placed")
                return

        # Record trade
        trade = Trade(
            ts=datetime.now(timezone.utc).isoformat(),
            slug=mkt["slug"], direction=direction,
            price=price, size=trade_size, shares=shares,
            est_prob=prob, edge=edge, kelly=kelly,
            fund=self.cfg.current_fund,
        )
        self.open.append(trade)
        self.trades_today += 1
        self.log.info(f"[TRADE] #{{self.trades_today}} placed!")

        # Check old trades
        self._resolve()

    def _resolve(self):
        """Resolve open trades (dry run: simulate after 5 min)."""
        resolved = []
        for t in self.open:
            ts = datetime.fromisoformat(t.ts.replace("Z", "+00:00"))
            elapsed = (datetime.now(timezone.utc) - ts).total_seconds()
            if elapsed < 300:
                continue

            if self.cfg.dry_run:
                import random
                won = random.random() < t.est_prob
            else:
                won = self._check_resolution(t)

            if won:
                profit = t.shares * 1.0 - t.size
                t.outcome = "WIN"
                t.pnl = round(profit, 2)
                self.cfg.current_fund = round(self.cfg.current_fund + profit, 2)
                self.daily_pnl += profit
                self.streak_loss = 0
                if self.cfg.current_fund > self.peak_fund:
                    self.peak_fund = self.cfg.current_fund
                growth = (self.cfg.current_fund / self.cfg.initial_fund - 1) * 100
                self.log.info(
                    f"[WIN] +${{profit:.2f}} | Fund: ${{self.cfg.current_fund:.2f}} "
                    f"({growth:+.1f}%) | Next: ${{self.cfg.trade_size:.2f}}"
                )
            else:
                t.outcome = "LOSS"
                t.pnl = -t.size
                self.cfg.current_fund = round(self.cfg.current_fund - t.size, 2)
                self.daily_pnl -= t.size
                self.streak_loss += 1
                self.last_loss_t = time.time()
                self.log.info(
                    f"[LOSS] -${{t.size:.2f}} | Fund: ${{self.cfg.current_fund:.2f}} "
                    f"| Streak: {{self.streak_loss}}"
                )

            self.history.append(t)
            resolved.append(t)

        for t in resolved:
            self.open.remove(t)

    def _check_resolution(self, trade: Trade) -> bool:
        """Check actual market resolution via Gamma API."""
        try:
            r = requests.get(
                f"{self.cfg.gamma_host}/events/slug/{{trade.slug}}",
                timeout=5
            )
            if r.status_code != 200:
                return False
            event = r.json()
            ms = event.get("markets", [])
            if not ms:
                return False
            m = ms[0]
            prices = json.loads(m.get("outcomePrices", "[]"))
            if not prices:
                return False
            up_price = float(prices[0])
            if trade.direction == "UP":
                return up_price > 0.95
            else:
                return up_price < 0.05
        except Exception:
            return False

    def _risk_check(self) -> Tuple[bool, str]:
        if self.daily_pnl <= -self.cfg.max_daily_loss:
            return False, f"Daily loss limit: ${{self.daily_pnl:.2f}}"
        if self.trades_today >= self.cfg.max_trades_day:
            return False, f"Max trades hit: {{self.trades_today}}"
        if self.streak_loss >= self.cfg.max_streak_loss:
            return False, f"Loss streak: {{self.streak_loss}}"
        if self.last_loss_t > 0:
            wait = self.cfg.cooldown_secs - (time.time() - self.last_loss_t)
            if wait > 0:
                return False, f"Cooldown: {{int(wait)}}s"
        dd = (self.peak_fund - self.cfg.current_fund) / self.peak_fund if self.peak_fund > 0 else 0
        if dd > self.cfg.max_drawdown_pct:
            return False, f"Drawdown: {{dd:.1%}}"
        if self.cfg.current_fund < self.cfg.min_trade:
            return False, "Fund too low"
        return True, "OK"

    def _banner(self):
        self.log.info("=" * 55)
        self.log.info("  Polymarket 5M BTC Probability Bot v3")
        self.log.info("=" * 55)
        self.log.info(f"  Fund:      ${{self.cfg.current_fund:.2f}}")
        self.log.info(f"  Per trade: {{self.cfg.risk_pct:.0%}} = ${{self.cfg.trade_size:.2f}}")
        self.log.info(f"  Min edge:  {{self.cfg.min_edge:.0%}}")
        self.log.info(f"  Compound:  ON")
        self.log.info(f"  Mode:      {{'DRY RUN' if self.cfg.dry_run else 'LIVE'}}")
        self.log.info(f"  Auth:      {{'API key' if self.cfg.api_key else 'PK-derived'}}")
        self.log.info(f"  Scan:      every {{self.cfg.scan_interval}}s")
        self.log.info("=" * 55)

    def _summary(self):
        wins = sum(1 for t in self.history if t.outcome == "WIN")
        losses = sum(1 for t in self.history if t.outcome == "LOSS")
        total = wins + losses
        self.log.info("\n" + "=" * 55)
        self.log.info("  SESSION SUMMARY")
        self.log.info("=" * 55)
        self.log.info(f"  Trades:  {{total}} ({{wins}}W / {{losses}}L)")
        if total > 0:
            self.log.info(f"  WinRate: {{wins/total:.1%}}")
        self.log.info(f"  PnL:     ${{self.daily_pnl:+.2f}}")
        self.log.info(f"  Start:   ${{self.cfg.initial_fund:.2f}}")
        self.log.info(f"  End:     ${{self.cfg.current_fund:.2f}}")
        g = (self.cfg.current_fund / self.cfg.initial_fund - 1) * 100
        self.log.info(f"  Growth:  {{g:+.1f}}%")
        self.log.info("=" * 55)

        # Save results
        data = {
            "session": {
                "start": self.cfg.initial_fund, "end": self.cfg.current_fund,
                "pnl": self.daily_pnl, "trades": total,
                "wins": wins, "losses": losses, "growth_pct": g,
            },
            "trades": [
                {"ts": t.ts, "slug": t.slug, "dir": t.direction,
                 "price": t.price, "size": t.size, "shares": t.shares,
                 "prob": t.est_prob, "edge": t.edge, "kelly": t.kelly,
                 "fund": t.fund, "outcome": t.outcome, "pnl": t.pnl}
                for t in self.history
            ]
        }
        with open("results.json", "w") as f:
            json.dump(data, f, indent=2)
        self.log.info("  Saved: results.json")


# ====================================================================== 
# CLI 
# ====================================================================== 
if __name__ == "__main__":
    p = argparse.ArgumentParser(description="Polymarket 5M BTC Bot")
    p.add_argument("--live", action="store_true", help="Live trading")
    p.add_argument("--fund", type=float, help="Starting fund")
    p.add_argument("--risk", type=float, help="Risk per trade (0.10 = 10%%)")
    p.add_argument("--min-edge", type=float, help="Min edge (0.03 = 3%%)")
    p.add_argument("--interval", type=int, help="Scan interval seconds")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args()

    cfg = Config.from_env()
    if args.live:
        cfg.dry_run = False
    if args.fund:
        cfg.initial_fund = args.fund
        cfg.current_fund = args.fund
    if args.risk:
        cfg.risk_pct = args.risk
    if args.min_edge is not None:
        cfg.min_edge = args.min_edge
    if args.interval:
        cfg.scan_interval = args.interval
    if args.verbose:
        cfg.log_level = "DEBUG"

    bot = Bot(cfg)
    bot.run()
