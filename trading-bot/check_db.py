import sqlite3
c = sqlite3.connect('trading_bot.db')
rows = c.execute('SELECT COUNT(*) FROM paper_trades WHERE status="open"').fetchone()
print('Remaining open positions:', rows[0])
c.close()
