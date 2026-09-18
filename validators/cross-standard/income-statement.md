# اعتبارسنجی صورت سود و زیان

> قواعد cross-standard برای بررسی صحت صورت سود و زیان

## 📌 قواعد اصلی (روش ماهیت هزینه)

### ۱. سود عملیاتی
سود عملیاتی = درآمد + سایر درآمدها - جمع هزینه‌ها

text

**کد:**
```python
assert operating_profit == revenue + other_income - total_expenses
۲. سود قبل از مالیات
text
سود قبل از مالیات = سود عملیاتی - هزینه‌های مالی + سهم از واحدهای وابسته
کد:

python
assert profit_before_tax == operating_profit - finance_costs + share_of_associates
۳. سود خالص
text
سود خالص = سود قبل از مالیات - هزینه مالیات + عملیات متوقف شده
کد:

python
assert net_profit == profit_before_tax - tax_expense + discontinued_operations
۴. تفکیک سود خالص
text
سود خالص = سود قابل انتساب به مالکان + منافع فاقد حق کنترل
کد:

python
assert net_profit == owners_of_parent + non_controlling_interests
📌 قواعد روش کارکرد هزینه
۵. سود ناخالص
text
سود ناخالص = درآمد - بهای تمام شده کالای فروش رفته
کد:

python
assert gross_profit == revenue - cost_of_goods_sold
۶. سود عملیاتی (روش کارکرد)
text
سود عملیاتی = سود ناخالص + سایر درآمدها - هزینه‌های فروش - اداری - سایر
📌 قواعد تکمیلی
۷. عدم تهاتر
درآمد و هزینه نباید تهاتر شوند (مگر موارد مجاز)

اقلام بااهمیت باید جداگانه ارائه شوند

۸. عملیات متوقف شده
باید مبلغ مجزا برای کل عملیات متوقف شده باشد

طبق استاندارد ۳۱

۹. سهم از واحدهای وابسته
طبق روش ارزش ویژه (استاندارد ۲۰)

۱۰. اقلام بااهمیت
اقلام خاص باید جداگانه افشا شوند:

کاهش ارزش موجودی

تجدید ساختار

واگذاری دارایی‌های ثابت

عملیات متوقف شده

حل و فصل دعاوی حقوقی

📌 الگوریتم اعتبارسنجی کامل
python
def validate_income_statement(is_):
    """اعتبارسنجی کامل صورت سود و زیان"""
    errors = []
    warnings = []
    
    method = is_.classification_method  # nature یا function
    
    if method == 'nature_of_expense':
        # روش ماهیت هزینه
        
        # ۱. جمع هزینه‌ها
        expected_total = (
            is_.raw_materials_consumed +
            is_.employee_benefits +
            is_.depreciation +
            is_.other_expenses -
            is_.changes_in_inventory
        )
        if abs(is_.total_expenses - expected_total) > 0.01:
            errors.append({
                'code': 'IS001',
                'message': 'جمع هزینه‌ها مطابقت ندارد',
                'expected': expected_total,
                'actual': is_.total_expenses
            })
        
        # ۲. سود عملیاتی
        expected_operating = is_.revenue + is_.other_income - is_.total_expenses
        if abs(is_.operating_profit - expected_operating) > 0.01:
            errors.append({
                'code': 'IS002',
                'message': 'سود عملیاتی مطابقت ندارد',
                'expected': expected_operating,
                'actual': is_.operating_profit
            })
    
    elif method == 'function_of_expense':
        # روش کارکرد هزینه
        
        # ۱. سود ناخالص
        expected_gross = is_.revenue - is_.cost_of_goods_sold
        if abs(is_.gross_profit - expected_gross) > 0.01:
            errors.append({
                'code': 'IS003',
                'message': 'سود ناخالص مطابقت ندارد',
                'expected': expected_gross,
                'actual': is_.gross_profit
            })
        
        # ۲. سود عملیاتی
        expected_operating = (
            is_.gross_profit + is_.other_income -
            is_.selling_expenses - is_.administrative_expenses - is_.other_expenses
        )
        if abs(is_.operating_profit - expected_operating) > 0.01:
            errors.append({
                'code': 'IS004',
                'message': 'سود عملیاتی مطابقت ندارد'
            })
    
    # ۳. سود قبل از مالیات (مشترک)
    expected_pbt = is_.operating_profit - is_.finance_costs + is_.share_of_associates
    if abs(is_.profit_before_tax - expected_pbt) > 0.01:
        errors.append({
            'code': 'IS005',
            'message': 'سود قبل از مالیات مطابقت ندارد'
        })
    
    # ۴. سود خالص (مشترک)
    expected_net = is_.profit_before_tax - is_.tax_expense + is_.discontinued_operations
    if abs(is_.net_profit - expected_net) > 0.01:
        errors.append({
            'code': 'IS006',
            'message': 'سود خالص مطابقت ندارد'
        })
    
    # ۵. تفکیک سود خالص
    expected_split = is_.owners_of_parent + is_.non_controlling_interests
    if abs(is_.net_profit - expected_split) > 0.01:
        errors.append({
            'code': 'IS007',
            'message': 'تفکیک سود خالص مطابقت ندارد'
        })
    
    # ۶. بررسی افشای اقلام خاص
    special_items = [
        'impairment_losses',
        'restructuring_costs',
        'disposal_of_assets',
        'discontinued_operations',
        'legal_settlements'
    ]
    for item in special_items:
        amount = getattr(is_, item, 0)
        if amount != 0 and not getattr(is_, f'{item}_disclosed', False):
            warnings.append({
                'code': 'IS-W01',
                'message': f'قلم {item} بااهمیت است ولی افشا نشده'
            })
    
    # ۷. بررسی عدم تهاتر
    if is_.revenue < 0 or is_.expenses < 0:
        warnings.append({
            'code': 'IS-W02',
            'message': 'مبالغ درآمد یا هزینه منفی - بررسی تهاتر'
        })
    
    return {
        'valid': len(errors) == 0,
        'errors': errors,
        'warnings': warnings
    }
📌 جدول قواعد
کد	شرح	شدت
IS001	جمع هزینه‌ها (روش ماهیت)	error
IS002	سود عملیاتی (روش ماهیت)	error
IS003	سود ناخالص (روش کارکرد)	error
IS004	سود عملیاتی (روش کارکرد)	error
IS005	سود قبل از مالیات	error
IS006	سود خالص	error
IS007	تفکیک سود خالص	error
IS-W01	افشای اقلام خاص	warning
IS-W02	بررسی تهاتر	warning
IS-W03	درآمد منفی	warning
IS-W04	هزینه مالیات منفی	warning
