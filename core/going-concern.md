# تداوم فعالیت (Going Concern)

> مرجع: استاندارد حسابداری ۱، بندهای ۲۳-۲۴ و مفاهیم نظری گزارشگری مالی ایران

## 📌 تعریف

**تداوم فعالیت:** فرض بر این است که واحد تجاری در آینده قابل پیش‌بینی (حداقل ۱۲ ماه پس از پایان دوره گزارشگری) به فعالیت خود ادامه می‌دهد.

## 📌 الزامات استاندارد

### ۱. ارزیابی مدیریت (بند ۲۳)
مدیریت باید توانایی تداوم فعالیت واحد تجاری را ارزیابی کند.

### ۲. افشای عدم اطمینان (بند ۲۳)
در صورت وجود **تردید اساسی** در توانایی تداوم فعالیت، باید افشا شود:
- ماهیت عدم اطمینان
- دلایل تردید

### ۳. عدم تداوم (بند ۲۴)
در صورت عدم تداوم فعالیت:
- صورت‌های مالی نباید بر مبنای تداوم فعالیت تهیه شوند
- باید افشای مبنا، دلایل و واقعیت‌ها

## 📌 نشانه‌های هشدار (Red Flags)

### نشانه‌های مالی:
- زیان انباشته > ۵۰٪ سرمایه
- نسبت جاری < ۱
- جریان نقدی عملیاتی منفی
- بدهی‌های سررسیدشده پرداخت‌نشده
- نسبت بدهی به حقوق مالکانه > ۲
- کاهش شدید درآمد

### نشانه‌های عملیاتی:
- از دست دادن بازار اصلی
- از دست دادن مدیران کلیدی
- کمبود نیروی کار ماهر
- توقف تولید
- فسخ قراردادهای مهم

### نشانه‌های دیگر:
- عدم رعایت الزامات سرمایه
- دعاوی حقوقی مهم
- تغییرات مقرراتی نامطلوب
- بلایای طبیعی

## 📌 الگوریتم نرم‌افزاری

```python
def assess_going_concern(financial_data):
    """
    ارزیابی تداوم فعالیت
    
    Returns:
        dict: {
            'concern_level': 'low' | 'medium' | 'high' | 'severe',
            'indicators': [...],
            'disclosure_required': bool
        }
    """
    indicators = []
    score = 0
    
    # ۱. زیان انباشته
    if financial_data['retained_earnings'] < 0:
        loss_ratio = abs(financial_data['retained_earnings']) / financial_data['capital']
        if loss_ratio > 0.5:
            indicators.append({
                'type': 'accumulated_losses',
                'severity': 'high',
                'message': f'زیان انباشته {loss_ratio*100:.1f}٪ سرمایه'
            })
            score += 3
        elif loss_ratio > 0.25:
            indicators.append({
                'type': 'accumulated_losses',
                'severity': 'medium',
                'message': f'زیان انباشته {loss_ratio*100:.1f}٪ سرمایه'
            })
            score += 2
    
    # ۲. نسبت جاری
    current_ratio = financial_data['current_assets'] / financial_data['current_liabilities']
    if current_ratio < 1:
        indicators.append({
            'type': 'current_ratio',
            'severity': 'high' if current_ratio < 0.5 else 'medium',
            'message': f'نسبت جاری {current_ratio:.2f}'
        })
        score += 2 if current_ratio < 0.5 else 1
    
    # ۳. جریان نقدی عملیاتی
    if financial_data['operating_cash_flow'] < 0:
        indicators.append({
            'type': 'negative_operating_cash_flow',
            'severity': 'medium',
            'message': 'جریان نقدی عملیاتی منفی'
        })
        score += 2
    
    # ۴. نسبت بدهی
    debt_ratio = financial_data['total_liabilities'] / financial_data['total_equity']
    if debt_ratio > 2:
        indicators.append({
            'type': 'high_debt_ratio',
            'severity': 'high' if debt_ratio > 4 else 'medium',
            'message': f'نسبت بدهی {debt_ratio:.2f}'
        })
        score += 2 if debt_ratio > 4 else 1
    
    # ۵. زیان دوره
    if financial_data['net_income'] < 0:
        indicators.append({
            'type': 'net_loss',
            'severity': 'medium',
            'message': 'زیان در دوره جاری'
        })
        score += 1
    
    # تعیین سطح
    if score >= 8:
        level = 'severe'
    elif score >= 5:
        level = 'high'
    elif score >= 3:
        level = 'medium'
    else:
        level = 'low'
    
    return {
        'concern_level': level,
        'score': score,
        'indicators': indicators,
        'disclosure_required': level in ('high', 'severe')
    }
    ## تداوم فعالیت

صورت‌های مالی این واحد تجاری بر مبنای تداوم فعالیت تهیه شده است.
با این حال، موارد زیر نشان‌دهنده وجود تردید اساسی در توانایی
واحد تجاری برای ادامه فعالیت است:

1. زیان انباشته به مبلغ XXX ریال (XX٪ سرمایه)
2. نسبت جاری XX٪ (کمتر از ۱)
3. جریان نقدی عملیاتی منفی به مبلغ XXX ریال

مدیریت در حال بررسی راهکارهای زیر است:
- افزایش سرمایه
- مذاکره با بانک‌ها برای تمدید وام‌ها
- کاهش هزینه‌ها

در صورت عدم موفقیت در این راهکارها، امکان تداوم فعالیت وجود ندارد.
استاندارد	کاربرد تداوم فعالیت
۱	افشای عدم اطمینان
۵	رویدادهای بعد از ترازنامه
۲۴	گزارشگری قبل از بهره‌برداری
۳۲	کاهش ارزش دارایی‌ها
۳۳	مزایای بازنشستگی
۳۶	ابزارهای مالی: ارائه
