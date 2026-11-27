# تاریخچه تغییرات

تمام تغییرات مهم این پروژه در این فایل مستند می‌شود.

قالب بر اساس [Keep a Changelog](https://keepachangelog.com/fa/1.1.0/)
و نسخه‌بندی بر اساس [Semantic Versioning](https://semver.org/lang/fa/).

> دو تاریخچه در این فایل است: بخش `[1.0.0]` و پایین‌تر، تاریخچهٔ
> بستهٔ **کاتالوگ استانداردها** (پروژهٔ پیشین این مخزن) است و بسته
> شده. انتشارهای **برنامهٔ حسابیار** (از `0.1.0`) زیر
> `[Unreleased]` و بخش‌های نسخه‌دار برنامه می‌آیند — روش کامل در
> `docs/release.md` §۲.

---

## [Unreleased]

### ✨ اضافه شده

- ران‌بوک ارتقا/بازگشت (`docs/runbook-upgrade-rollback.md`)، رویهٔ
  مهاجرت داده و تطبیق حسابداری (`docs/data-migration.md`)، مستندات
  انتشار (`docs/release.md`) و راهنمای اپراتور
  (`docs/operator-manual.md`) — US-705، فاز ۷.
- چک‌های اسکریپتی پیش‌ازارتقا (`scripts/pre_upgrade_check.py`:
  مایگریشن در head، تازگی بک‌آپ، در دسترس بودن ترایتون) و
  چک‌های تطبیق مهاجرت (استفادهٔ مجدد از چک‌های تمرین بازیابی
  US-703 + چک جدید tie-out سررسید اسناد) — US-705.
- تست بازگشت مایگریشن 0042 (downgrade به 0041 و upgrade دوباره به
  head روی دیتابیس موقت) — US-705. Head فعلی Alembic همچنان
  `0042` است؛ این انتشار مایگریشن جدیدی ندارد.

## [1.0.0] - 1403/07/01

### 🎉 نسخه اولیه

اولین نسخه پایدار پروژه با پشتیبانی از ۳۵ استاندارد حسابداری ایران.

### ✨ اضافه شده

#### استانداردها
- ۳۵ استاندارد حسابداری ایران (به‌روزرسانی ۱۳۹۷)
- متن کامل هر استاندارد در قالب Markdown
- فایل `SKILL.md` جداگانه برای هر استاندارد
- نگاشت استانداردهای ایران به IFRS

#### مفاهیم نظری (core/)
- `definitions.md` — تعاریف عناصر صورت‌های مالی
- `materiality.md` — اهمیت و آستانه‌ها
- `going-concern.md` — تداوم فعالیت
- `concepts.md` — مفاهیم نظری گزارشگری مالی

#### قالب‌های صورت‌های مالی (templates/)
- `balance-sheet.yaml` — صورت وضعیت مالی
- `income-statement.yaml` — صورت سود و زیان (دو روش)
- `comprehensive-income.yaml` — صورت سود و زیان جامع
- `equity-changes.yaml` — صورت تغییرات در حقوق مالکانه
- `cash-flow.yaml` — صورت جریان‌های نقدی (دو روش)
- `notes/structure.md` — ساختار یادداشت‌های توضیحی
- `notes/accounting-policies.md` — رویه‌های حسابداری
- `disclosures/estimates-uncertainty.md` — عدم اطمینان برآوردها

#### اعتبارسنجی (validators/)
- `rules.yaml` — ۴۰ قاعده ماشین‌خوان
- `cross-standard/balance-check.md` — اعتبارسنجی ترازنامه
- `cross-standard/income-statement.md` — اعتبارسنجی سود و زیان
- `cross-standard/comprehensive-income.md` — اعتبارسنجی OCI
- `cross-standard/equity-changes.md` — اعتبارسنجی حقوق مالکانه
- `cross-standard/cash-flow.md` — اعتبارسنجی جریان نقدی
- `cross-standard/cross-checks.md` — اعتبارسنجی بین صورت‌ها

#### اسکریپت‌های خودکار (scripts/)
- `config.py` — تنظیمات مرکزی
- `scraper.py` — استخراج لینک‌ها از منبع
- `downloader.py` — دانلود فایل‌های Word و PDF
- `converter.py` — تبدیل Word به Markdown
- `processor.py` — تولید فایل‌های `SKILL.md`
- `metadata_generator.py` — تولید `metadata.json`
- `validator.py` — موتور اعتبارسنجی
- `main.py` — اجرای کل pipeline
- `fix_yaml.py` — اصلاح خودکار YAML

#### مثال‌های عملی (examples/)
- `manufacturing/example-01.md` — شرکت تولیدی
- `banking/example-01.md` — بانک
- `insurance/example-01.md` — شرکت بیمه
- `nonprofit/example-01.md` — سازمان غیرانتفاعی

#### مستندات
- `README.md` — راهنمای کامل پروژه
- `SKILL.md` — فایل اصلی مهارت
- `LICENSE` — مجوز MIT
- `CHANGELOG.md` — این فایل

#### تست‌ها (tests/)
- `sample-data.json` — داده نمونه برای اعتبارسنجی

### 🔧 تغییرات فنی

- پشتیبانی از Windows، macOS و Linux
- تبدیل خودکار `.doc` به `.docx` با LibreOffice
- تبدیل `.docx` به Markdown با Pandoc
- پشتیبانی از UTF-8 و RTL
- موتور اعتبارسنجی با ۳۶ قاعده
- CLI برای اعتبارسنجی از خط فرمان

### 📊 آمار

| مورد | تعداد |
|------|-------|
| استانداردها | ۳۵ |
| فایل‌های `SKILL.md` | ۳۵ |
| فایل‌های `standard.md` | ۳۵ |
| قواعد اعتبارسنجی | ۴۰ (۳۶ فعال) |
| قالب‌های صورت مالی | ۵ |
| مثال‌های عملی | ۴ |
| اسکریپت‌ها | ۹ |
| کامیت‌ها | ۱۴ |
| حجم پروژه | ~۵۰ MB |

### 🙏 تقدیر

- **سازمان حسابرسی ایران** — استانداردهای رسمی
- **thdorsan.com** — منبع دانلود
- **Pandoc** — تبدیل Word به Markdown
- **LibreOffice** — تبدیل `.doc` به `.docx`

---

## [Unreleased]

### ✨ اضافه شده (فاز ۵-۳، موج B — US-524)

- کلاینت درگاه‌های پرداخت برای تطبیق وضعیت (بدون API تسویه): زرین‌پال (REST + GraphQL)، اسنپ‌پی، ترب‌پی، دیجی‌پی (UPG)؛ سداد و سلام‌پی دستی (بدون API)
- سرویس تطبیق درگاه: `verify-and-record`، `reconcile`، `callbacks`، `refunds` (فقط بازپرداخت کامل) — مسیرهای REST زیر `/api/v1/gateways`
- ذخیره رمزنگاری‌شدهٔ اطلاعات اتصال درگاه در کانال‌ها (مهاجرت 0021) + روت `PUT /integrations/channels/{channel_key}/credentials`
- کانال `torob_marketplace` به‌صورت RESERVED (غیرفعال، غیرقابل فعال‌سازی تا تأیید API سفارش ترب) — اکنون ۸ کانال داخلی

### 🔮 برنامه‌های آینده

#### نسخه 1.1.0
- [ ] افزودن تست‌های خودکار pytest
- [ ] GitHub Actions برای CI/CD
- [ ] پکیج‌بندی با `pyproject.toml`
- [ ] API پایتون برای اعتبارسنجی

#### نسخه 1.2.0
- [ ] پشتیبانی از چند زبان (انگلیسی)
- [ ] خروجی PDF از گزارش اعتبارسنجی
- [ ] داشبورد وب برای اعتبارسنجی
- [ ] یکپارچه‌سازی با نرم‌افزارهای حسابداری

#### نسخه 2.0.0
- [ ] موتور تولید خودکار صورت‌های مالی
- [ ] رابط گرافیکی (GUI)
- [ ] پشتیبانی از API REST
- [ ] یکپارچه‌سازی با IFRS

---

## 📝 راهنمای نسخه‌بندی

- **Major (X.0.0):** تغییرات ناسازگار
- **Minor (0.X.0):** افزودن قابلیت جدید (سازگار)
- **Patch (0.0.X):** رفع باگ (سازگار)

---

## 🔗 لینک‌های مرتبط

- [مخزن GitHub](https://github.com/yourusername/accounting-iran-standards)
- [مستندات](README.md)
- [فایل اصلی مهارت](SKILL.md)
- [مجوز](LICENSE)
