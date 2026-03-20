import sys
print("Python:", sys.version)

try:
    from bot.data.fetcher import get_exchange
    print("OK: bot.data.fetcher")
except Exception as e:
    print(f"FAIL: bot.data.fetcher - {e}")

try:
    from bot.execution.paper_trade import PaperTradeEngine
    print("OK: bot.execution.paper_trade")
except Exception as e:
    print(f"FAIL: bot.execution.paper_trade - {e}")

try:
    from bot.risk.risk_manager import calculate_take_profits
    # Test Fibonacci SHORT fix
    tp = calculate_take_profits(2000, 2100, "short", {"extensions": {"1.272": 1800}})
    print(f"OK: risk_manager - short TP1={tp['tp1']:.2f} TP2={tp['tp2']:.2f}")
except Exception as e:
    print(f"FAIL: bot.risk.risk_manager - {e}")

try:
    from api.server import app
    print("OK: api.server")
except Exception as e:
    print(f"FAIL: api.server - {e}")
