
# 📚 مهارت استانداردهای حسابداری ایران

> مجموعه‌ای جامع از ۳۵ استاندارد حسابداری ایران برای طراحی و پیاده‌سازی نرم‌افزارهای حسابداری، آموزش، و حسابرسی.

[![Python](https://img.shields.io/badge/Python-3.10%2B-blue)](https://www.python.org/)
[![License](https://img.shields.io/badge/License-MIT-green)](LICENSE)
[![Standards](https://img.shields.io/badge/Standards-35-orange)](standards/)
[![IFRS](https://img.shields.io/badge/IFRS-Compatible-purple)](mappings/ifrs-to-iranian.md)

---

## 🎯 معرفی

این پروژه، یک **مهارت (Skill)** کامل برای طراحی و پیاده‌سازی نرم‌افزارهای حسابداری منطبق با استانداردهای ایران است. شامل:

- ✅ **۳۵ استاندارد فعال** حسابداری ایران
- ✅ **مفاهیم نظری** گزارشگری مالی
- ✅ **قالب‌های آماده** صورت‌های مالی
- ✅ **موتور اعتبارسنجی** با ۳۶ قاعده
- ✅ **تطابق با IFRS** برای تمام استانداردها
- ✅ **اسکریپت‌های خودکار** برای دانلود و پردازش

---

## 🚀 شروع سریع

### نصب پیش‌نیازها

```bash
# ۱. نصب Pandoc
# Windows:
winget install --id JohnMacFarlane.Pandoc
# macOS:
brew install pandoc
# Linux:
sudo apt-get install pandoc

# ۲. نصب LibreOffice
# Windows:
winget install TheDocumentFoundation.LibreOffice
# macOS:
brew install --cask libreoffice
# Linux:
sudo apt-get install libreoffice

# ۳. نصب پکیج‌های پایتون
pip install -r requirements.txt
اعتبارسنجی صورت‌های مالی
bash
# اعتبارسنجی نمونه
python scripts/validator.py tests/sample-data.json --format markdown

# خروجی:
# کل قواعد: 36
# موفق: 36 ✅
# وضعیت کلی: ✅ معتبر
دانلود مجدد استانداردها
bash
# دانلود همه ۳۵ استاندارد
python scripts/main.py

# فقط استانداردهای خاص
python scripts/main.py --only 1 2 34
📁 ساختار پروژه
text
accounting-iran-standards/
├── SKILL.md                          # فایل اصلی مهارت
├── README.md                         # این فایل
├── metadata.json                     # متادیتای ۳۵ استاندارد
├── requirements.txt                  # وابستگی‌های پایتون
│
├── core/                             # مفاهیم مشترک
│   ├── definitions.md                # تعاریف عناصر
│   ├── materiality.md                # اهمیت
│   ├── going-concern.md              # تداوم فعالیت
│   └── concepts.md                   # مفاهیم نظری
│
├── standards/                        # ۳۵ استاندارد فعال
│   ├── 01-presentation/
│   │   ├── SKILL.md                  # خلاصه استاندارد
│   │   └── source/
│   │       ├── standard.md           # متن کامل
│   │       ├── standard.docx         # Word
│   │       └── standard.pdf          # PDF
│   ├── 02-cash-flow/
│   └── ... (تا 44-leases)
│
├── templates/                        # قالب‌های آماده
│   ├── financial-statements/
│   │   ├── balance-sheet.yaml        # صورت وضعیت مالی
│   │   ├── income-statement.yaml     # صورت سود و زیان
│   │   ├── comprehensive-income.yaml # صورت سود و زیان جامع
│   │   ├── equity-changes.yaml       # صورت تغییرات حقوق مالکانه
│   │   └── cash-flow.yaml            # صورت جریان‌های نقدی
│   ├── notes/
│   │   ├── structure.md              # ساختار یادداشت‌ها
│   │   └── accounting-policies.md    # رویه‌های حسابداری
│   └── disclosures/
│       └── estimates-uncertainty.md  # عدم اطمینان برآوردها
│
├── validators/                       # اعتبارسنجی
│   ├── rules.yaml                    # ۳۶ قاعده ماشین‌خوان
│   └── cross-standard/
│       ├── balance-check.md          # اعتبارسنجی ترازنامه
│       ├── income-statement.md       # اعتبارسنجی سود و زیان
│       ├── comprehensive-income.md   # اعتبارسنجی OCI
│       ├── equity-changes.md         # اعتبارسنجی حقوق مالکانه
│       ├── cash-flow.md              # اعتبارسنجی جریان نقدی
│       └── cross-checks.md           # اعتبارسنجی بین صورت‌ها
│
├── mappings/                         # تطابق با IFRS
│   └── ifrs-to-iranian.md
│
├── scripts/                          # اسکریپت‌های خودکار
│   ├── config.py                     # تنظیمات
│   ├── scraper.py                    # استخراج لینک‌ها
│   ├── downloader.py                 # دانلود فایل‌ها
│   ├── converter.py                  # تبدیل Word به Markdown
│   ├── processor.py                  # تولید SKILL.md
│   ├── metadata_generator.py         # تولید metadata
│   ├── validator.py                  # موتور اعتبارسنجی
│   └── main.py                       # اجرای کل pipeline
│
├── tests/                            # تست‌ها
│   └── sample-data.json              # داده نمونه
│
└── logs/                             # لاگ‌های pipeline
    └── pipeline.log
📊 فهرست ۳۵ استاندارد
فاز ۱: صورت‌های مالی پایه
#	عنوان	IFRS
۱	ارائه صورت‌های مالی	IAS 1
۲	صورت جریان‌های نقدی	IAS 7
۳۴	رویه‌های حسابداری	IAS 8
۳۵	مالیات بر درآمد	IAS 12
فاز ۲: دارایی‌ها
#	عنوان	IFRS
۸	موجودی مواد و کالا	IAS 2
۱۰	کمک‌های بلاعوض دولت	IAS 20
۱۱	دارایی‌های ثابت مشهود	IAS 16
۱۳	مخارج تأمین مالی	IAS 23
۱۵	سرمایه‌گذاری‌ها	IAS 39/IFRS 9
۱۷	دارایی‌های نامشهود	IAS 38
۳۲	کاهش ارزش دارایی‌ها	IAS 36
۴۲	اندازه‌گیری ارزش منصفانه	IFRS 13
فاز ۳: بدهی‌ها و تعهدات
#	عنوان	IFRS
۴	ذخایر و بدهی‌های احتمالی	IAS 37
۵	رویدادهای بعد از ترازنامه	IAS 10
۳۳	مزایای بازنشستگی	IAS 19
۴۴	اجاره‌ها	IFRS 16
فاز ۴: درآمد و ارز
#	عنوان	IFRS
۱۶	تغییر نرخ ارز	IAS 21
۲۶	فعالیت‌های کشاورزی	IAS 41
۴۳	درآمد از قرارداد	IFRS 15
فاز ۵: تلفیق و سرمایه‌گذاری‌ها
#	عنوان	IFRS
۱۸	صورت‌های مالی جداگانه	IAS 27
۲۰	واحدهای وابسته	IAS 28
۳۸	ترکیب‌های تجاری	IFRS 3
۳۹	صورت‌های تلفیقی	IFRS 10
۴۰	مشارکت‌ها	IFRS 11
۴۱	افشای منافع	IFRS 12
فاز ۶: افشا و گزارشگری خاص
#	عنوان	IFRS
۱۲	اشخاص وابسته	IAS 24
۲۲	گزارشگری میان‌دوره‌ای	IAS 34
۲۴	قبل از بهره‌برداری	—
۲۵	قسمت‌های مختلف	IFRS 8
۳۰	سود هر سهم	IAS 33
۳۱	غیرجاری برای فروش	IFRS 5
فاز ۷: صنایع خاص
#	عنوان	IFRS
۲۷	مزایای بازنشستگی	IAS 26
۲۸	بیمه عمومی	IFRS 4/17
فاز ۸: ابزارهای مالی
#	عنوان	IFRS
۳۶	ابزارهای مالی: ارائه	IAS 32
۳۷	ابزارهای مالی: افشا	IFRS 7
🧪 تست و اعتبارسنجی
تست سریع
bash
# اجرای موتور اعتبارسنجی روی داده نمونه
python scripts/validator.py tests/sample-data.json --format markdown

# خروجی: ۳۶ قاعده، ۳۶ موفق، ✅ معتبر
خروجی JSON
bash
python scripts/validator.py tests/sample-data.json --format json -o report.json
خروجی Markdown
bash
python scripts/validator.py tests/sample-data.json --format markdown -o report.md
🔧 نحوه استفاده در نرم‌افزار
۱. خواندن قواعد اعتبارسنجی
python
import yaml
from pathlib import Path

rules = yaml.safe_load(Path("validators/rules.yaml").read_text(encoding="utf-8"))
print(f"تعداد قواعد: {len(rules['balance_sheet']) + len(rules['income_statement']) + ...}")
۲. استفاده از موتور اعتبارسنجی
python
from scripts.validator import FinancialValidator

validator = FinancialValidator()

report = validator.validate_all(
    balance_sheet=my_balance_sheet,
    income_statement=my_income_statement,
    cash_flow=my_cash_flow,
)

if report.is_valid:
    print("✅ صورت‌های مالی معتبر است")
else:
    for error in report.errors:
        print(f"❌ {error.rule_id}: {error.message}")
۳. استفاده از قالب‌ها
python
import yaml
from pathlib import Path

template = yaml.safe_load(
    Path("templates/financial-statements/balance-sheet.yaml").read_text(encoding="utf-8")
)

# ساختار قالب رو ببین
for section in template["structure"]:
    print(section["section"])
📚 مستندات بیشتر
SKILL.md — فایل اصلی مهارت

core/definitions.md — تعاریف عناصر

core/materiality.md — اهمیت

core/going-concern.md — تداوم فعالیت

core/concepts.md — مفاهیم نظری

mappings/ifrs-to-iranian.md — تطابق با IFRS

🤝 مشارکت
از مشارکت شما استقبال می‌کنیم! برای مشارکت:

Fork کنید

Branch جدید بسازید (git checkout -b feature/amazing-feature)

Commit کنید (git commit -m 'Add amazing feature')

Push کنید (git push origin feature/amazing-feature)

Pull Request بسازید

📄 مجوز
این پروژه تحت مجوز MIT منتشر می‌شود. جزئیات در LICENSE.

🙏 تقدیر
سازمان حسابرسی ایران — برای استانداردهای رسمی

thdorsan.com — برای منبع دانلود

Pandoc — برای تبدیل Word به Markdown

LibreOffice — برای تبدیل .doc به .docx

📞 09128005969 
GitHub Issues: برای گزارش باگ و پیشنهاد

ایمیل: seiahposh@yahoo.com

📊 آمار
مورد	تعداد
استانداردها	۳۵
خطوط Markdown	~۳۰,۰۰۰
قواعد اعتبارسنجی	۳۶
قالب‌های صورت مالی	۵
کامیت‌ها	۱۱+
حجم پروژه	~۵۰ MB
⭐ اگه این پروژه به کارت اومد، ستاره بده! ⭐