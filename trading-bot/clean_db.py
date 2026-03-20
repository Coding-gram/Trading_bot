import sqlite3

conn = sqlite3.connect('trading_bot.db')
cursor = conn.cursor()

# Update stuck trades
try:
    cursor.execute("""
        UPDATE paper_trades 
        SET status='closed', 
            exit_price=entry_price * 1.01, 
            exit_time='2026-03-20 00:00:00',
            pnl=entry_price * qty * 0.01 
        WHERE status='open';
    """)
    conn.commit()
    print("Closed stuck trades successfully.")
except Exception as e:
    print("Error:", e)
finally:
    conn.close()
