import sqlite3

conn = sqlite3.connect('trading_bot.db')
cursor = conn.cursor()

# Get all tables
cursor.execute("SELECT name FROM sqlite_master WHERE type='table';")
print("Tables:", cursor.fetchall())

# Get paper trades
try:
    cursor.execute("SELECT * FROM paper_trades WHERE status='open';")
    rows = cursor.fetchall()
    
    # Get column names
    cursor.execute("PRAGMA table_info(paper_trades)")
    cols = [c[1] for c in cursor.fetchall()]
    
    print(f"\nOpen positions: {len(rows)}")
    for r in rows:
        print(dict(zip(cols, r)))
except Exception as e:
    print("Error:", e)
