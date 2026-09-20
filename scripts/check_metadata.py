# -*- coding: utf-8 -*-
"""Validate metadata.json and current-period applicability gates."""
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
    print(f"ERROR: Expected 35 catalog records, got {count}")
    sys.exit(1)

standards = {item["number"]: item for item in data.get("standards", [])}

required = {15, 43, 44}
missing = required - standards.keys()
if missing:
    print(f"ERROR: Missing required current-period metadata records: {sorted(missing)}")
    sys.exit(1)

s15 = standards[15]
if s15.get("status") != "blocked_current_source_gap":
    print("ERROR: Standard 15 must remain blocked for 1405+ until revised-1404 official source is verified")
    sys.exit(1)
if s15.get("current_from") != "1405/01/01":
    print("ERROR: Standard 15 current-source gate must start at 1405/01/01")
    sys.exit(1)

s43 = standards[43]
if s43.get("effective_from") != "1404/01/01" or s43.get("supersedes") != [3, 9, 29]:
    print("ERROR: Standard 43 effective-date/supersession metadata is incorrect")
    sys.exit(1)

s44 = standards[44]
if s44.get("effective_from") != "1405/01/01" or s44.get("supersedes") != [21]:
    print("ERROR: Standard 44 effective-date/supersession metadata is incorrect")
    sys.exit(1)

if data.get("current_rules_baseline") != "HYA-RULES-1405-01":
    print("ERROR: metadata.json is not linked to the current 1405 rules baseline")
    sys.exit(1)

print("OK: metadata.json current-period gates are valid")
