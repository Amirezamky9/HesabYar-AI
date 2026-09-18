# -*- coding: utf-8 -*-
"""تولید metadata.json و mappings"""
import json
import logging
from datetime import datetime
from pathlib import Path
from config import (
    IFRS_MAPPING, STANDARD_SLUGS, METADATA_FILE,
    MAPPING_FILE, STANDARDS_DIR, SOURCE_URL
)

logger = logging.getLogger(__name__)


PHASE_NAMES = {
    1: "صورت‌های مالی پایه",
    2: "دارایی‌ها",
    3: "بدهی‌ها و تعهدات",
    4: "درآمد و ارز",
    5: "تلفیق و سرمایه‌گذاری‌ها",
    6: "افشا و گزارشگری خاص",
    7: "صنایع خاص",
    8: "ابزارهای مالی",
}


def generate_metadata(standards: list) -> dict:
    """تولید متادیتای کامل"""
    metadata = {
        "name": "accounting-iran-standards",
        "version": "1.0.0",
        "generated_at": datetime.now().isoformat(),
        "source_url": SOURCE_URL,
        "standards_count": len(standards),
        "phases": {str(k): v for k, v in PHASE_NAMES.items()},
        "standards": [],
    }

    for std in sorted(standards, key=lambda x: x["number"]):
        num = std["number"]
        info = IFRS_MAPPING.get(num, {})
        slug = STANDARD_SLUGS.get(num, f"{num:02d}-standard")
        skill_path = STANDARDS_DIR / slug / "SKILL.md"
        md_path = STANDARDS_DIR / slug / "source" / "standard.md"

        metadata["standards"].append({
            "number": num,
            "title": std["title"],
            "slug": slug,
            "ifrs_equivalent": info.get("ifrs"),
            "title_en": info.get("title_en"),
            "phase": info.get("phase"),
            "phase_name": PHASE_NAMES.get(info.get("phase"), ""),
            "status": "active",
            "has_skill_md": skill_path.exists(),
            "has_markdown": md_path.exists(),
            "word_url": std.get("word_url", ""),
            "pdf_url": std.get("pdf_url", ""),
        })

    return metadata


def save_metadata(metadata: dict) -> None:
    """ذخیره metadata.json"""
    METADATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    METADATA_FILE.write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    logger.info(f"Metadata saved: {METADATA_FILE}")


def generate_mapping_md(metadata: dict) -> None:
    """تولید فایل mapping به IFRS"""
    MAPPING_FILE.parent.mkdir(parents=True, exist_ok=True)

    lines = [
        "# تطابق استانداردهای حسابداری ایران با IFRS\n",
        f"\n> منبع: {metadata['source_url']}\n",
        f"\n> تاریخ تولید: {metadata['generated_at']}\n",
        f"\n> تعداد استانداردها: {metadata['standards_count']}\n\n",
        "## بر اساس فاز پیاده‌سازی\n\n",
    ]

    # گروه‌بندی بر اساس فاز
    by_phase = {}
    for std in metadata["standards"]:
        phase = std.get("phase") or 0
        by_phase.setdefault(phase, []).append(std)

    for phase in sorted(by_phase.keys()):
        lines.append(f"### فاز {phase}: {PHASE_NAMES.get(phase, 'نامشخص')}\n\n")
        lines.append("| شماره | عنوان | IFRS | عنوان انگلیسی |\n")
        lines.append("|-------|-------|------|----------------|\n")
        for std in by_phase[phase]:
            ifrs = std.get("ifrs_equivalent") or "—"
            title_en = std.get("title_en") or "—"
            lines.append(
                f"| {std['number']} | {std['title']} | {ifrs} | {title_en} |\n"
            )
        lines.append("\n")

    # جدول کامل
    lines.append("\n## جدول کامل\n\n")
    lines.append("| شماره | عنوان | IFRS | فاز | SKILL.md |\n")
    lines.append("|-------|-------|------|-----|----------|\n")
    for std in metadata["standards"]:
        ifrs = std.get("ifrs_equivalent") or "—"
        phase = std.get("phase") or "—"
        skill = "✅" if std["has_skill_md"] else "❌"
        lines.append(
            f"| {std['number']} | {std['title']} | {ifrs} | {phase} | {skill} |\n"
        )

    MAPPING_FILE.write_text("".join(lines), encoding="utf-8")
    logger.info(f"Mapping saved: {MAPPING_FILE}")


if __name__ == "__main__":
    import sys
    sys.path.insert(0, str(Path(__file__).parent))
    import scraper

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s"
    )

    print("\nFetching standards...")
    standards = scraper.scrape()

    print(f"\nGenerating metadata for {len(standards)} standards...")
    metadata = generate_metadata(standards)
    save_metadata(metadata)
    generate_mapping_md(metadata)

    print("\nSuccess!")
