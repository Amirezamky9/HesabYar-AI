# Statutory Index: Iranian Social Security Law (قانون تامین اجتماعی)

**Primary Statute:** Social Security Law enacted on 1354/04/03 (1975-06-24) and subsequent amendments.  
**Domain Authority:** Social Security Organization of Iran (سازمان تامین اجتماعی - Tamin).

---

## 1. Statutory Contribution Rates (Article 28)

Under Article 28 of the Social Security Law and Article 5 of the Unemployment Insurance Law (قانون بیمه بیکاری مصوب ۱۳۶۹):

| Levy Component | Rate | Borne By | Statutory Basis | Accounting Classification |
|---|:---:|---|---|---|
| **Employee Share (سهم بیمه‌شده)** | **7%** | Employee (Deducted from gross pay) | Social Security Law Art. 28 | Payroll Deduction / Payable to Tamin |
| **Employer Share (سهم کارفرما)** | **20%** | Employer | Social Security Law Art. 28 | Operating Expense (هزینه بیمه سهم کارفرما) |
| **Unemployment Insurance (بیمه بیکاری)** | **3%** | Employer | Unemployment Ins. Law Art. 5 | Operating Expense (هزینه بیمه بیکاری) |
| **Total Statutory Levy** | **30%** | **7% Employee + 23% Employer** | Combined Statutes | Net Remittance to Tamin Account |

### Exemptions from Unemployment Insurance (3%)
Certain employee categories are exempt from the 3% unemployment insurance levy:
1. Retirees (بازنشستگان تامین اجتماعی یا لشکری/کشوری).
2. Total disability pensioners (ازکارافتادگان کلی).
3. Foreign nationals without reciprocal unemployment treaty coverage (اتباع خارجی).
4. Self-employed and voluntary insured persons (بیمه‌شدگان اختیاری و خاص).

---

## 2. Wage Ceilings & Floors (سقف و کف دستمزد مشمول کسر حق بیمه)

### Minimum Floor (کف دستمزد مشمول بیمه)
- Assessable earnings for full-time regular employees cannot be less than the statutory minimum daily wage enacted by the Supreme Labor Council for that calendar year.

### Statutory Ceiling (سقف دستمزد مشمول کسر حق بیمه)
- Pursuant to Article 35 of the Social Security Law and annual SSO circulars:
  $$\text{Daily Insurance Ceiling} = 7 \times \text{Approved Statutory Minimum Daily Wage}$$
  $$\text{Monthly Insurance Ceiling} = \text{Daily Insurance Ceiling} \times \text{Calendar Days of Month}$$
- Any portion of gross assessable earnings exceeding this monthly ceiling is **strictly exempt** from the 30% contribution.

---

## 3. Employer Legal Obligations & Filing Deadlines

### Article 36 (مسئولیت کارفرما)
- The employer is legally responsible for paying the entire 30% contribution (including withholding the 7% employee share at wage payment time).
- Failure of the employer to deduct or remit does not extinguish the employee's insurance rights or benefit accruals.

### Article 39 (مهلت ارسال لیست و پرداخت حق بیمه)
- The employer must submit the monthly wage list (لیست حقوق و دستمزد) and remit the full 30% contribution to the Social Security Organization by the **last day of the following calendar month** (e.g., Ordibehesht 31 for Farvardin wages).
- Late submission incurs statutory monthly penalties (2% per month or statutory delinquency surcharge).

---

## 4. Tamin Insurance Assessability Matrix

Based on Comprehensive Income Circular (بخشنامه جامع درآمد و بازرسی دفاتر قانونی سازمان تامین اجتماعی):

### Assessable Items (مشمول کسر حق بیمه)
- Base wage (مزد شغل / پایه)
- Seniority bonus (پایه سنوات)
- Housing allowance (حق مسکن)
- Grocery voucher / food allowance (بن خواربار / کمک‌هزینه اقلام مصرفی)
- Marriage allowance (حق تاهل)
- Overtime pay (اضافه کاری)
- Shift work premiums (نوبت کاری)
- Night work premiums (شب کاری)
- Friday work premiums (جمعه کاری)
- Productivity and performance bonuses (پاداش بهره‌وری و کارانه)
- Periodic bonuses agreed in employment contracts

### Non-Assessable Items (معاف از کسر حق بیمه)
- Child allowance (حق اولاد / کمک عائله‌مندی تحت ماده ۸۶ قانون کار)
- Mission allowance (فوق‌العاده ماموریت موضوع ماده ۴۶ قانون کار)
- Severance pay upon contract termination (حق سنوات / مزایای پایان کار موضوع ماده ۲۴ و ۳۱)
- Unused leave settlement (بازخرید مانده مرخصی موضوع ماده ۷۱)
- Annual Eidi and New Year bonus (عیدی و پاداش سالانه موضوع قانون ۱۳۷۰)
- In-kind travel, food, and transport allowances provided on company premises
- Direct medical expenses reimbursement

---

## 5. Monthly Tamin Diskette Specifications (DSKWOR00 & DSKKAR00)

Tamin mandates submission in DBF (dBase III / IV) table format, historically constrained to MS-DOS / Windows-1256 (or IRAN-SYSTEM) encodings:

### 1. `DSKWOR00.DBF` — Employee Master File (مشخصات بیمه‌شدگان)
Key fields:
- `DSK_ID`: Workshop / Employer ID (کد کارگاه - 10 chars)
- `DSK_NUM`: List Sequence Number
- `NAT_CODE`: National Code (کد ملی - 10 chars)
- `SS_CODE`: Social Security Insurance Number (شماره بیمه - 10 chars)
- `FIRST_NAME`: First Name (Persian encoded)
- `LAST_NAME`: Last Name (Persian encoded)
- `FATHER_NAME`: Father's Name
- `ID_NUM`: Birth Certificate Number (شماره شناسنامه)
- `JOB_CODE`: National Occupational Code (کد شغل)
- `START_DATE`: Employment start date in workshop

### 2. `DSKKAR00.DBF` — Monthly Assessment File (کارکرد ماهانه و مبالغ)
Key fields:
- `DSK_ID`: Workshop ID
- `SS_CODE`: Employee Social Security Number
- `WORK_DAYS`: Total worked days in period (1–31)
- `DAILY_WAGE`: Daily base wage (Rials)
- `MONTH_WAGE`: Total monthly base wage (Rials)
- `BENEFITS`: Monthly assessable benefits (Housing, Grocery, etc.)
- `TOT_WAGE`: Total gross assessable earnings (capped at 7x ceiling)
- `INS_EMP`: 7% Employee contribution amount
- `INS_EMPLR`: 20% Employer contribution amount
- `INS_UNEMP`: 3% Unemployment insurance amount
