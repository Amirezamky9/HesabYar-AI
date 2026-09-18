# اعتبارسنجی ترازنامه

> قواعد cross-standard برای بررسی صحت صورت وضعیت مالی

## 📌 قواعد اصلی

### ۱. معادله اصلی حسابداری
مجموع دارایی‌ها = مجموع بدهی‌ها + مجموع حقوق مالکانه

**کد:**
```python
assert total_assets == total_liabilities + total_equity
۲. تفکیک دارایی‌ها
دارایی جاری + دارایی غیرجاری = کل دارایی
کد:
assert total_current_assets + total_non_current_assets == total_assets
۳. تفکیک بدهی‌ها
بدهی جاری + بدهی غیرجاری = کل بدهی
کد:
assert total_current_liabilities + total_non_current_liabilities == total_liabilities
۴. جمع اجزای دارایی جاری
مجموع اجزای دارایی جاری = مجموع دارایی جاری
کد:
assert sum(current_asset_items) == total_current_assets
۵. جمع اجزای دارایی غیرجاری
مجموع اجزای دارایی غیرجاری = مجموع دارایی غیرجاری
۶. جمع اجزای بدهی جاری
مجموع اجزای بدهی جاری = مجموع بدهی جاری
۷. جمع اجزای بدهی غیرجاری
مجموع اجزای بدهی غیرجاری = مجموع بدهی غیرجاری
۸. جمع اجزای حقوق مالکانه
مجموع اجزای حقوق مالکانه = مجموع حقوق مالکانه
 قواعد تکمیلی
۹. نقد و معادل‌های نقد
باید با مانده پایان دوره صورت جریان نقدی مطابقت داشته باشد

کد:
assert cash_and_equivalents == cash_flow.ending_cash
۱۰. مالیات انتقالی
دارایی مالیات انتقالی در دارایی غیرجاری

بدهی مالیات انتقالی در بدهی غیرجاری

کد:
assert deferred_tax_assets in non_current_assets
assert deferred_tax_liabilities in non_current_liabilities
۱۱. دارایی‌های غیرجاری نگهداری شده برای فروش
طبق استاندارد ۳۱ طبقه‌بندی می‌شود

۱۲. منافع فاقد حق کنترل
در بخش حقوق مالکانه ارائه می‌شود

📌 قواعد تناسب
۱۳. نسبت جاری
current_ratio = current_assets / current_liabilities
if current_ratio < 1:
    warning("نسبت جاری کمتر از ۱ - احتمال مشکل نقدینگی")
۱۴. نسبت بدهی به حقوق مالکانه
debt_to_equity = total_liabilities / total_equity
if debt_to_equity > 2:
    warning("نسبت بدهی بالا - احتمال ریسک مالی")
۱۵. نسبت بدهی
debt_ratio = total_liabilities / total_assets
if debt_ratio > 0.7:
    warning("نسبت بدهی بالا")
الگوریتم اعتبارسنجی کامل
def validate_balance_sheet(bs):
    """اعتبارسنجی کامل ترازنامه"""
    errors = []
    warnings = []
    
    # ۱. معادله اصلی
    if bs.total_assets != bs.total_liabilities + bs.total_equity:
        errors.append({
            'code': 'BS001',
            'message': 'معادله اصلی برقرار نیست',
            'expected': bs.total_liabilities + bs.total_equity,
            'actual': bs.total_assets,
            'diff': bs.total_assets - (bs.total_liabilities + bs.total_equity)
        })
    
    # ۲. تفکیک دارایی‌ها
    if bs.total_current_assets + bs.total_non_current_assets != bs.total_assets:
        errors.append({
            'code': 'BS002',
            'message': 'جمع دارایی جاری و غیرجاری با کل دارایی مطابقت ندارد'
        })
    
    # ۳. تفکیک بدهی‌ها
    if bs.total_current_liabilities + bs.total_non_current_liabilities != bs.total_liabilities:
        errors.append({
            'code': 'BS003',
            'message': 'جمع بدهی جاری و غیرجاری با کل بدهی مطابقت ندارد'
        })
    
    # ۴. جمع اجزای دارایی جاری
    sum_current = sum(item.amount for item in bs.current_assets)
    if sum_current != bs.total_current_assets:
        errors.append({
            'code': 'BS004',
            'message': 'جمع اجزای دارایی جاری مطابقت ندارد',
            'diff': sum_current - bs.total_current_assets
        })
    
    # ۵. جمع اجزای دارایی غیرجاری
    sum_non_current = sum(item.amount for item in bs.non_current_assets)
    if sum_non_current != bs.total_non_current_assets:
        errors.append({
            'code': 'BS005',
            'message': 'جمع اجزای دارایی غیرجاری مطابقت ندارد'
        })
    
    # ۶. جمع اجزای بدهی جاری
    sum_current_liab = sum(item.amount for item in bs.current_liabilities)
    if sum_current_liab != bs.total_current_liabilities:
        errors.append({
            'code': 'BS006',
            'message': 'جمع اجزای بدهی جاری مطابقت ندارد'
        })
    
    # ۷. جمع اجزای بدهی غیرجاری
    sum_non_current_liab = sum(item.amount for item in bs.non_current_liabilities)
    if sum_non_current_liab != bs.total_non_current_liabilities:
        errors.append({
            'code': 'BS007',
            'message': 'جمع اجزای بدهی غیرجاری مطابقت ندارد'
        })
    
    # ۸. جمع اجزای حقوق مالکانه
    sum_equity = sum(item.amount for item in bs.equity_items)
    if sum_equity != bs.total_equity:
        errors.append({
            'code': 'BS008',
            'message': 'جمع اجزای حقوق مالکانه مطابقت ندارد'
        })
    
    # ۹. نسبت جاری
    if bs.total_current_liabilities > 0:
        current_ratio = bs.total_current_assets / bs.total_current_liabilities
        if current_ratio < 1:
            warnings.append({
                'code': 'BS-W01',
                'message': f'نسبت جاری {current_ratio:.2f} کمتر از ۱'
            })
    
    # ۱۰. نسبت بدهی
    if bs.total_equity > 0:
        debt_to_equity = bs.total_liabilities / bs.total_equity
        if debt_to_equity > 2:
            warnings.append({
                'code': 'BS-W02',
                'message': f'نسبت بدهی به حقوق مالکانه {debt_to_equity:.2f}'
            })
    
    return {
        'valid': len(errors) == 0,
        'errors': errors,
        'warnings': warnings
    }
جدول قواعد
کد	شرح	شدت
BS001	معادله اصلی حسابداری	error
BS002	تفکیک دارایی‌ها	error
BS003	تفکیک بدهی‌ها	error
BS004	جمع اجزای دارایی جاری	error
BS005	جمع اجزای دارایی غیرجاری	error
BS006	جمع اجزای بدهی جاری	error
BS007	جمع اجزای بدهی غیرجاری	error
BS008	جمع اجزای حقوق مالکانه	error
BS-W01	نسبت جاری < ۱	warning
BS-W02	نسبت بدهی > ۲	warning
BS-W03	نسبت بدهی > ۰.۷	warning
BS-W04	تفاوت نقد با جریان نقدی	error

