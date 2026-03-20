import sqlite3
import os

DB_PATH = 'trading_bot.db'
if os.path.exists(DB_PATH):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("UPDATE paper_trades SET score=77 WHERE symbol='DOT/USDT' AND status='open'")
    conn.commit()
    conn.close()
    print("DOT/USDT score updated to 77")
