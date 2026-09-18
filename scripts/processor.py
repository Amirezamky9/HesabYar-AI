# -*- coding: utf-8 -*-
"""پردازش Markdown و تولید SKILL.md"""
import logging
import re
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)


# ==================== استخراج تیترها ====================
def extract_headings(md_text: str) -> List[Dict]:
    """استخراج همه تیترها با سطح و شماره خط"""
    headings = []
    for i, line in enumerate(md_text.split("\n"), 1):
        m = re.match(r"^(#{1,6})\s+(.+)$", line.strip())
        if m:
            headings.append({
                "level": len(m.group(1)),
                "title": m.group(2).strip(),
                "line": i,
            })
    return headings


# ==================== استخراج بخش‌ها ====================
def extract_sections(md_text: str) -> Dict[str, str]:
    """استخراج بخش‌های اصلی بر اساس تیترهای سطح ۱"""
    sections = {}
    lines = md_text.split("\n")
    current_section = None
    current_content = []

    for line in lines:
        m = re.match(r"^#\s+(.+)$", line.strip())
        if m:
            # ذخیره بخش قبلی
            if current_section:
                sections[current_section] = "\n".join(current_content).strip()
            current_section = m.group(1).strip()
            current_content = []
        elif current_section:
            current_content.append(line)

    # ذخیره آخرین بخش
    if current_section:
        sections[current_section] = "\n".join(current_content).strip()

    return sections


# ==================== استخراج بندها ====================
def extract_clauses(md_text: str) -> List[Dict]:
    """استخراج بندهای شماره‌دار (مثل 1. ، 2. ، 3.)"""
    clauses = []
    lines = md_text.split("\n")
    current_clause = None
    current_text = []

    # الگوی بند: شروع خط با عدد و نقطه
    clause_pattern = re.compile(r"^(\d+)\s*[\.\-]\s*(.*)$")

    for line in lines:
        stripped = line.strip()
        if not stripped:
            if current_clause:
                current_text.append("")
            continue

        m = clause_pattern.match(stripped)
        if m:
            # ذخیره بند قبلی
            if current_clause:
                clauses.append({
                    "number": current_clause,
                    "text": "\n".join(current_text).strip(),
                })
            current_clause = int(m.group(1))
            current_text = [m.group(2)]
        elif current_clause:
            current_text.append(stripped)

    # ذخیره آخرین بند
    if current_clause:
        clauses.append({
            "number": current_clause,
            "text": "\n".join(current_text).strip(),
        })

    # حذف بندهای خیلی کوتاه یا تکراری
    seen = set()
    unique = []
    for c in clauses:
        if c["number"] in seen:
            continue
        seen.add(c["number"])
        if len(c["text"]) > 5:
            unique.append(c)

    return unique


