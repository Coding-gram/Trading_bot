import sys

lines = []
with open('diag_out.txt', 'r', encoding='utf-8', errors='replace') as f:
    for line in f:
        if "LIVE SIGNAL SCAN" in line or lines:
            lines.append(line.strip())

for line in lines[:100]:
    print(line)
