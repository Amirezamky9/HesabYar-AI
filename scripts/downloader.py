# -*- coding: utf-8 -*-
"""دانلود فایل‌های Word و PDF"""
import logging
import time
import requests
from pathlib import Path
from typing import Dict
from tqdm import tqdm
from config import (
    STANDARDS_DIR, STANDARD_SLUGS, USER_AGENT,
    REQUEST_TIMEOUT, MAX_RETRIES, RETRY_DELAY
)

logger = logging.getLogger(__name__)


def get_standard_dir(number: int) -> Path:
    """مسیر پوشه استاندارد"""
    slug = STANDARD_SLUGS.get(number, f"{number:02d}-standard")
    return STANDARDS_DIR / slug


def _get_extension(url: str) -> str:
    """پسوند فایل از URL"""
    if url.lower().endswith(".docx"):
        return ".docx"
    if url.lower().endswith(".doc"):
        return ".doc"
    if url.lower().endswith(".pdf"):
        return ".pdf"
    return ""


def download_file(url: str, dest: Path, description: str = "") -> bool:
    """دانلود یک فایل با retry"""
    if not url:
        logger.warning(f"Empty URL for {description}")
        return False

    if dest.exists() and dest.stat().st_size > 0:
        logger.info(f"Already downloaded: {dest.name}")
        return True

    dest.parent.mkdir(parents=True, exist_ok=True)
    headers = {"User-Agent": USER_AGENT}

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            response = requests.get(
                url, headers=headers, timeout=REQUEST_TIMEOUT, stream=True
            )
            response.raise_for_status()

            total = int(response.headers.get("content-length", 0))
            with open(dest, "wb") as f:
                with tqdm(
                    desc=description,
                    total=total,
                    unit="B",
                    unit_scale=True,
                    unit_divisor=1024,
                    leave=False,
                ) as bar:
                    for chunk in response.iter_content(chunk_size=8192):
                        f.write(chunk)
                        bar.update(len(chunk))

            logger.info(f"Downloaded: {dest.name}")
            return True

        except Exception as e:
            logger.warning(f"Attempt {attempt}/{MAX_RETRIES} failed: {e}")
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_DELAY)

    logger.error(f"Failed after {MAX_RETRIES} attempts: {url}")
    return False


def download_standard(std: Dict) -> Dict[str, bool]:
    """دانلود Word و PDF یک استاندارد"""
    number = std["number"]
    folder = get_standard_dir(number)
    source_dir = folder / "source"
    source_dir.mkdir(parents=True, exist_ok=True)

    results = {}

    # Word
    word_url = std.get("word_url", "")
    word_ext = _get_extension(word_url) or ".doc"
    results["word"] = download_file(
        word_url,
        source_dir / f"standard{word_ext}",
        f"[{number}] Word"
    )

    # PDF
    pdf_url = std.get("pdf_url", "")
    results["pdf"] = download_file(
        pdf_url,
        source_dir / "standard.pdf",
        f"[{number}] PDF"
    )

    return results


def download_all(standards: list) -> Dict[int, Dict[str, bool]]:
    """دانلود همه استانداردها"""
    results = {}
    for std in standards:
        results[std["number"]] = download_standard(std)
    return results


if __name__ == "__main__":
    import sys
    sys.path.insert(0, str(Path(__file__).parent))
    import scraper

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s"
    )

    print("Fetching standards list...")
    standards = scraper.scrape()

    print(f"\nDownloading {len(standards)} standards...\n")
    results = download_all(standards)

    ok = sum(1 for r in results.values() if r.get("word") and r.get("pdf"))
    print(f"\nComplete: {ok}/{len(standards)} standards")
