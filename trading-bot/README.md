# Trading Bot

## Quick Setup

### 1. Install Python dependencies
```bash
cd trading-bot
pip install -r requirements.txt
```

### 2. Configure your credentials
```bash
cp .env.example .env
# Edit .env with your Binance API keys & Telegram bot token
```

### 3. Run in Paper Mode (Simulation – START HERE)
```bash
python -m bot.main --mode paper
```

From workspace root (path-agnostic launcher):
```bash
python run_bot.py --mode paper
```

### 3a. Live mode (fully automated)
```bash
# Required: arm live mode explicitly
LIVE_CONFIRMATION_REQUIRED_VALUE=I_UNDERSTAND_LIVE_RISK
LIVE_CONFIRMATION_PHRASE=I_UNDERSTAND_LIVE_RISK

# Optional: order type = market (default) or limit
LIVE_ORDER_TYPE=market

# Optional: WS pressure control (extra symbols still scanned via REST)
WS_MAX_SYMBOLS=6

# Optional: signal cadence tuning
# (signals update on PRIMARY_TIMEFRAME candle close)
PRIMARY_TIMEFRAME=15m
CONFIRM_TIMEFRAME=1h
SCAN_INTERVAL_SECONDS=60

# Optional reliability tuning
DB_RETENTION_DAYS=180
SIGNAL_QUEUE_MAXSIZE=200
LIVE_RECONCILE_MAX_UNSYNC=2
LIVE_RECONCILE_HARD_FAIL=false
```

Run live mode:
```bash
python -m bot.main --mode live
```

From workspace root (path-agnostic launcher):
```bash
python run_bot.py --mode live
```

The bot now runs `health_gate.py` automatically at startup and blocks launch if checks fail.
To bypass manually (not recommended):
```bash
python -m bot.main --mode paper --skip-health-gate
```

### 3b. Deterministic score replay check (recommended before deploy)
```bash
# Capture fixed OHLCV snapshot + baseline scores
python score_replay_check.py capture --limit 8

# Replay from captured snapshot to verify scoring drift
python score_replay_check.py replay --snapshot-dir replay_artifacts/<snapshot_folder>
```

### 3e. Strategy robustness check (walk-forward + Monte Carlo)
```bash
python robustness_check.py --symbols-limit 6 --n-candles 700 --folds 4 --mc-paths 2000 --output robustness_report.json
```

This writes a robustness report with per-symbol validation metrics and Monte Carlo risk estimates
(`p_loss`, `p_ruin_10pct`, `p_drawdown_20pct`) so strategy quality is evaluated beyond pass/fail tests.

### 3c. Pre-run health gate (recommended)
```bash
python health_gate.py
```

Optional: enforce strategy-quality thresholds as part of health gate.
By default this is disabled to keep startup fast; enable when you want a strict deploy gate.
```bash
ROBUSTNESS_GATE_ENABLED=true
ROBUSTNESS_GATE_SYMBOLS_LIMIT=6
ROBUSTNESS_GATE_N_CANDLES=700
ROBUSTNESS_GATE_FOLDS=4
ROBUSTNESS_GATE_MC_PATHS=1200
ROBUSTNESS_GATE_SIGNAL_SCORE_OVERRIDE=58
ROBUSTNESS_GATE_TREND_WEIGHT_MULT=1.0
ROBUSTNESS_GATE_MACD_WEIGHT_MULT=1.0
ROBUSTNESS_GATE_PENALTY_WEIGHT_MULT=1.1

# Thresholds (9.5-style strict defaults)
ROBUSTNESS_GATE_MIN_TOTAL_TRADES=300
ROBUSTNESS_GATE_MIN_SYMBOL_TRADES=100
ROBUSTNESS_GATE_MIN_SYMBOLS_PASS=3
ROBUSTNESS_GATE_MIN_AVG_PF=1.35
ROBUSTNESS_GATE_MIN_AVG_SHARPE=1.2
ROBUSTNESS_GATE_MAX_AVG_MDD=0.12
ROBUSTNESS_GATE_MAX_P_LOSS=0.45
ROBUSTNESS_GATE_MAX_P_DRAWDOWN_20=0.35
```

### 3d. CI verification gate
GitHub Actions now runs the offline verification suite on push/PR:
- `smoke_test.py`
- `verify_fixes.py`
- `verify_runtime_safety.py`
- `chaos_reconcile_test.py`

Workflow file: `.github/workflows/ci.yml`

### Test isolation note
The test suite uses `tests/conftest.py` to force an isolated temporary SQLite DB
(`DB_PATH`) per test, so tests do not read/write your runtime trading database.

Run tests from `trading-bot/` with:
```bash
python -m pytest -q
```

### 4. Start the API server (for dashboard)
```bash
uvicorn api.server:app --reload --port 8000
```

API stats/open/trade endpoints include both paper and live trade visibility.

By default, dashboard history endpoints show only trades from the current API server session.
Set `DASHBOARD_ONLY_CURRENT_SESSION=false` in `.env` to show full DB history.

### 5. Start the Dashboard
```bash
cd dashboard
npm install
npm run dev
# Open http://localhost:3000
```

---

## Components

| Module | Purpose |
|---|---|
| `bot/data/fetcher.py` | Binance OHLCV data fetcher |
| `bot/analysis/indicators.py` | 12+ technical indicators (EMA, RSI, MACD, ATR…) |
| `bot/analysis/patterns/candlestick.py` | 15+ candlestick patterns |
| `bot/analysis/patterns/chart.py` | 10+ chart patterns (H&S, Double Top, Flags…) |
| `bot/analysis/support_resistance.py` | S/R level detection + Fibonacci levels |
| `bot/strategy/scorer.py` | Multi-factor confluence scorer (0–100) |
| `bot/risk/risk_manager.py` | ATR-SL, Trailing SL, Partial TP, Position Sizing |
| `bot/execution/paper_trade.py` | Paper trading engine |
| `bot/notifications/telegram.py` | Signal + trade alerts |
| `bot/main.py` | Main scheduler/orchestrator |
| `api/server.py` | FastAPI backend for dashboard |
| `dashboard/` | Next.js web dashboard |

## Risk Rules
- Max 2% capital risk per trade
- Max 5 simultaneous trades (diversification)
- Max 20% capital in any single trade
- ATR-based stop loss (adapts to volatility)
- Trailing SL moves to breakeven after TP1 hit
- 50% profit booked at TP1, rest at TP2
- Daily loss limit: 6% → auto-halt
- Max drawdown: 15% → auto-halt
- Min signal confidence: 70/100

## Getting Telegram Alerts
1. Open Telegram, search `@BotFather`
2. Send `/newbot` and follow prompts
3. Copy the token into `.env` → `TELEGRAM_BOT_TOKEN`
4. Open `@userinfobot`, get your chat ID → `TELEGRAM_CHAT_ID`
