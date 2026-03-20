"""Diagnostic — reads bot.log cleanly (no live scan)."""
import sys, os

log_path = os.path.join(os.path.dirname(__file__), "bot.log")
with open(log_path, encoding="utf-8", errors="replace") as f:
    lines = f.readlines()

# Show last 200 lines
print("".join(lines[-200:]))
