# -*- coding: utf-8 -*-
"""تبدیل Word به Markdown - نسخه python-docx"""
import logging
import re
import subprocess
import shutil
from pathlib import Path

logger = logging.getLogger(__name__)

SOFFICE_PATHS = [
    r"C:\Program Files\LibreOffice\program\soffice.exe",
    r"C:\Program Files (x86)\LibreOffice\program\soffice.exe",
]


def find_soffice() -> str:
    for path in SOFFICE_PATHS:
        if Path(path).exists():
            return path
    return shutil.which("soffice")


def find_word_file(source_dir: Path) -> Path:
    for ext in [".docx", ".doc"]:
        for f in source_dir.glob(f"standard{ext}"):
            return f
    return None


def convert_doc_to_docx(doc_path: Path, docx_path: Path) -> bool:
    if docx_path.exists():
        logger.info(f"Already converted: {docx_path.name}")
        return True

    soffice = find_soffice()
    if not soffice:
        logger.error("LibreOffice not found!")
        return False

    try:
        result = subprocess.run(
            [soffice, "--headless", "--convert-to", "docx",
             "--outdir", str(docx_path.parent), str(doc_path)],
            capture_output=True, text=True, timeout=120,
        )
        if result.returncode != 0:
            logger.error(f"LibreOffice error: {result.stderr}")
            return False

        expected = docx_path.parent / (doc_path.stem + ".docx")
        if expected.exists() and expected != docx_path:
            expected.rename(docx_path)

        if docx_path.exists():
            logger.info(f"Converted: {doc_path.name} -> {docx_path.name}")
            return True
        logger.error(f"Output not created: {docx_path}")
        return False

    except subprocess.TimeoutExpired:
        logger.error("LibreOffice timeout")
        return False
    except Exception as e:
        logger.error(f"Conversion error: {e}")
        return False


def clean_text(text: str) -> str:
    """پاکسازی متن از کاراکترهای اضافی"""
    if not text:
        return ""
    # حذف {dir="rtl"} و []
    text = re.sub(r'\{dir="rtl"\}', '', text)
    text = re.sub(r'\[\]\{dir="rtl"\}', '', text)
    text = re.sub(r'\[([^\]]+)\]\{dir="rtl"\}', r'\1', text)
    text = re.sub(r'\[\]', '', text)
    # فاصله‌های اضافی
    text = re.sub(r'\s+', ' ', text).strip()
    return text


def get_heading_level(paragraph) -> int:
    """تشخیص سطح تیتر از استایل Word"""
    try:
        style_name = paragraph.style.name if paragraph.style else ""
        if not style_name:
            return 0
        # Heading 1, Heading 2, ...
        m = re.match(r"Heading (\d+)", style_name, re.IGNORECASE)
        if m:
            return int(m.group(1))
        # Title
        if "title" in style_name.lower():
            return 1
    except Exception:
        pass
    return 0


def table_to_markdown(table) -> str:
    """تبدیل جدول Word به Markdown"""
    rows = []
    for row in table.rows:
        cells = [clean_text(cell.text).replace("|", "\\|") for cell in row.cells]
        rows.append(cells)

    if not rows:
        return ""

    # حذف ردیف‌های خالی
    rows = [r for r in rows if any(c for c in r)]
    if not rows:
        return ""

    max_cols = max(len(r) for r in rows)
    lines = []

    # هدر
    header = rows[0] + [""] * (max_cols - len(rows[0]))
    lines.append("| " + " | ".join(header) + " |")
    lines.append("|" + "|".join(["---"] * max_cols) + "|")

    # بدنه
    for row in rows[1:]:
        row = row + [""] * (max_cols - len(row))
        lines.append("| " + " | ".join(row) + " |")

    return "\n".join(lines) + "\n"


def extract_markdown(docx_path: Path, md_path: Path) -> bool:
    """استخراج Markdown از docx با python-docx"""
    try:
        from docx import Document
        from docx.table import Table
        from docx.text.paragraph import Paragraph
    except ImportError:
        logger.error("python-docx not installed! pip install python-docx")
        return False

    try:
        doc = Document(str(docx_path))
    except Exception as e:
        logger.error(f"Cannot open docx: {e}")
        return False

    output = []
    body = doc.element.body

    for child in body.iterchildren():
        tag = child.tag.split("}")[-1] if "}" in child.tag else child.tag

        if tag == "p":
            para = Paragraph(child, doc)
            text = clean_text(para.text)
            if not text:
                output.append("")
                continue

            level = get_heading_level(para)
            if level > 0:
                output.append(f"{'#' * level} {text}")
            else:
                output.append(text)
            output.append("")

        elif tag == "tbl":
            table = Table(child, doc)
            md_table = table_to_markdown(table)
            if md_table:
                output.append(md_table)
                output.append("")

    text = "\n".join(output)
    text = re.sub(r"\n{3,}", "\n\n", text)

    md_path.write_text(text, encoding="utf-8")
    logger.info(f"Extracted to Markdown: {md_path.name}")
    return True


def convert_standard(number: int, slug: str) -> bool:
    from config import STANDARDS_DIR
    source_dir = STANDARDS_DIR / slug / "source"
    md = source_dir / "standard.md"
    docx = source_dir / "standard.docx"

    word_file = find_word_file(source_dir)
    if not word_file:
        logger.warning(f"No Word file in {source_dir}")
        return False

    if word_file.suffix.lower() == ".doc":
        logger.info("Found .doc, converting to .docx...")
        if not convert_doc_to_docx(word_file, docx):
            return False
        word_file = docx

    # حذف md قدیمی
    if md.exists():
        md.unlink()
        logger.info(f"Removed old: {md.name}")

    return extract_markdown(word_file, md)


if __name__ == "__main__":
    import sys
    sys.path.insert(0, str(Path(__file__).parent))
    from config import STANDARD_SLUGS

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s"
    )

    print("\nConverting standard 1...\n")
    slug = STANDARD_SLUGS[1]
    success = convert_standard(1, slug)

    if success:
        print("\nSuccess!")
    else:
        print("\nFailed!")
        sys.exit(1)
