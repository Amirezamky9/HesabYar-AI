# اعتبارسنجی صورت تغییرات در حقوق مالکانه

> قواعد cross-standard برای بررسی صحت صورت تغییرات در حقوق مالکانه
> مرجع: استاندارد حسابداری ۱، بندهای ۱۰۸ تا ۱۱۳

## 📌 قواعد اصلی

### ۱. معادله اصلی
مانده پایان = مانده ابتدا + مجموع سود و زیان جامع + معاملات با مالکان

text

**کد:**
```python
assert closing_balance == (
    adjusted_opening_balance +
    total_comprehensive_income +
    total_owner_transactions
)
۲. مانده تعدیل‌شده ابتدای دوره
text
مانده تعدیل‌شده = مانده ابتدا + تسری به گذشته + تجدید ارائه
کد:

python
assert adjusted_opening_balance == (
    opening_balance +
    retrospective_application +
    retrospective_restatement
)
۳. مجموع سود و زیان جامع
text
مجموع سود و زیان جامع = سود دوره + سایر اقلام سود و زیان جامع
کد:

python
assert total_comprehensive_income == net_profit + other_comprehensive_income
۴. تفکیک مجموع سود و زیان جامع
text
مجموع سود و زیان جامع = قابل انتساب به مالکان + منافع فاقد حق کنترل
کد:

python
assert total_comprehensive_income == (
    owners_of_parent_portion +
    non_controlling_interests_portion
)
۵. معاملات با مالکان
text
مجموع معاملات = آورده مالکان - توزیع + تغییر منافع مالکیت
کد:

python
assert total_owner_transactions == (
    capital_contributions -
    distributions_to_owners +
    changes_in_ownership
)
📌 قواعد تکمیلی
۶. مطابقت اجزای حقوق مالکانه با ترازنامه
مانده پایان هر جزء باید با ترازنامه مطابقت داشته باشد

کد:

python
assert closing_balance.share_capital == balance_sheet.share_capital
assert closing_balance.retained_earnings == balance_sheet.retained_earnings
assert closing_balance.total_equity == balance_sheet.total_equity
۷. مطابقت سود دوره
سود دوره در صورت تغییرات = سود خالص در صورت سود و زیان

کد:

python
assert comprehensive_income.net_profit == income_statement.net_profit
۸. مطابقت سایر اقلام سود و زیان جامع
باید با صورت سود و زیان جامع مطابقت داشته باشد

کد:

python
assert other_comprehensive_income == comprehensive_income.other_comprehensive_income
۹. مطابقت سود تقسیمی
کل سود تقسیمی باید افشا شود

سود تقسیمی هر سهم باید افشا شود

کد:

python
assert dividends_disclosed == True
assert dividends_per_share_disclosed == True
۱۰. معاملات با مالکان
آورده مالکان: افزایش سرمایه، صرف سهام

توزیع: سود تقسیمی نقدی و سهامی

تغییر منافع مالکیت: معاملات با منافع فاقد حق کنترل (بدون از دست دادن کنترل)

📌 الگوریتم اعتبارسنجی کامل
python
def validate_equity_changes(ec, bs, is_, ci):
    """اعتبارسنجی کامل صورت تغییرات در حقوق مالکانه"""
    errors = []
    warnings = []
    
    # ۱. معادله اصلی
    expected_closing = (
        ec.adjusted_opening_balance +
        ec.total_comprehensive_income +
        ec.total_owner_transactions
    )
    if abs(ec.closing_balance.total_equity - expected_closing) > 0.01:
        errors.append({
            'code': 'EC001',
            'message': 'معادله اصلی حقوق مالکانه مطابقت ندارد',
            'expected': expected_closing,
            'actual': ec.closing_balance.total_equity
        })
    
    # ۲. مانده تعدیل‌شده ابتدای دوره
    expected_adjusted = (
        ec.opening_balance +
        ec.retrospective_application +
        ec.retrospective_restatement
    )
    if abs(ec.adjusted_opening_balance - expected_adjusted) > 0.01:
        errors.append({
            'code': 'EC002',
            'message': 'مانده تعدیل‌شده ابتدای دوره مطابقت ندارد'
        })
    
    # ۳. مجموع سود و زیان جامع
    expected_tci = ec.net_profit + ec.other_comprehensive_income
    if abs(ec.total_comprehensive_income - expected_tci) > 0.01:
        errors.append({
            'code': 'EC003',
            'message': 'مجموع سود و زیان جامع مطابقت ندارد'
        })
    
    # ۴. مطابقت سود دوره با صورت سود و زیان
    if abs(ec.net_profit - is_.net_profit) > 0.01:
        errors.append({
            'code': 'EC004',
            'message': 'سود دوره با صورت سود و زیان مطابقت ندارد',
            'equity': ec.net_profit,
            'income': is_.net_profit
        })
    
    # ۵. مطابقت سایر اقلام OCI
    if abs(ec.other_comprehensive_income - ci.other_comprehensive_income) > 0.01:
        errors.append({
            'code': 'EC005',
            'message': 'سایر اقلام OCI با صورت سود و زیان جامع مطابقت ندارد'
        })
    
    # ۶. مطابقت اجزای حقوق مالکانه با ترازنامه
    for item in ['share_capital', 'retained_earnings', 'total_equity']:
        ec_value = getattr(ec.closing_balance, item, None)
        bs_value = getattr(bs, item, None)
        if ec_value is not None and bs_value is not None:
            if abs(ec_value - bs_value) > 0.01:
                errors.append({
                    'code': 'EC006',
                    'message': f'مطابقت {item} با ترازنامه',
                    'equity_changes': ec_value,
                    'balance_sheet': bs_value
                })
    
    # ۷. معاملات با مالکان
    expected_owner = (
        ec.capital_contributions -
        ec.distributions_to_owners +
        ec.changes_in_ownership
    )
    if abs(ec.total_owner_transactions - expected_owner) > 0.01:
        errors.append({
            'code': 'EC007',
            'message': 'جمع معاملات با مالکان مطابقت ندارد'
        })
    
    # ۸. افشای سود تقسیمی
    if not ec.dividends_disclosed:
        warnings.append({
            'code': 'EC-W01',
            'message': 'سود تقسیمی افشا نشده'
        })
    
    if not ec.dividends_per_share_disclosed:
        warnings.append({
            'code': 'EC-W02',
            'message': 'سود تقسیمی هر سهم افشا نشده'
        })
    
    # ۹. تفکیک مجموع سود و زیان جامع
    expected_split = (
        ec.owners_of_parent_portion +
        ec.non_controlling_interests_portion
    )
    if abs(ec.total_comprehensive_income - expected_split) > 0.01:
        errors.append({
            'code': 'EC008',
            'message': 'تفکیک مجموع سود و زیان جامع مطابقت ندارد'
        })
    
    return {
        'valid': len(errors) == 0,
        'errors': errors,
        'warnings': warnings
    }
📌 جدول قواعد
کد	شرح	شدت
EC001	معادله اصلی حقوق مالکانه	error
EC002	مانده تعدیل‌شده ابتدای دوره	error
EC003	مجموع سود و زیان جامع	error
EC004	مطابقت سود دوره با صورت سود و زیان	error
EC005	مطابقت سایر اقلام OCI	error
EC006	مطابقت اجزای حقوق مالکانه با ترازنامه	error
EC007	جمع معاملات با مالکان	error
EC008	تفکیک مجموع سود و زیان جامع	error
EC-W01	افشای سود تقسیمی	warning
EC-W02	افشای سود تقسیمی هر سهم	warning
EC-W03	مطابقت تسری به گذشته با استاندارد ۳۴	warning
