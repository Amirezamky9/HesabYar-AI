# -*- coding: utf-8 -*-
"""استخراج لینک‌های دانلود از صفحه منبع"""
import logging
import requests
from bs4 import BeautifulSoup
from typing import List, Dict
from urllib.parse import urljoin
from config import SOURCE_URL, USER_AGENT, REQUEST_TIMEOUT

logger = logging.getLogger(__name__)


def fetch_page(url: str) -> str:
    """دریافت HTML صفحه"""
    headers = {"User-Agent": USER_AGENT}
    response = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    response.encoding = "utf-8"
    return response.text


def _extract_link(cell, base_url: str) -> str:
    """استخراج لینک از یک سلول جدول"""
    a_tag = cell.find("a")
    if not a_tag or not a_tag.get("href"):
        return ""
    return urljoin(base_url, a_tag["href"])


def parse_standards(html: str, base_url: str) -> List[Dict]:
    """استخراج اطلاعات استانداردها از جدول"""
    soup = BeautifulSoup(html, "lxml")
    standards = []

    for table in soup.find_all("table"):
        for row in table.find_all("tr"):
            cells = row.find_all("td")
            if len(cells) < 4:
                continue

            num_text = cells[0].get_text(strip=True)
            if not num_text.isdigit():
                continue
            number = int(num_text)

            title = cells[1].get_text(strip=True)
            word_link = _extract_link(cells[2], base_url)
            pdf_link = _extract_link(cells[3], base_url)

            if not (word_link or pdf_link):
                logger.warning(f"No link for standard {number}")
                continue

            standards.append({
                "number": number,
                "title": title,
                "word_url": word_link,
                "pdf_url": pdf_link,
            })

    seen = set()
    unique = []
    for std in standards:
        if std["number"] not in seen:
            seen.add(std["number"])
            unique.append(std)

    logger.info(f"Found {len(unique)} standards")
    return sorted(unique, key=lambda x: x["number"])


def scrape() -> List[Dict]:
    """تابع اصلی استخراج"""
    logger.info(f"Fetching: {SOURCE_URL}")
    html = fetch_page(SOURCE_URL)
    return parse_standards(html, SOURCE_URL)


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s"
    )
    standards = scrape()
    print("=" * 70)
    print(f"Found {len(standards)} standards")
    print("=" * 70)
    for s in standards:
        print(f"[{s['number']:2d}] {s['title'][:50]}")
        print(f"     Word: {s['word_url'][:80]}")
        print(f"     PDF:  {s['pdf_url'][:80]}")
        print()
