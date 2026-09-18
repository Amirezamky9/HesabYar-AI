# -*- coding: utf-8 -*-
"""بررسی وجود همه SKILL.md ها"""
import json
import sys
from pathlib import Path

root = Path(__file__).parent.parent
path = root / "metadata.json"

with open(path, "r", encoding="utf-8") as f:
    data = json.load(f)

missing = []
for std in data["standards"]:
    slug = std["slug"]
    skill = root / "standards" / slug / "SKILL.md"
    if not skill.exists():
        missing.append(slug)

if missing:
    print("Missing SKILL.md files:")
    for m in missing:
        print(f"  - {m}")
    sys.exit(1)

print(f"OK: All {len(data['standards'])} SKILL.md files found")