import sqlite3
import os

db_path = 'trading_bot.db'
if not os.path.exists(db_path):
    print(f"{db_path} not found.")
    exit(1)

try:
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    # Get all tables
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table';")
    tables = [t[0] for t in cursor.fetchall() if t[0] != 'sqlite_sequence']
    print(f"Tables found: {tables}")

    for t in tables:
        cursor.execute(f"DELETE FROM {t};")
        print(f"Cleared table: {t}")

    conn.commit()
    cursor.execute("VACUUM;") # Reclaim space
    conn.commit()
    
    print("All existing trades cleared successfully. The database schema was not changed.")
except Exception as e:
    print(f"Error: {e}")
finally:
    if 'conn' in locals():
        conn.close()
