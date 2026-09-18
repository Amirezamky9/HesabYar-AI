# اعتبارسنجی صورت جریان‌های نقدی

> قواعد cross-standard برای بررسی صحت صورت جریان‌های نقدی
> مرجع: استاندارد حسابداری ۲

## 📌 قواعد اصلی

### ۱. تغییر خالص در نقد
تغییر خالص = عملیاتی + سرمایه‌گذاری + تأمین مالی

text

**کد:**
```python
assert net_change_in_cash == (
    net_cash_from_operating +
    net_cash_from_investing +
    net_cash_from_financing
)
۲. مانده پایان
text
مانده پایان = مانده ابتدا + تغییر خالص + اثر ارز
کد:

python
assert ending_cash == (
    beginning_cash +
    net_change_in_cash +
    effect_of_exchange_rate
)
۳. مطابقت با ترازنامه
text
مانده پایان نقد = مانده نقد در ترازنامه
کد:

python
assert ending_cash == balance_sheet.cash_and_equivalents
📌 قواعد روش غیرمستقیم
۴. جمع تعدیلات
text
سود قبل از مالیات + تعدیلات = جریان نقدی عملیاتی
کد:

python
adjustments = (
    depreciation +
    amortization +
    provisions +
    impairment_losses +
    foreign_exchange_gains_losses +
    gains_losses_on_disposal +
    change_in_receivables +
    change_in_inventory +
    change_in_payables +
    change_in_other_working_capital -
    income_tax_paid +
    other_adjustments
)

assert net_cash_from_operating == net_profit_before_tax + adjustments
۵. تعدیلات غیرنقدی
استهلاک: باید با یادداشت دارایی‌های ثابت مطابقت داشته باشد

ذخایر: باید با تغییر ذخایر در ترازنامه مطابقت داشته باشد

تسعیر ارز: باید با سایر اقلام سود و زیان جامع مطابقت داشته باشد

📌 قواعد تکمیلی
۶. معاملات غیرنقدی
باید افشا شوند (مثل تبدیل بدهی به سرمایه)

۷. مالیات پرداختی
باید جداگانه افشا شود

۸. سود و سود سهام
طبقه‌بندی باید یکنواخت باشد

سود دریافتی: عملیاتی یا سرمایه‌گذاری

سود سهام دریافتی: عملیاتی یا سرمایه‌گذاری

سود پرداختی: عملیاتی یا تأمین مالی

سود سهام پرداختی: تأمین مالی

۹. محدودیت‌های نقد
اگر مانده نقد قابل استفاده نباشد، باید افشا شود

📌 الگوریتم اعتبارسنجی کامل
python
def validate_cash_flow(cf, bs, is_):
    """اعتبارسنجی کامل صورت جریان‌های نقدی"""
    errors = []
    warnings = []
    
    # ۱. تغییر خالص
    expected_net_change = (
        cf.net_cash_from_operating +
        cf.net_cash_from_investing +
        cf.net_cash_from_financing
    )
    if abs(cf.net_change_in_cash - expected_net_change) > 0.01:
        errors.append({
            'code': 'CF001',
            'message': 'تغییر خالص در نقد مطابقت ندارد',
            'expected': expected_net_change,
            'actual': cf.net_change_in_cash
        })
    
    # ۲. مانده پایان
    expected_ending = (
        cf.beginning_cash +
        cf.net_change_in_cash +
        cf.effect_of_exchange_rate
    )
    if abs(cf.ending_cash - expected_ending) > 0.01:
        errors.append({
            'code': 'CF002',
            'message': 'مانده پایان مطابقت ندارد'
        })
    
    # ۳. مطابقت با ترازنامه
    if abs(cf.ending_cash - bs.cash_and_equivalents) > 0.01:
        errors.append({
            'code': 'CF003',
            'message': 'مانده پایان با ترازنامه مطابقت ندارد',
            'cash_flow': cf.ending_cash,
            'balance_sheet': bs.cash_and_equivalents,
            'diff': cf.ending_cash - bs.cash_and_equivalents
        })
    
    # ۴. روش غیرمستقیم
    if cf.method == 'indirect':
        expected_operating = (
            is_.profit_before_tax +
            cf.depreciation +
            cf.amortization +
            cf.provisions +
            cf.impairment_losses +
            cf.foreign_exchange_gains_losses +
            cf.gains_losses_on_disposal +
            cf.change_in_receivables +
            cf.change_in_inventory +
            cf.change_in_payables +
            cf.change_in_other_working_capital -
            cf.income_tax_paid +
            cf.other_adjustments
        )
        if abs(cf.net_cash_from_operating - expected_operating) > 0.01:
            errors.append({
                'code': 'CF004',
                'message': 'جریان عملیاتی (روش غیرمستقیم) مطابقت ندارد'
            })
    
    # ۵. مطابقت استهلاک
    if hasattr(is_, 'depreciation'):
        # استهلاک در صورت سود و زیان باید با جریان نقدی مطابقت داشته باشد
        if abs(cf.depreciation - is_.depreciation) > 0.01:
            warnings.append({
                'code': 'CF-W01',
                'message': 'استهلاک در صورت سود و زیان و جریان نقدی مطابقت ندارد'
            })
    
    # ۶. معاملات غیرنقدی
    if cf.non_cash_transactions and not cf.non_cash_disclosed:
        errors.append({
            'code': 'CF005',
            'message': 'معاملات غیرنقدی بااهمیت افشا نشده'
        })
    
    # ۷. مالیات پرداختی
    if cf.income_tax_paid == 0 and is_.tax_expense != 0:
        warnings.append({
            'code': 'CF-W02',
            'message': 'مالیات پرداختی صفر است ولی هزینه مالیات وجود دارد'
        })
    
    return {
        'valid': len(errors) == 0,
        'errors': errors,
        'warnings': warnings
    }
📌 جدول قواعد
کد	شرح	شدت
CF001	تغییر خالص در نقد	error
CF002	مانده پایان	error
CF003	مطابقت با ترازنامه	error
CF004	جریان عملیاتی (غیرمستقیم)	error
CF005	معاملات غیرنقدی	error
CF-W01	مطابقت استهلاک	warning
CF-W02	مالیات پرداختی	warning
CF-W03	طبقه‌بندی سود و سود سهام	warning
CF-W04	محدودیت‌های نقد	warning
