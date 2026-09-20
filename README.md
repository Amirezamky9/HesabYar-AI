# 🏛️ HesabYar-AI | حساب‌یار

> **دستیار هوش مصنوعی حسابداری ایران و سرور پروتکل کانتکست مدل (MCP) سامانه مؤدیان**  
> **Iranian AI Accountant & Moadian Tax System MCP Server**

[![Python](https://img.shields.io/badge/Python-3.10%2B-blue)](https://www.python.org/)
[![License](https://img.shields.io/badge/License-MIT-green)](LICENSE)
[![Standards](https://img.shields.io/badge/Standards-35%20Active-orange)](standards/)
[![Validation Rules](https://img.shields.io/badge/Validation%20Rules-36%20Automated-brightgreen)](validators/)
[![IFRS](https://img.shields.io/badge/IFRS-Compatible-purple)](mappings/ifrs-to-iranian.md)
[![MCP Protocol](https://img.shields.io/badge/MCP-Ready-teal)](SKILL.md)
[![Upstream](https://img.shields.io/badge/Upstream-seiahposh%2Faccounting--iran--standards-blueviolet)](https://github.com/seiahposh/accounting-iran-standards)

---

## 📜 Lineage & Acknowledgments | شجره‌نامه متن‌باز و تقدیرنامه

> *"Standing on the shoulders of giants."*  
> *«ایستادن بر شانه‌های غول‌ها: پیشرفت متن‌باز بر پایه کار ارزشمند پیشگامان شکل می‌گیرد.»*

Following the open-source lineage tradition established by projects like **OmniRoute** and **9router**, **HesabYar-AI** proudly acknowledges its origin and foundational architecture.

### Upstream Origin | ریشه پروژه
**HesabYar-AI** is a direct continuation and evolution of [`seiahposh/accounting-iran-standards`](https://github.com/seiahposh/accounting-iran-standards), originally created and authored by **Vohuman (`seiahposh`)**. 

We express our deepest gratitude and highest respect to **Vohuman** for the monumental effort of:
- Curating and structuring a **35-standard Iranian accounting corpus** (استانداردهای حسابداری ایران). Current applicability is now period-aware: Standards 43 and 44 have explicit effective dates, and the bundled Standard 15 source is blocked for 1405+ until its revised-1404 official text is imported and verified.
- Formulating **36 machine-readable validation rules** (`validators/rules.yaml`) across core financial statements.
- Establishing standardized **YAML financial statement templates** (balance sheet, income statement, cash flow, equity changes).
- Mapping Iranian standards to international standards (**IFRS / IAS equivalents**).
- Developing the initial automated scraping, document conversion, and validation pipeline in Python.

### Original Author & Upstream Credits
- **Original Project:** [seiahposh/accounting-iran-standards](https://github.com/seiahposh/accounting-iran-standards)
- **Original Author & Creator:** Vohuman ([@seiahposh](https://github.com/seiahposh))
- **Email:** `seiahposh@yahoo.com`
- **Contact:** `09128005969`
- **License:** MIT License (Copyright © 2026 Vohuman)

All upstream copyright notices, licensing terms, and author attributions are preserved in full accordance with the MIT License.

### External Acknowledgments | سایر تقدیرها
- **سازمان حسابرسی ایران (Audit Organization of Iran):** برای تدوین استانداردهای رسمی حسابداری
- **thdorsan.com:** منبع دریافت اسناد تجدیدنظرشده استانداردهای حسابداری
- **Pandoc & LibreOffice:** ابزارهای تبدیل اسناد استاندارد به Markdown ساختاریافته

---

## 🎯 معرفی پروژه | Project Overview

### فارسی (Persian)
**حساب‌یار (HesabYar-AI)** یک اکوسیستم متن‌باز جامع برای حسابداری هوشمند، انطباق با استانداردهای حسابداری ایران و اتصال مستقیم به **سامانه مؤدیان مالیاتی** از طریق پروتکل کانتکست مدل (**MCP - Model Context Protocol**) است.

هدف حساب‌یار این است که موتور دانشی استانداردهای حسابداری ایران را به مغز متفکر مدل‌های زبانی بزرگ (LLMها نظیر Claude و GPT) متصل کرده و بستری استاندارد، قابل اتکا و ماشین‌خوان برای اتوماسیون حسابداری، حسابرسی، صدور صورتحساب و ارسال به سامانه مؤدیان فراهم کند.

### English
**HesabYar-AI** is an open-source Iranian AI Accountant ecosystem and Model Context Protocol (MCP) server. It equips Large Language Models (Claude, GPT, local LLMs) with deep, structured domain knowledge of Iranian Accounting Standards, automated financial statement validation, and native integration capabilities for the **Moadian Tax System** (سامانه مؤدیان).

---

## 🌟 ارکان و قابلیت‌های کلیدی | Key Pillars & Capabilities

### ۱. 🤖 دستیار هوش مصنوعی حسابداری (AI Accountant Assistant)
- **پرسش و پاسخ تخصصی:** پاسخ به پرسش‌های پیچیده حسابداری، مالیاتی و حسابرسی بر مبنای استانداردهای لازم‌الاجرای ایران.
- **تفسیر و راهنمای استانداردها:** بررسی تفاوت‌های استانداردهای ایران با استانداردهای بین‌المللی گزارشگری مالی (IFRS).
- **ثبت‌های حسابداری خودکار:** پیشنهاد و اعتبارسنجی کدهای آرتیکل حسابداری مطابق با سرفصل‌های استاندارد.

### ۲. 🏛️ اتصال به سامانه مؤدیان و مالیات (Moadian Tax System Integration)
- **صورتحساب الکترونیکی:** ساخت و اعتبارسنجی قالب‌های صورتحساب الکترونیکی سامانه مؤدیان (انواع الگوهای ۱ تا ۷ فروش، صادرات، طلا، قرارداد و...).
- **محاسبات مالیاتی:** محاسبه خودکار ارزش افزوده (VAT)، عوارض، معافیت‌ها و انطباق با قوانین پایانه‌های فروشگاهی.
- **امضای دیجیتال و توکن:** ساختار آماده برای اتصال امن به کارپوشه مؤدیان مالیاتی.

### ۳. 🔌 سرور پروتکل کانتکست مدل (MCP Server)
- **ابزارهای استاندارد (MCP Tools):** قابلیت استعلام قواعد، اعتبارسنجی ترازنامه، صورت سود و زیان و گردش وجوه نقد مستقیماً از داخل کلاینت‌های هوش مصنوعی (مانند Claude Desktop، Cursor یا Agentها).
- **منابع و پرامپت‌ها (MCP Resources & Prompts):** دسترسی مستقیم مدل به ۳۵ استاندارد فعال و تعاریف کلیدی بدون نیاز به بارگذاری دستی فایل‌ها.

### ۴. 📐 موتور اعتبارسنجی و تطابق مالی (Financial Validation Engine)
- **۳۶ قاعده محاسباتی خودکار:** کنترل عدم مغایرت‌های ترازنامه، برابری سود سهام، مطابقت سود خالص با تغییرات حقوق مالکانه و جریان وجوه نقد.
- **قالب‌های ساختاریافته YAML:** صورت وضعیت مالی، صورت سود و زیان، سود و زیان جامع، تغییرات حقوق مالکانه و یادداشت‌های همراه.

---

## 📁 ساختار پروژه | Project Structure

```text
HesabYar-AI/
├── SKILL.md                          # فایل راهنمای مهارت و پرامپت سیستم
├── README.md                         # مستندات معرفی، شجره‌نامه و راهنمای پروژه
├── metadata.json                     # متادیتای ۳۵ استاندارد فعال حسابداری
├── requirements.txt                  # وابستگی‌های پایتون
├── LICENSE                           # مجوز متن‌باز MIT (حفظ حقوق پدیدآورنده اصلی)
│
├── core/                             # مبانی نظری و مفاهیم بنیادین
│   ├── definitions.md                # تعاریف عناصر صورت‌های مالی
│   ├── materiality.md                # مفهوم اهمیت در گزارشگری
│   ├── going-concern.md              # فرض تداوم فعالیت
│   └── concepts.md                   # مفاهیم نظری گزارشگری مالی
│
├── standards/                        # مخزن ۳۵ استاندارد حسابداری ایران
│   ├── 01-presentation/              # استاندارد ۱: ارائه صورت‌های مالی
│   │   ├── SKILL.md                  # چکیده اجرایی استاندارد
│   │   └── source/                   # متن کامل (Markdown، Word و PDF)
│   ├── 02-cash-flow/                 # استاندارد ۲: صورت جریان‌های نقدی
│   └── ...                           # سایر استانداردها تا ۴۴
│
├── templates/                        # قالب‌های استاندارد صورت‌های مالی
│   ├── financial-statements/         # قالب‌های YAML ساختاریافته
│   │   ├── balance-sheet.yaml        # صورت وضعیت مالی
│   │   ├── income-statement.yaml     # صورت سود و زیان
│   │   ├── comprehensive-income.yaml # صورت سود و زیان جامع
│   │   ├── equity-changes.yaml       # صورت تغییرات حقوق مالکانه
│   │   └── cash-flow.yaml            # صورت جریان‌های نقدی
│   ├── notes/                        # ساختار یادداشت‌های توضیحی همراه
│   └── disclosures/                  # افشاهای الزامی
│
├── validators/                       # قوانین و منطق اعتبارسنجی
│   ├── rules.yaml                    # ۳۶ قاعده اعتبارسنجی ماشین‌خوان
│   └── cross-standard/               # کنترل‌های متقابل بین‌صورت‌های مالی
│       ├── balance-check.md          # اعتبارسنجی ترازنامه
│       ├── income-statement.md       # اعتبارسنجی صورت سود و زیان
│       ├── cash-flow.md              # اعتبارسنجی جریان‌های نقدی
│       └── cross-checks.md           # چک‌های تقاطعی بین صورت‌ها
│
├── mappings/                         # جداول تطابق استانداردهای ایران با IFRS
│   └── ifrs-to-iranian.md            # نگاشت متناظر با استانداردهای IAS / IFRS
│
├── scripts/                          # ابزارها و اسکریپت‌های پردازش
│   ├── config.py                     # پیکربندی و مسیرها
│   ├── validator.py                  # موتور اعتبارسنجی صورت‌های مالی
│   ├── metadata_generator.py         # تولید متادیتای استانداردها
│   ├── converter.py                  # تبدیل اسناد Word به Markdown
│   ├── scraper.py                    # دریافت اسناد از منابع رسمی
│   ├── downloader.py                 # ابزار دانلود خودکار
│   └── main.py                       # خط لوله (Pipeline) کامل
│
└── tests/                            # آزمون‌های خودکار
    ├── test_validator.py             # آزمون‌های موتور اعتبارسنجی با pytest
    └── sample-data.json              # داده‌های نمونه صورت‌های مالی
```

---

## 🚀 شروع سریع | Quick Start

### ۱. نصب پیش‌نیازها

```bash
# کلون کردن مخزن
git clone https://github.com/Amirezamky9/HesabYar-AI.git
cd HesabYar-AI

# ایجاد و فعال‌سازی محیط مجازی
python3 -m venv .venv
source .venv/bin/activate  # در ویندوز: .venv\Scripts\activate

# نصب وابستگی‌های پایتون
pip install -r requirements.txt
```

> **توجه:** برای اجرای اسکریپت‌های تبدیل اسناد اولیه، ابزارهای `pandoc` و `libreoffice` مورد نیاز است. اما برای اجرای موتور اعتبارسنجی و تست‌ها، صرفاً وابستگی‌های پایتون کافی است.

### ۲. اجرای آزمون‌های موتور اعتبارسنجی

```bash
# اجرای تست‌های واحد با pytest
pytest tests/ -v

# خروجی:
# 21 passed
```

### ۳. اعتبارسنجی داده‌های نمونه صورت مالی

```bash
# اعتبارسنجی با فرمت متنی (Markdown)
python scripts/validator.py tests/sample-data.json --format markdown

# اعتبارسنجی و صدور گزارش خروجی در قالب JSON
python scripts/validator.py tests/sample-data.json --format json -o report.json
```

---

## 💻 نحوه استفاده در کد پایتون | Python SDK Usage

### اعتبارسنجی صورت‌های مالی با موتور محاسباتی

```python
import json
from pathlib import Path
from scripts.validator import FinancialValidator

# بارگذاری داده‌های صورت‌های مالی
sample_data = json.loads(Path("tests/sample-data.json").read_text(encoding="utf-8"))

validator = FinancialValidator()
report = validator.validate_all(
    balance_sheet=sample_data.get("balance_sheet"),
    income_statement=sample_data.get("income_statement"),
    cash_flow=sample_data.get("cash_flow"),
    equity_changes=sample_data.get("equity_changes"),
)

if report.is_valid:
    print("✅ تمامی ۳۶ قاعده اعتبارسنجی رعایت شده است.")
else:
    print(f"❌ تعداد خطاها: {len(report.errors)}")
    for error in report.errors:
        print(f"  - [{error.rule_id}] {error.message}")
```

### خواندن قواعد اعتبارسنجی ماشین‌خوان

```python
import yaml
from pathlib import Path

rules = yaml.safe_load(Path("validators/rules.yaml").read_text(encoding="utf-8"))
print(f"قواعد ترازنامه: {len(rules['balance_sheet'])}")
print(f"قواعد سود و زیان: {len(rules['income_statement'])}")
print(f"قواعد کنترل متقابل: {len(rules['cross_checks'])}")
```

---

## 📊 فهرست استانداردهای حسابداری ایران | Iranian Accounting Standards

> **Current-period warning (1405):** this table is a catalog, not proof that every bundled source is current. Use `metadata.json` + `docs/CURRENT_RULES_1405.md`. Standard 43 applies from 1404/01/01 and replaces 3/9/29; Standard 44 applies from 1405/01/01 and replaces 21; bundled Standard 15 is legacy and blocked for 1405+ until the official revised-1404 source is verified.

| شماره | عنوان استاندارد | استاندارد معادل بین‌المللی (IFRS / IAS) | دسته‌بندی |
| :---: | :--- | :---: | :--- |
| **۱** | ارائه صورت‌های مالی | IAS 1 | صورت‌های مالی پایه |
| **۲** | صورت جریان‌های نقدی | IAS 7 | صورت‌های مالی پایه |
| **۴** | ذخایر، بدهی‌های احتمالی و دارایی‌های احتمالی | IAS 37 | بدهی‌ها و تعهدات |
| **۵** | رویدادهای بعد از دوره گزارشگری | IAS 10 | بدهی‌ها و تعهدات |
| **۸** | حسابداری موجودی مواد و کالا | IAS 2 | دارایی‌ها |
| **۱۰** | حسابداری کمک‌های بلاعوض دولت | IAS 20 | دارایی‌ها |
| **۱۱** | دارایی‌های ثابت مشهود | IAS 16 | دارایی‌ها |
| **۱۲** | افشای اطلاعات اشخاص وابسته | IAS 24 | افشا و گزارشگری خاص |
| **۱۳** | مخارج تأمین مالی | IAS 23 | دارایی‌ها |
| **۱۵** | حسابداری سرمایه‌گذاری‌ها — **منبع جاری ۱۴۰۵ در repo ناقص/مسدود** | نیازمند بازاعتبارسنجی نگاشت نسخه ۱۴۰۴ | دارایی‌ها |
| **۱۶** | آثار تغییر در نرخ ارز | IAS 21 | درآمد و ارز |
| **۱۷** | دارایی‌های نامشهود | IAS 38 | دارایی‌ها |
| **۱۸** | صورت‌های مالی جداگانه | IAS 27 | تلفیق و سرمایه‌گذاری‌ها |
| **۲۰** | سرمایه‌گذاری در واحدهای تجاری وابسته | IAS 28 | تلفیق و سرمایه‌گذاری‌ها |
| **۲۲** | گزارشگری مالی میان‌دوره‌ای | IAS 34 | افشا و گزارشگری خاص |
| **۲۴** | گزارشگری مالی واحدهای تجاری در مرحله قبل از بهره‌برداری | — | افشا و گزارشگری خاص |
| **۲۵** | گزارشگری بر حسب قسمت‌های مختلف | IFRS 8 | افشا و گزارشگری خاص |
| **۲۶** | فعالیت‌های کشاورزی | IAS 41 | درآمد و ارز |
| **۲۷** | طرح‌های مزایای بازنشستگی | IAS 26 | صنایع خاص |
| **۲۸** | فعالیت‌های بیمه عمومی | IFRS 4 / IFRS 17 | صنایع خاص |
| **۳۰** | سود هر سهم | IAS 33 | افشا و گزارشگری خاص |
| **۳۱** | دارایی‌های غیرجاری نگهداری‌شده برای فروش و عملیات متوقف‌شده | IFRS 5 | افشا و گزارشگری خاص |
| **۳۲** | کاهش ارزش دارایی‌ها | IAS 36 | دارایی‌ها |
| **۳۳** | مزایای بازنشستگی کارکنان | IAS 19 | بدهی‌ها و تعهدات |
| **۳۴** | رویه‌های حسابداری، تغییر در برآوردهای حسابداری و اشتباهات | IAS 8 | صورت‌های مالی پایه |
| **۳۵** | مالیات بر درآمد | IAS 12 | صورت‌های مالی پایه |
| **۳۶** | ابزارهای مالی: ارائه | IAS 32 | ابزارهای مالی |
| **۳۷** | ابزارهای مالی: افشا | IFRS 7 | ابزارهای مالی |
| **۳۸** | ترکیب‌های تجاری | IFRS 3 | تلفیق و سرمایه‌گذاری‌ها |
| **۳۹** | صورت‌های مالی تلفیقی | IFRS 10 | تلفیق و سرمایه‌گذاری‌ها |
| **۴۰** | مشارکت‌ها | IFRS 11 | تلفیق و سرمایه‌گذاری‌ها |
| **۴۱** | افشای منافع در واحدهای تجاری دیگر | IFRS 12 | تلفیق و سرمایه‌گذاری‌ها |
| **۴۲** | اندازه‌گیری ارزش منصفانه | IFRS 13 | دارایی‌ها و اندازه‌گیری |
| **۴۳** | درآمد عملیاتی حاصل از قرارداد با مشتریان — لازم‌الاجرا از ۱۴۰۴/۰۱/۰۱ | IFRS 15 | درآمد و ارز |
| **۴۴** | اجاره‌ها — لازم‌الاجرا از ۱۴۰۵/۰۱/۰۱، جایگزین استاندارد ۲۱ | IFRS 16 | بدهی‌ها و تعهدات |

---

## 🗺️ نقشه راه توسعه | Development Roadmap

- [x] **پایه‌گذاری مخزن و مستندسازی corpus استانداردهای حسابداری** (با تشکر از Vohuman)
- [x] **افزودن current-rules baseline برای ۱۴۰۵ و effective-date gates برای استانداردهای ۱۵/۴۳/۴۴**
- [ ] **واردکردن و راستی‌آزمایی متن رسمی تجدیدنظر ۱۴۰۴ استاندارد ۱۵ برای دوره‌های ۱۴۰۵+**
- [ ] **پیاده‌سازی Electronic Commercial Books export/compliance با schema profile و compliance calendar**
- [x] **قواعد اعتبارسنجی ۳۶‌گانه و موتور اعتبارسنجی صورت‌های مالی**
- [x] **افزودن تست‌های اتوماتیک با pytest و خط لوله CI/CD در GitHub Actions**
- [ ] **طراحی و پیاده‌سازی سرور پروتکل کانتکست مدل (HesabYar MCP Server)**
- [ ] **اتصال به الگوهای صورتحساب الکترونیکی سامانه مؤدیان (Moadian Invoices)**
- [ ] **پکیج رسمی پایتون (`pip install hesabyar-ai`)**
- [ ] **ابزار خط فرمان (CLI) حساب‌یار برای ممیزی سریع فایل‌های مالی**
- [ ] **عامل‌های هوشمند چندگانه (Multi-Agent Workflows) برای حسابرسی و بستن حساب‌ها**

---

## 🤝 مشارکت در توسعه | Contributing

ما صمیمانه از همکاری توسعه‌دهندگان، حسابداران خبره، مشاوران مالیاتی و متخصصان هوش مصنوعی استقبال می‌کنیم!
برای مشارکت:

1. این مخزن را **Fork** کنید.
2. یک شاخه جدید بسازید (`git checkout -b feature/amazing-feature`).
3. تغییرات خود را Commit کنید (`git commit -m 'feat: add moadian invoice validator'`).
4. شاخه را Push کنید (`git push origin feature/amazing-feature`).
5. یک **Pull Request** ارسال نمایید.

---

## 📄 مجوز | License

این پروژه تحت مجوز **[MIT License](LICENSE)** منتشر شده است.  
کلیه حقوق معنوی اثر مبنا متعلق به **Vohuman** و مخزن بالادستی [accounting-iran-standards](https://github.com/seiahposh/accounting-iran-standards) است و مطابق شروط مجوز MIT، کپی‌رایت اصلی در این پروژه محفوظ می‌باشد.

---

## 📞 ارتباط و پشتیبانی | Contact & Support

- **سازنده و مؤلف مخزن مبنا (Upstream Creator):** Vohuman (`seiahposh`)
  - 📧 Email: `seiahposh@yahoo.com`
  - 📱 تماس: `09128005969`
  - 🐙 Upstream GitHub: [seiahposh/accounting-iran-standards](https://github.com/seiahposh/accounting-iran-standards)
- **مخزن و توسعه‌دهنده HesabYar-AI:** [Amirezamky9/HesabYar-AI](https://github.com/Amirezamky9/HesabYar-AI)
  - 🐛 گزارش باگ و پیشنهادات: [GitHub Issues](https://github.com/Amirezamky9/HesabYar-AI/issues)

⭐ اگر این پروژه برای شما مفید است، لطفاً به این مخزن و مخزن بالادستی ستاره دهید! ⭐
