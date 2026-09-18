# -*- coding: utf-8 -*-
"""تنظیمات پروژه - استانداردهای حسابداری ایران"""
from pathlib import Path

# مسیرها
BASE_DIR = Path(__file__).resolve().parent.parent
STANDARDS_DIR = BASE_DIR / "standards"
LOGS_DIR = BASE_DIR / "logs"
METADATA_FILE = BASE_DIR / "metadata.json"
MAPPING_FILE = BASE_DIR / "mappings" / "ifrs-to-iranian.md"

# منبع
SOURCE_URL = "https://thdorsan.com/sam/codificated-standards"

# تنظیمات دانلود
REQUEST_TIMEOUT = 30
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
MAX_RETRIES = 3
RETRY_DELAY = 5

# نگاشت IFRS
IFRS_MAPPING = {
    1:  {"ifrs": "IAS 1",         "title_en": "Presentation of Financial Statements",       "phase": 1},
    2:  {"ifrs": "IAS 7",         "title_en": "Statement of Cash Flows",                     "phase": 1},
    4:  {"ifrs": "IAS 37",        "title_en": "Provisions, Contingent Liabilities",          "phase": 3},
    5:  {"ifrs": "IAS 10",        "title_en": "Events after the Reporting Period",           "phase": 3},
    8:  {"ifrs": "IAS 2",         "title_en": "Inventories",                                 "phase": 2},
    10: {"ifrs": "IAS 20",        "title_en": "Government Grants",                           "phase": 2},
    11: {"ifrs": "IAS 16",        "title_en": "Property, Plant and Equipment",               "phase": 2},
    12: {"ifrs": "IAS 24",        "title_en": "Related Party Disclosures",                   "phase": 6},
    13: {"ifrs": "IAS 23",        "title_en": "Borrowing Costs",                             "phase": 2},
    15: {"ifrs": "IAS 39/IFRS 9", "title_en": "Investments",                                 "phase": 2},
    16: {"ifrs": "IAS 21",        "title_en": "Effects of Changes in Foreign Exchange",      "phase": 4},
    17: {"ifrs": "IAS 38",        "title_en": "Intangible Assets",                           "phase": 2},
    18: {"ifrs": "IAS 27",        "title_en": "Separate Financial Statements",               "phase": 5},
    20: {"ifrs": "IAS 28",        "title_en": "Investments in Associates",                   "phase": 5},
    22: {"ifrs": "IAS 34",        "title_en": "Interim Financial Reporting",                 "phase": 6},
    24: {"ifrs": None,            "title_en": "Pre-operating Entities",                      "phase": 6},
    25: {"ifrs": "IFRS 8",        "title_en": "Segment Reporting",                           "phase": 6},
    26: {"ifrs": "IAS 41",        "title_en": "Agriculture",                                 "phase": 4},
    27: {"ifrs": "IAS 26",        "title_en": "Retirement Benefit Plans",                    "phase": 7},
    28: {"ifrs": "IFRS 4/17",     "title_en": "General Insurance",                           "phase": 7},
    30: {"ifrs": "IAS 33",        "title_en": "Earnings per Share",                          "phase": 6},
    31: {"ifrs": "IFRS 5",        "title_en": "Non-current Assets Held for Sale",            "phase": 6},
    32: {"ifrs": "IAS 36",        "title_en": "Impairment of Assets",                        "phase": 2},
    33: {"ifrs": "IAS 19",        "title_en": "Employee Benefits",                           "phase": 3},
    34: {"ifrs": "IAS 8",         "title_en": "Accounting Policies, Changes, Errors",        "phase": 1},
    35: {"ifrs": "IAS 12",        "title_en": "Income Taxes",                                "phase": 1},
    36: {"ifrs": "IAS 32",        "title_en": "Financial Instruments: Presentation",         "phase": 8},
    37: {"ifrs": "IFRS 7",        "title_en": "Financial Instruments: Disclosures",          "phase": 8},
    38: {"ifrs": "IFRS 3",        "title_en": "Business Combinations",                       "phase": 5},
    39: {"ifrs": "IFRS 10",       "title_en": "Consolidated Financial Statements",           "phase": 5},
    40: {"ifrs": "IFRS 11",       "title_en": "Joint Arrangements",                          "phase": 5},
    41: {"ifrs": "IFRS 12",       "title_en": "Disclosure of Interests in Other Entities",   "phase": 5},
    42: {"ifrs": "IFRS 13",       "title_en": "Fair Value Measurement",                      "phase": 2},
    43: {"ifrs": "IFRS 15",       "title_en": "Revenue from Contracts with Customers",       "phase": 4},
    44: {"ifrs": "IFRS 16",       "title_en": "Leases",                                      "phase": 3},
}

# نگاشت slug پوشه‌ها
STANDARD_SLUGS = {
    1:  "01-presentation",
    2:  "02-cash-flow",
    4:  "04-provisions-contingencies",
    5:  "05-events-after-balance-sheet",
    8:  "08-inventory",
    10: "10-government-grants",
    11: "11-property-plant-equipment",
    12: "12-related-party-disclosures",
    13: "13-borrowing-costs",
    15: "15-investments",
    16: "16-foreign-exchange",
    17: "17-intangible-assets",
    18: "18-separate-financial-statements",
    20: "20-associates",
    22: "22-interim-reporting",
    24: "24-pre-operating-entities",
    25: "25-segment-reporting",
    26: "26-agriculture",
    27: "27-retirement-benefit-plans",
    28: "28-general-insurance",
    30: "30-earnings-per-share",
    31: "31-non-current-assets-held-for-sale",
    32: "32-impairment",
    33: "33-employee-benefits",
    34: "34-changes-in-accounting-policies",
    35: "35-income-taxes",
    36: "36-financial-instruments-presentation",
    37: "37-financial-instruments-disclosure",
    38: "38-business-combinations",
    39: "39-consolidated-financial-statements",
    40: "40-joint-arrangements",
    41: "41-disclosure-of-interests",
    42: "42-fair-value-measurement",
    43: "43-revenue-from-contracts",
    44: "44-leases",
}

# تست
if __name__ == "__main__":
    print("=" * 50)
    print("config.py check")
    print("=" * 50)
    print(f"BASE_DIR: {BASE_DIR}")
    print(f"STANDARDS_DIR: {STANDARDS_DIR}")
    print(f"SOURCE_URL: {SOURCE_URL}")
    print(f"IFRS_MAPPING items: {len(IFRS_MAPPING)}")
    print(f"STANDARD_SLUGS items: {len(STANDARD_SLUGS)}")
    print("=" * 50)
    print("OK")
