# -*- coding: utf-8 -*-
"""اسکریپت اصلی - اجرای کل pipeline"""
import argparse
import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from config import LOGS_DIR, STANDARD_SLUGS
import scraper
import downloader
import converter
import processor
import metadata_generator


def setup_logging(verbose: bool = False) -> None:
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
        handlers=[
            logging.FileHandler(LOGS_DIR / "pipeline.log", encoding="utf-8"),
            logging.StreamHandler(),
        ],
    )


def run_pipeline(only: list = None, skip: dict = None) -> None:
    skip = skip or {}
    logger = logging.getLogger("main")
    t0 = time.time()

    # ۱. استخراج
    logger.info("=" * 60)
    logger.info("STEP 1: Scraping standards list")
    logger.info("=" * 60)
    standards = scraper.scrape()
    if only:
        standards = [s for s in standards if s["number"] in only]
        logger.info(f"Filtered to {len(standards)} standards")

    # ۲. دانلود
    if not skip.get("download"):
        logger.info("=" * 60)
        logger.info("STEP 2: Downloading files")
        logger.info("=" * 60)
        downloader.download_all(standards)

    # ۳. تبدیل
    if not skip.get("convert"):
        logger.info("=" * 60)
        logger.info("STEP 3: Converting to Markdown")
        logger.info("=" * 60)
        for std in standards:
            slug = STANDARD_SLUGS.get(std["number"])
            if slug:
                try:
                    converter.convert_standard(std["number"], slug)
                except Exception as e:
                    logger.error(f"Error converting {std['number']}: {e}")

    # ۴. پردازش
    if not skip.get("process"):
        logger.info("=" * 60)
        logger.info("STEP 4: Generating SKILL.md files")
        logger.info("=" * 60)
        numbers = [s["number"] for s in standards]
        processor.process_all(numbers)

    # ۵. متادیتا
    if not skip.get("metadata"):
        logger.info("=" * 60)
        logger.info("STEP 5: Generating metadata")
        logger.info("=" * 60)
        metadata = metadata_generator.generate_metadata(standards)
        metadata_generator.save_metadata(metadata)
        metadata_generator.generate_mapping_md(metadata)

    elapsed = time.time() - t0
    logger.info("=" * 60)
    logger.info(f"DONE in {elapsed:.1f} seconds")
    logger.info("=" * 60)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--only", type=int, nargs="+", help="Only these standards")
    parser.add_argument("--skip-download", action="store_true")
    parser.add_argument("--skip-convert", action="store_true")
    parser.add_argument("--skip-process", action="store_true")
    parser.add_argument("--skip-metadata", action="store_true")
    parser.add_argument("-v", "--verbose", action="store_true")

    args = parser.parse_args()
    setup_logging(args.verbose)

    try:
        run_pipeline(
            only=args.only,
            skip={
                "download": args.skip_download,
                "convert": args.skip_convert,
                "process": args.skip_process,
                "metadata": args.skip_metadata,
            },
        )
    except KeyboardInterrupt:
        logging.warning("Interrupted by user")
        sys.exit(1)
    except Exception as e:
        logging.exception(f"Unexpected error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
