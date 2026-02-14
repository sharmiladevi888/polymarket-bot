# Polymarket 5M BTC Trading Bot v3

Probability-based trading with compounding. No LLM. Real API trading.

## Setup (3 steps)

### Step 1: Install
```
pip install -r requirements.txt
```
Pin `web3==6.14.0` to avoid dependency conflicts.

### Step 2: Generate API Keys
```
python setup_keys.py
```
This will:
- Ask for your private key (get it from https://reveal.polymarket.com)
- Ask for your login type (email=1, MetaMask=2, EOA=0)
- Ask for your funder address (from polymarket.com/settings -> Deposit Address)
- Generate and save API key + secret + passphrase to `keys.env`

### Step 3: Test Connection
```
python test_connection.py
```
Verifies: server connection, authentication, market discovery, price fetching.

### Step 4: Run Bot
```
python bot.py              # Dry run (paper trading)
python bot.py --live       # REAL money trading
python bot.py --min-edge 0.02 --interval 10  # More aggressive
```

## How It Works

1. Fetches BTC price/orderflow from Binance (free, no key needed)
2. Detects price moves that Polymarket hasn't repriced yet (temporal arbitrage)
3. Combines 4 signals: temporal arb (40%), order flow (25%), book pressure (15%), technicals (20%)
4. Only trades when edge > 3% over market price
5. Uses Kelly Criterion for optimal bet sizing
6. Compounds: 10% of GROWING fund per trade

## Files
```
setup_keys.py       <- Run FIRST (generates keys.env)
test_connection.py  <- Run SECOND (verifies everything)
bot.py              <- Run THIRD (the actual bot)
keys.env            <- Auto-generated credentials (DO NOT SHARE)
requirements.txt    <- Python dependencies
```

## Auth Notes

The bot supports both authentication methods:
- **Private key only**: Derives API creds automatically via `create_or_derive_api_creds()`
- **API key + secret + passphrase**: Uses saved creds from `keys.env` (faster, no derivation needed each time)

Important:
- Private key should NOT have `0x` prefix
- Funder address = your Polymarket deposit address (NOT your MetaMask address for email logins)
- If you change your private key, run `setup_keys.py` again to regenerate API creds
