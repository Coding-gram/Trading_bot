"""
Configuration for the Trading Bot
All settings are loaded from .env file. Never hardcode API keys here.
"""
import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()


def _env_bool(name: str, default: bool) -> bool:
	raw = os.getenv(name)
	if raw is None:
		return default
	return raw.strip().lower() in {"1", "true", "yes", "on"}

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
MAX_TOTAL_PORTFOLIO_RISK_PCT = float(os.getenv("MAX_TOTAL_PORTFOLIO_RISK_PCT", "0.06"))
MAX_CORRELATED_POSITIONS = int(os.getenv("MAX_CORRELATED_POSITIONS", "2"))
CORRELATION_THRESHOLD = float(os.getenv("CORRELATION_THRESHOLD", "0.75"))

# ─── Symbols to Watch ─────────────────────────────────────────────────────────
DEFAULT_WATCHLIST = "BTC/USDT,ETH/USDT,BNB/USDT,SOL/USDT,ADA/USDT"
WATCHLIST = [s.strip() for s in os.getenv("WATCHLIST", DEFAULT_WATCHLIST).split(",") if s.strip()]

# ─── Timeframes ───────────────────────────────────────────────────────────────
PRIMARY_TIMEFRAME = os.getenv("PRIMARY_TIMEFRAME", "1h").strip()
CONFIRM_TIMEFRAME = os.getenv("CONFIRM_TIMEFRAME", "4h").strip()
DAILY_TIMEFRAME = os.getenv("DAILY_TIMEFRAME", "1d").strip()

# ─── Signal Scoring ───────────────────────────────────────────────────────────
MIN_SIGNAL_SCORE = int(os.getenv("MIN_SIGNAL_SCORE", "70"))
AUTO_WEIGHT_FEEDBACK_ENABLED = _env_bool("AUTO_WEIGHT_FEEDBACK_ENABLED", True)
AUTO_WEIGHT_MIN_CLOSED_TRADES = int(os.getenv("AUTO_WEIGHT_MIN_CLOSED_TRADES", "20"))
AUTO_WEIGHT_RECALIBRATE_EVERY = int(os.getenv("AUTO_WEIGHT_RECALIBRATE_EVERY", "5"))
AUTO_WEIGHT_MIN_INDICATOR_TRADES = int(os.getenv("AUTO_WEIGHT_MIN_INDICATOR_TRADES", "8"))
AUTO_WEIGHT_MIN_EDGE_PCT = float(os.getenv("AUTO_WEIGHT_MIN_EDGE_PCT", "3.0"))
AUTO_WEIGHT_LEARNING_RATE = float(os.getenv("AUTO_WEIGHT_LEARNING_RATE", "0.6"))
AUTO_WEIGHT_MAX_STEP_PCT = float(os.getenv("AUTO_WEIGHT_MAX_STEP_PCT", "0.12"))
AUTO_WEIGHT_MIN_WEIGHT = int(os.getenv("AUTO_WEIGHT_MIN_WEIGHT", "1"))
AUTO_WEIGHT_MAX_WEIGHT = int(os.getenv("AUTO_WEIGHT_MAX_WEIGHT", "40"))
AUTO_WEIGHT_REQUIRE_TELEGRAM_APPROVAL = _env_bool("AUTO_WEIGHT_REQUIRE_TELEGRAM_APPROVAL", False)
AUTO_WEIGHT_TELEGRAM_APPROVE_COMMAND = os.getenv("AUTO_WEIGHT_TELEGRAM_APPROVE_COMMAND", "/approve_tune").strip()
AUTO_WEIGHT_TELEGRAM_REJECT_COMMAND = os.getenv("AUTO_WEIGHT_TELEGRAM_REJECT_COMMAND", "/reject_tune").strip()

# ─── Risk / Reward ────────────────────────────────────────────────────────────
ATR_MULTIPLIER_SL = 2.0
RR_RATIO = 2.0
PARTIAL_TP1_RATIO = 1.0
PARTIAL_TP2_RATIO = 2.0
TRAILING_SL_PCT = 0.015

