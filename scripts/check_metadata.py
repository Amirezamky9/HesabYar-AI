# -*- coding: utf-8 -*-
"""بررسی صحت metadata.json"""
import json
import sys
from pathlib import Path

path = Path(__file__).parent.parent / "metadata.json"

if not path.exists():
    print(f"ERROR: {path} not found")
    sys.exit(1)

with open(path, "r", encoding="utf-8") as f:
    data = json.load(f)

count = data.get("standards_count", 0)
print(f"Standards count: {count}")

if count != 35:
    print(f"ERROR: Expected 35, got {count}")
    sys.exit(1)

print("OK: metadata.json is valid")