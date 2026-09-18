# -*- coding: utf-8 -*-
"""اصلاح خودکار message ها در rules.yaml"""
import re
from pathlib import Path

path = Path(__file__).parent.parent / "validators" / "rules.yaml"
content = path.read_text(encoding="utf-8")


def fix_message(match):
    indent = match.group(1)
    text = match.group(2).strip()
    # اگه قبلاً کوتیشن داره، دست نزن
    if text.startswith('"') or text.startswith("'"):
        return match.group(0)
    # escape کردن کوتیشن‌های داخلی
    text = text.replace('"', '\\"')
    return f'{indent}message: "{text}"'


content = re.sub(
    r'^(\s*)message:\s*(.+)$',
    fix_message,
    content,
    flags=re.MULTILINE
)

path.write_text(content, encoding="utf-8")
print(f"Fixed: {path}")
print(f"Size: {path.stat().st_size} bytes")