# ─── Notifications ────────────────────────────────────────────────────────────
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
TELEGRAM_MIN_SECONDS_BETWEEN_MESSAGES = float(os.getenv("TELEGRAM_MIN_SECONDS_BETWEEN_MESSAGES", "1.0"))
TELEGRAM_SIGNAL_ALERT_COOLDOWN_SECONDS = int(os.getenv("TELEGRAM_SIGNAL_ALERT_COOLDOWN_SECONDS", "45"))
TELEGRAM_TRADE_ALERT_COOLDOWN_SECONDS = int(os.getenv("TELEGRAM_TRADE_ALERT_COOLDOWN_SECONDS", "15"))
TELEGRAM_RISK_ALERT_COOLDOWN_SECONDS = int(os.getenv("TELEGRAM_RISK_ALERT_COOLDOWN_SECONDS", "120"))
TELEGRAM_PANIC_ALERT_COOLDOWN_SECONDS = int(os.getenv("TELEGRAM_PANIC_ALERT_COOLDOWN_SECONDS", "60"))
TELEGRAM_EXEC_REJECT_ALERT_COOLDOWN_SECONDS = int(os.getenv("TELEGRAM_EXEC_REJECT_ALERT_COOLDOWN_SECONDS", "45"))
TELEGRAM_ESCALATION_WINDOW_SECONDS = int(os.getenv("TELEGRAM_ESCALATION_WINDOW_SECONDS", "600"))
TELEGRAM_ESCALATION_EVENT_THRESHOLD = int(os.getenv("TELEGRAM_ESCALATION_EVENT_THRESHOLD", "3"))

# ─── Database ─────────────────────────────────────────────────────────────────
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_DB_PATH_RAW = os.getenv("DB_PATH", "trading_bot.db").strip()
_DB_PATH_OBJ = Path(_DB_PATH_RAW)
if not _DB_PATH_OBJ.is_absolute():
	_DB_PATH_OBJ = _PROJECT_ROOT / _DB_PATH_OBJ
DB_PATH = str(_DB_PATH_OBJ.resolve())
DB_RETENTION_DAYS = int(os.getenv("DB_RETENTION_DAYS", "180"))
DASHBOARD_ONLY_CURRENT_SESSION = _env_bool("DASHBOARD_ONLY_CURRENT_SESSION", False)

# ─── Scheduler ────────────────────────────────────────────────────────────────
SCAN_INTERVAL_SECONDS = int(os.getenv("SCAN_INTERVAL_SECONDS", "300"))
WS_MAX_SYMBOLS = int(os.getenv("WS_MAX_SYMBOLS", "6"))
SIGNAL_QUEUE_MAXSIZE = int(os.getenv("SIGNAL_QUEUE_MAXSIZE", "200"))
SIGNAL_DEDUP_COOLDOWN_SECONDS = int(os.getenv("SIGNAL_DEDUP_COOLDOWN_SECONDS", "1800"))
SIGNAL_DEDUP_MAX_SCORE_DELTA = int(os.getenv("SIGNAL_DEDUP_MAX_SCORE_DELTA", "2"))
SIGNAL_DEDUP_MIN_PRICE_MOVE_PCT = float(os.getenv("SIGNAL_DEDUP_MIN_PRICE_MOVE_PCT", "0.0015"))
WS_WATCHDOG_TIMEOUT_SECONDS = int(os.getenv("WS_WATCHDOG_TIMEOUT_SECONDS", "180"))
WS_WATCHDOG_MAX_TIMEOUTS = int(os.getenv("WS_WATCHDOG_MAX_TIMEOUTS", "3"))
TRADE_COOLDOWN_MINUTES = int(os.getenv("TRADE_COOLDOWN_MINUTES", "0"))
TRADE_COOLDOWN_CANDLES = int(os.getenv("TRADE_COOLDOWN_CANDLES", "3"))

# ─── Execution Safety Guards ───────────────────────────────────────────────────
MIN_ORDER_NOTIONAL_USDT = float(os.getenv("MIN_ORDER_NOTIONAL_USDT", "10"))
MAX_ORDER_NOTIONAL_USDT = float(
	os.getenv("MAX_ORDER_NOTIONAL_USDT", str(TOTAL_CAPITAL_USDT * MAX_CAPITAL_PER_TRADE_PCT))
)
MAX_ENTRY_SPREAD_PCT = float(os.getenv("MAX_ENTRY_SPREAD_PCT", "0.002"))
MAX_SLIPPAGE_PCT = float(os.getenv("MAX_SLIPPAGE_PCT", "0.003"))
MAX_SIGNAL_STALENESS_SECONDS = int(os.getenv("MAX_SIGNAL_STALENESS_SECONDS", "180"))
LIVE_RECONCILE_MAX_UNSYNC = int(os.getenv("LIVE_RECONCILE_MAX_UNSYNC", "2"))
LIVE_RECONCILE_HARD_FAIL = _env_bool("LIVE_RECONCILE_HARD_FAIL", False)

# ─── Paper Simulation Realism ─────────────────────────────────────────────────
PAPER_SLIPPAGE_PCT = float(os.getenv("PAPER_SLIPPAGE_PCT", "0.0005"))
PAPER_FEE_PCT = float(os.getenv("PAPER_FEE_PCT", "0.001"))
