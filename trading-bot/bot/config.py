"""
Configuration for the Trading Bot
All settings are loaded from .env file. Never hardcode API keys here.
"""
import os
from dotenv import load_dotenv

load_dotenv()

# ─── Exchange ─────────────────────────────────────────────────────────────────
BINANCE_API_KEY = os.getenv("BINANCE_API_KEY", "")
BINANCE_API_SECRET = os.getenv("BINANCE_API_SECRET", "")
EXCHANGE_ID = "binance"

# ─── Trading Mode ─────────────────────────────────────────────────────────────
# "paper"  → simulation, no real money
# "live"   → fully automated real orders (after paper validation)
TRADING_MODE = os.getenv("TRADING_MODE", "paper")
LIVE_ORDER_TYPE = os.getenv("LIVE_ORDER_TYPE", "market").strip().lower()
LIVE_CONFIRMATION_REQUIRED_VALUE = os.getenv("LIVE_CONFIRMATION_REQUIRED_VALUE", "I_UNDERSTAND_LIVE_RISK")
LIVE_CONFIRMATION_PHRASE = os.getenv("LIVE_CONFIRMATION_PHRASE", "").strip()

# ─── Capital Management ───────────────────────────────────────────────────────
TOTAL_CAPITAL_USDT = float(os.getenv("TOTAL_CAPITAL_USDT", "1000"))
MAX_RISK_PER_TRADE_PCT = 0.02
MAX_SIMULTANEOUS_TRADES = 5
MAX_CAPITAL_PER_TRADE_PCT = 0.20
DAILY_LOSS_LIMIT_PCT = 0.06
MAX_DRAWDOWN_PCT = 0.15

# ─── Symbols to Watch ─────────────────────────────────────────────────────────
DEFAULT_WATCHLIST = "BTC/USDT,ETH/USDT,BNB/USDT,SOL/USDT,ADA/USDT"
WATCHLIST = os.getenv("WATCHLIST", DEFAULT_WATCHLIST).split(",")

# ─── Timeframes ───────────────────────────────────────────────────────────────
PRIMARY_TIMEFRAME = "1h"
CONFIRM_TIMEFRAME = "4h"

# ─── Signal Scoring ───────────────────────────────────────────────────────────
MIN_SIGNAL_SCORE = 70

# ─── Risk / Reward ────────────────────────────────────────────────────────────
ATR_MULTIPLIER_SL = 2.0
RR_RATIO = 2.0
PARTIAL_TP1_RATIO = 1.0
PARTIAL_TP2_RATIO = 2.0
TRAILING_SL_PCT = 0.015

# ─── Notifications ────────────────────────────────────────────────────────────
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

# ─── Database ─────────────────────────────────────────────────────────────────
DB_PATH = os.getenv("DB_PATH", "trading_bot.db")
DB_RETENTION_DAYS = int(os.getenv("DB_RETENTION_DAYS", "180"))

# ─── Scheduler ────────────────────────────────────────────────────────────────
SCAN_INTERVAL_SECONDS = 300
WS_MAX_SYMBOLS = int(os.getenv("WS_MAX_SYMBOLS", "6"))
SIGNAL_QUEUE_MAXSIZE = int(os.getenv("SIGNAL_QUEUE_MAXSIZE", "200"))

# ─── Execution Safety Guards ───────────────────────────────────────────────────
MIN_ORDER_NOTIONAL_USDT = float(os.getenv("MIN_ORDER_NOTIONAL_USDT", "10"))
MAX_ORDER_NOTIONAL_USDT = float(
	os.getenv("MAX_ORDER_NOTIONAL_USDT", str(TOTAL_CAPITAL_USDT * MAX_CAPITAL_PER_TRADE_PCT))
)
MAX_ENTRY_SPREAD_PCT = float(os.getenv("MAX_ENTRY_SPREAD_PCT", "0.002"))
MAX_SLIPPAGE_PCT = float(os.getenv("MAX_SLIPPAGE_PCT", "0.003"))
MAX_SIGNAL_STALENESS_SECONDS = int(os.getenv("MAX_SIGNAL_STALENESS_SECONDS", "180"))
