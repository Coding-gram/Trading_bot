import sqlite3

DB_PATH = 'trading_bot.db'
conn = sqlite3.connect(DB_PATH)
c = conn.cursor()

# Show current open positions before cleanup
c.execute('SELECT id, symbol, direction, entry_price, stop_loss, tp1, tp2, score FROM paper_trades WHERE status = "open"')
rows = c.fetchall()
print(f'Open positions before cleanup: {len(rows)}')
for r in rows:
    print(f'  {r[1]} {r[2]} entry={r[3]:.4f} sl={r[4]:.4f} tp1={r[5]:.4f} tp2={r[6]:.4f} score={r[7]}')

# Delete all open positions (paper trades only, no real money)
c.execute('DELETE FROM paper_trades WHERE status = "open"')
deleted = c.rowcount
conn.commit()
conn.close()
print(f'\nDeleted {deleted} open positions. DB is now clean.')