# ==================== تولید SKILL.md ====================
def generate_skill_md(
    number: int,
    title: str,
    md_text: str,
    ifrs_info: Optional[Dict] = None,
) -> str:
    """تولید محتوای SKILL.md"""
    ifrs_info = ifrs_info or {}

    sections = extract_sections(md_text)
    clauses = extract_clauses(md_text)
    headings = extract_headings(md_text)

    # ========== YAML frontmatter ==========
    fm_lines = ["---"]
    fm_lines.append(f"name: accounting-standard-{number:02d}")
    fm_lines.append(f"title: {title}")
    fm_lines.append(f"standard-number: {number}")
    fm_lines.append(f"revision: 1397")
    if ifrs_info.get("ifrs"):
        fm_lines.append(f"ifrs-equivalent: {ifrs_info['ifrs']}")
    if ifrs_info.get("title_en"):
        fm_lines.append(f"title-en: {ifrs_info['title_en']}")
    if ifrs_info.get("phase"):
        fm_lines.append(f"phase: {ifrs_info['phase']}")
    fm_lines.append("status: active")
    fm_lines.append("source: https://thdorsan.com/sam/codificated-standards")
    fm_lines.append(f"headings-count: {len(headings)}")
    fm_lines.append(f"clauses-count: {len(clauses)}")
    fm_lines.append(f"sections-count: {len(sections)}")
    fm_lines.append("---")
    fm_lines.append("")

    # ========== بدنه ==========
    body = [f"# استاندارد حسابداری {number}: {title}", ""]

    # هدف
    if "هدف" in sections:
        body.append("## هدف")
        body.append("")
        body.append(sections["هدف"][:3000])
        body.append("")

    # دامنه کاربرد
    if "دامنه کاربرد" in sections or "دامنه‌ کاربرد" in sections:
        key = "دامنه کاربرد" if "دامنه کاربرد" in sections else "دامنه‌ کاربرد"
        body.append("## دامنه کاربرد")
        body.append("")
        body.append(sections[key][:2000])
        body.append("")

    # تعاریف
    if "تعاریف" in sections or "تعاريف" in sections:
        key = "تعاریف" if "تعاریف" in sections else "تعاريف"
        body.append("## تعاریف کلیدی")
        body.append("")
        body.append(sections[key][:3000])
        body.append("")

    # فهرست تیترها
    if headings:
        body.append("## فهرست عناوین")
        body.append("")
        for h in headings[:40]:
            indent = "  " * (h["level"] - 1)
            body.append(f"{indent}- {h['title']}")
        body.append("")

    # بندهای کلیدی
    if clauses:
        body.append("## بندهای کلیدی")
        body.append("")
        for c in clauses[:50]:
            text = c["text"].replace("\n", " ")[:300]
            body.append(f"- **بند {c['number']}:** {text}")
        body.append("")

    # ارتباط با سایر استانداردها
    body.append("## ارتباط با سایر استانداردها")
    body.append("")
    body.append("(نیاز به تکمیل دستی)")
    body.append("")

    # قواعد نرم‌افزاری
    body.append("## قواعد نرم‌افزاری")
    body.append("")
    body.append("```yaml")
    body.append("validation_rules:")
    body.append(f"  - standard: {number}")
    body.append("  - rules:")
    body.append("      - (نیاز به تعریف)")
    body.append("```")
    body.append("")

    # متن کامل در پیوست
    body.append("## متن کامل استاندارد")
    body.append("")
    body.append("<details>")
    body.append("<summary>مشاهده متن کامل (کلیک کنید)</summary>")
    body.append("")
    body.append(md_text[:30000])
    body.append("")
    body.append("</details>")

    return "\n".join(fm_lines) + "\n".join(body)


# ==================== استخراج عنوان ====================
def extract_title(md_text: str) -> str:
    """استخراج عنوان از خطوط اول"""
    lines = md_text.split("\n")[:10]
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or stripped.startswith("|"):
            continue
        # خطی که شامل "استاندارد حسابداری" هست
        if "استاندارد" in stripped:
            continue
        # عنوان اصلی
        if len(stripped) > 3 and len(stripped) < 100:
            return stripped
    # fallback
    m = re.search(r"^#\s+(.+)$", md_text, re.MULTILINE)
    return m.group(1).strip() if m else "استاندارد حسابداری"


# ==================== پردازش یک استاندارد ====================
def process_standard(number: int) -> Optional[Path]:
    """پردازش یک استاندارد"""
    from config import STANDARD_SLUGS, IFRS_MAPPING, STANDARDS_DIR

    slug = STANDARD_SLUGS.get(number)
    if not slug:
        logger.warning(f"No slug for standard {number}")
        return None

    md_path = STANDARDS_DIR / slug / "source" / "standard.md"
    if not md_path.exists():
        logger.warning(f"Markdown not found: {md_path}")
        return None

    md_text = md_path.read_text(encoding="utf-8")
    title = extract_title(md_text)
    ifrs_info = IFRS_MAPPING.get(number, {})

    skill_content = generate_skill_md(number, title, md_text, ifrs_info)

    skill_path = STANDARDS_DIR / slug / "SKILL.md"
    skill_path.write_text(skill_content, encoding="utf-8")
    logger.info(f"Generated: {skill_path}")
    return skill_path


def process_all(numbers: List[int]) -> List[Path]:
    """پردازش همه استانداردها"""
    results = []
    for num in numbers:
        try:
            path = process_standard(num)
            if path:
                results.append(path)
        except Exception as e:
            logger.error(f"Error processing standard {num}: {e}")
    return results


if __name__ == "__main__":
    import sys
    sys.path.insert(0, str(Path(__file__).parent))

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s"
    )

    print("\nProcessing standard 1...\n")
    path = process_standard(1)

    if path:
        print(f"\nSuccess! Generated: {path}")
    else:
        print("\nFailed!")
        sys.exit(1)
