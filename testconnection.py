"""
Step 2: Test your connection before running the bot.
======================================================
Usage: python test_connection.py
"""
import os
import sys
import json
import requests
from datetime import datetime, timezone, timedelta

try:
    from py_clob_client.client import ClobClient
    from py_clob_client.clob_types import ApiCreds
    from dotenv import load_dotenv
except ImportError:
    print("[ERROR] Missing packages. Run:")
    print("  pip install py-clob-client==0.34.5 web3==6.14.0 python-dotenv requests")
    sys.exit(1)

load_dotenv("keys.env")

HOST = "https://clob.polymarket.com"
GAMMA = "https://gamma-api.polymarket.com"
CHAIN_ID = 137


def init_client():
    """Initialize CLOB client from keys.env."""
    pk = os.getenv("PK", "")
    funder = os.getenv("FUNDER", "")
    sig_type = int(os.getenv("SIG_TYPE", "1"))
    api_key = os.getenv("CLOB_API_KEY", "")
    api_secret = os.getenv("CLOB_API_SECRET", "")
    api_pass = os.getenv("CLOB_API_PASSPHRASE", "")

    if not pk:
        print("[FAIL] No PK in keys.env. Run setup_keys.py first.")
        sys.exit(1)

    if pk.startswith("0x"):
        pk = pk[2:]

    # Build client
    if sig_type in (1, 2) and funder:
        client = ClobClient(
            HOST, key=pk, chain_id=CHAIN_ID,
            signature_type=sig_type, funder=funder
        )
    else:
        client = ClobClient(HOST, key=pk, chain_id=CHAIN_ID)

    # Set API creds (either from env or derive fresh)
    if api_key and api_secret and api_pass:
        client.set_api_creds(ApiCreds(
            api_key=api_key,
            api_secret=api_secret,
            api_passphrase=api_pass,
        ))
        print("[OK] Using saved API credentials from keys.env")
    else:
        print("[...] Deriving API credentials...")
        creds = client.create_or_derive_api_creds()
        client.set_api_creds(creds)
        print(f"[OK] Derived API key: {creds.api_key}")

    return client


def test_basic(client):
    """Test basic CLOB connection."""
    print("\n--- Basic Connection ---")
    try:
        ok = client.get_ok()
        print(f"[OK] Server status: {ok}")
    except Exception as e:
        print(f"[FAIL] get_ok: {e}")

    try:
        t = client.get_server_time()
        print(f"[OK] Server time: {t}")
    except Exception as e:
        print(f"[FAIL] get_server_time: {e}")


def test_auth(client):
    """Test authenticated endpoints."""
    print("\n--- Authentication ---")
    try:
        orders = client.get_orders()
        count = len(orders) if orders else 0
        print(f"[OK] get_orders: {count} open orders")
    except Exception as e:
        print(f"[FAIL] get_orders: {e}")
        print("  -> Check: PK, FUNDER, SIG_TYPE, API credentials")


def find_5m_markets():
    """Find active 5M BTC markets via Gamma API."""
    print("\n--- Market Discovery (5M BTC) ---")

    # Method 1: Search events
    try:
        r = requests.get(f"{GAMMA}/events", params={
            "active": "true", "closed": "false", "limit": 50,
        }, timeout=10)
        events = r.json()
        found = []
        for e in events:
            slug = e.get("slug", "").lower()
            title = e.get("title", "").lower()
            if ("btc" in slug or "bitcoin" in title) and "5m" in slug:
                markets = e.get("markets", [])
                for m in markets:
                    ids = m.get("clobTokenIds", [])
                    if len(ids) >= 2:
                        found.append({
                            "slug": e["slug"],
                            "question": m.get("question", e.get("title", "")),
                            "yes_token": ids[0],
                            "no_token": ids[1],
                            "prices": m.get("outcomePrices", ""),
                            "active": m.get("active", False),
                            "closed": m.get("closed", False),
                        })
        if found:
            print(f"[OK] Found {len(found)} active 5M BTC market(s):")
            for m in found[:3]:
                print(f"     Slug: {m['slug']}")
                print(f"     Q:    {m['question'][:70]}")
                print(f"     YES:  {m['yes_token'][:30]}...")
                print(f"     NO:   {m['no_token'][:30]}...")
                print()
            return found[0]
        else:
            print("[WARN] No active 5M BTC markets found via events search")
    except Exception as e:
        print(f"[FAIL] Events search: {e}")

    # Method 2: Try generated slugs
    print("[...] Trying generated slug patterns...")
    now = datetime.now(timezone.utc)
    ts = int(now.timestamp())
    aligned = (ts // 300) * 300

    for offset in [0, 300, -300, 600]:
        slug = f"btc-updown-5m-{aligned + offset}"
        try:
            r = requests.get(f"{GAMMA}/events/slug/{slug}", timeout=5)
            if r.status_code == 200:
                event = r.json()
                if event and not event.get("closed"):
                    print(f"[OK] Found market: {slug}")
                    markets = event.get("markets", [])
                    if markets:
                        m = markets[0]
                        ids = m.get("clobTokenIds", [])
                        if len(ids) >= 2:
                            return {
                                "slug": slug,
                                "question": m.get("question", ""),
                                "yes_token": ids[0],
                                "no_token": ids[1],
                            }
        except Exception:
            pass

    print("[WARN] Could not find 5M BTC market. Markets may not be active right now.")
    return None


def test_prices(client, market):
    """Test price fetching for a discovered market."""
    if not market:
        return
    print("\n--- Price Check ---")
    try:
        yes_id = market["yes_token"]
        mid = client.get_midpoint(yes_id)
        print(f"[OK] YES midpoint: {mid}")

        price = client.get_price(yes_id, side="BUY")
        print(f"[OK] YES buy price: {price}")

        book = client.get_order_book(yes_id)
        bids = len(book.get("bids", [])) if isinstance(book, dict) else 0
        asks = len(book.get("asks", [])) if isinstance(book, dict) else 0
        print(f"[OK] Order book: {bids} bids, {asks} asks")
    except Exception as e:
        print(f"[FAIL] Price fetch: {e}")


def main():
    print("=" * 60)
    print("  Polymarket Connection Test")
    print("=" * 60)

    client = init_client()
    test_basic(client)
    test_auth(client)
    market = find_5m_markets()
    test_prices(client, market)

    print("\n" + "=" * 60)
    print("  TEST COMPLETE")
    print("=" * 60)
    if market:
        print("\n  Everything looks good! Run: python bot.py")
    else:
        print("\n  Auth works but no 5M market found.")
        print("  Markets rotate every 5 mins. Try again shortly.")
    print()


if __name__ == "__main__":
    main()
