# اعتبارسنجی‌های بین صورت‌های مالی

> قواعد cross-standard که بین چند صورت مالی بررسی می‌شوند

## 📌 بررسی‌های اصلی

### ۱. مطابقت سود خالص
سود خالص در صورت سود و زیان = سود دوره در صورت تغییرات حقوق مالکانه

text

**کد:**
```python
assert income_statement.net_profit == equity_changes.net_profit
۲. مطابقت سایر اقلام OCI
text
سایر اقلام OCI در صورت سود و زیان جامع = سایر اقلام OCI در صورت تغییرات حقوق مالکانه
کد:

python
assert comprehensive_income.other_comprehensive_income == equity_changes.other_comprehensive_income
۳. مطابقت مجموع سود و زیان جامع
text
مجموع سود و زیان جامع در صورت OCI = مجموع در صورت تغییرات حقوق مالکانه
کد:

python
assert comprehensive_income.total_comprehensive_income == equity_changes.total_comprehensive_income
۴. مطابقت نقد و معادل‌های نقد
text
مانده پایان نقد در صورت جریان نقدی = مانده نقد در ترازنامه
کد:

python
assert cash_flow.ending_cash == balance_sheet.cash_and_equivalents
۵. مطابقت حقوق مالکانه
text
مجموع حقوق مالکانه در ترازنامه = مانده پایان در صورت تغییرات حقوق مالکانه
کد:

python
assert balance_sheet.total_equity == equity_changes.closing_balance.total_equity
۶. مطابقت اجزای حقوق مالکانه
text
هر جزء حقوق مالکانه در ترازنامه = مانده پایان همان جزء در صورت تغییرات
کد:

python
for item in ['share_capital', 'retained_earnings', 'legal_reserve', ...]:
    assert getattr(balance_sheet, item) == getattr(equity_changes.closing_balance, item)
۷. مطابقت استهلاک
text
استهلاک در صورت سود و زیان = استهلاک در صورت جریان نقدی
کد:

python
if hasattr(income_statement, 'depreciation') and hasattr(cash_flow, 'depreciation'):
    assert abs(income_statement.depreciation - cash_flow.depreciation) < 0.01
۸. مطابقت مالیات
text
هزینه مالیات در صورت سود و زیان ≈ مالیات پرداختی در جریان نقدی
(با در نظر گرفتن تغییرات مالیات پرداختنی و انتقالی)
کد:

python
expected_tax_paid = (
    income_statement.tax_expense +
    balance_sheet.opening_tax_payable -
    balance_sheet.closing_tax_payable
)
# تقریبی
۹. مطابقت دارایی‌های ثابت
text
تغییرات دارایی ثابت در ترازنامه = 
  خرید در جریان نقدی 
- استهلاک 
- فروش 
+ تجدید ارزیابی
۱۰. مطابقت سرمایه در گردش
text
تغییر در اقلام سرمایه در گردش در جریان نقدی = تغییرات ترازنامه
📌 الگوریتم اعتبارسنجی جامع
python
def validate_all_statements(bs, is_, ci, ec, cf):
    """اعتبارسنجی جامع همه صورت‌ها"""
    errors = []
    warnings = []
    
    # ۱. سود خالص
    if abs(is_.net_profit - ec.net_profit) > 0.01:
        errors.append({
            'code': 'XC001',
            'message': 'سود خالص در صورت سود و زیان و تغییرات حقوق مالکانه مطابقت ندارد',
            'income_statement': is_.net_profit,
            'equity_changes': ec.net_profit
        })
    
    # ۲. سایر اقلام OCI
    if abs(ci.other_comprehensive_income - ec.other_comprehensive_income) > 0.01:
        errors.append({
            'code': 'XC002',
            'message': 'سایر اقلام OCI مطابقت ندارد'
        })
    
    # ۳. مجموع سود و زیان جامع
    if abs(ci.total_comprehensive_income - ec.total_comprehensive_income) > 0.01:
        errors.append({
            'code': 'XC003',
            'message': 'مجموع سود و زیان جامع مطابقت ندارد'
        })
    
    # ۴. نقد و معادل‌های نقد
    if abs(cf.ending_cash - bs.cash_and_equivalents) > 0.01:
        errors.append({
            'code': 'XC004',
            'message': 'نقد و معادل‌های نقد مطابقت ندارد',
            'cash_flow': cf.ending_cash,
            'balance_sheet': bs.cash_and_equivalents,
            'diff': cf.ending_cash - bs.cash_and_equivalents
        })
    
    # ۵. حقوق مالکانه
    if abs(bs.total_equity - ec.closing_balance.total_equity) > 0.01:
        errors.append({
            'code': 'XC005',
            'message': 'مجموع حقوق مالکانه مطابقت ندارد'
        })
    
    # ۶. استهلاک
    if hasattr(is_, 'depreciation') and hasattr(cf, 'depreciation'):
        if abs(is_.depreciation - cf.depreciation) > 0.01:
            warnings.append({
                'code': 'XC-W01',
                'message': 'استهلاک در صورت سود و زیان و جریان نقدی مطابقت ندارد',
                'income_statement': is_.depreciation,
                'cash_flow': cf.depreciation
            })
    
    # ۷. مالیات
    if hasattr(is_, 'tax_expense') and hasattr(cf, 'income_tax_paid'):
        tax_diff = abs(is_.tax_expense - cf.income_tax_paid)
        if tax_diff > 0.1 * abs(is_.tax_expense):
            warnings.append({
                'code': 'XC-W02',
                'message': f'تفاوت قابل توجه بین هزینه مالیات و مالیات پرداختی: {tax_diff}',
                'hint': 'بررسی تغییرات مالیات پرداختنی و انتقالی'
            })
    
    # ۸. سرمایه در گردش
    wc_change_bs = bs.current_assets - bs.current_liabilities
    # این رو باید با تغییرات در جریان نقدی مقایسه کرد
    
    return {
        'valid': len(errors) == 0,
        'errors': errors,
        'warnings': warnings,
        'summary': {
            'total_errors': len(errors),
            'total_warnings': len(warnings),
            'statements_validated': 5
        }
    }
📌 جدول قواعد
کد	شرح	شدت
XC001	مطابقت سود خالص	error
XC002	مطابقت سایر اقلام OCI	error
XC003	مطابقت مجموع سود و زیان جامع	error
XC004	مطابقت نقد و معادل‌های نقد	error
XC005	مطابقت حقوق مالکانه	error
XC-W01	مطابقت استهلاک	warning
XC-W02	مطابقت مالیات	warning
XC-W03	مطابقت سرمایه در گردش	warning
XC-W04	مطابقت معاملات با مالکان	warning
📌 نمودار ارتباط بین صورت‌ها
text
                    ┌─────────────────────┐
                    │  صورت وضعیت مالی     │
                    │  (Balance Sheet)    │
                    └──────────┬──────────┘
                               │
              ┌────────────────┼────────────────┐
              │                │                │
              ▼                ▼                ▼
    ┌──────────────┐  ┌──────────────┐  ┌──────────────┐
    │ صورت سود     │  │ صورت تغییرات │  │ صورت جریان   │
    │ و زیان       │  │ حقوق مالکانه │  │ نقدی         │
    └──────┬───────┘  └──────┬───────┘  └──────┬───────┘
           │                 │                 │
           │  سود خالص ◄─────┼─────────────────┘
           │                 │
           │  OCI    ◄───────┤
           │                 │
           ▼                 ▼
    ┌──────────────┐  ┌──────────────┐
    │ صورت سود و   │  │              │
    │ زیان جامع    │◄─┤              │
    └──────────────┘  └──────────────┘
نکات کلیدی:

سود خالص از صورت سود و زیان به تغییرات حقوق مالکانه و OCI منتقل می‌شود

OCI از صورت سود و زیان جامع به تغییرات حقوق مالکانه منتقل می‌شود

نقد از ترازنامه به جریان نقدی منتقل می‌شود

حقوق مالکانه از ترازنامه به تغییرات حقوق مالکانه منتقل می‌شود

