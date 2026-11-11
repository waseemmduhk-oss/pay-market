"""
report_utils.py
أدوات مشتركة لتوليد التقارير الشهرية (Excel) — تُستخدم من لوحة الأدمن (لتقرير
شبكة واحدة) ولوحة المطوّر (لتقرير شبكة واحدة أو كل الشبكات مجمّعة معاً).
"""
import io
from datetime import datetime


ARABIC_MONTHS = {
    1: "يناير", 2: "فبراير", 3: "مارس", 4: "أبريل", 5: "مايو", 6: "يونيو",
    7: "يوليو", 8: "أغسطس", 9: "سبتمبر", 10: "أكتوبر", 11: "نوفمبر", 12: "ديسمبر"
}


def month_label(year, month):
    return f"{ARABIC_MONTHS.get(month, month)} {year}"


def _month_bounds(year, month):
    start = f"{year:04d}-{month:02d}-01 00:00:00"
    if month == 12:
        end = f"{year + 1:04d}-01-01 00:00:00"
    else:
        end = f"{year:04d}-{month + 1:02d}-01 00:00:00"
    return start, end


def _xlsx_safe(value):
    """
    حماية من "CSV/Excel Formula Injection": لو قيمة أي خلية تبدأ بـ = أو + أو
    - أو @، برامج زي Excel ممكن تفسّرها كصيغة (formula) بدل نص عادي عند فتح
    الملف — وهاي ممكن تُستغل لتسريب بيانات أو تنفيذ أوامر (خصوصاً بصيغ DDE
    القديمة). كل الحقول المصدَّرة هون (هاتف/كرت/رمز محفظة) مُتحقَّق من
    صيغتها الرقمية بأماكن تانية بالكود حالياً، لكن هاي طبقة حماية إضافية لو
    تغيّر التحقق مستقبلاً أو أُضيف حقل نصي حر لاحقاً.
    """
    if value is None:
        return value
    s = str(value)
    if s and s[0] in ('=', '+', '-', '@'):
        return "'" + s
    return value


def build_monthly_report_workbook(conn, year, month, operator_id=None):
    """
    يبني ملف Excel لتقرير شهر معيّن.
    - لو تم تمرير operator_id: التقرير خاص بهذا المشغّل فقط (بدون عمود اسم المشغّل).
    - لو operator_id=None: تقرير مجمّع لكل المشغّلين (مع عمود اسم المشغّل)، متاح لحساب المطوّر فقط.
    يرجّع bytes جاهزة للتنزيل.
    """
    import openpyxl
    from openpyxl.styles import Font, PatternFill, Alignment

    start, end = _month_bounds(year, month)

    base_query = '''
        SELECT o.name AS operator_name, t.transaction_id, t.customer_phone, t.wallet_code,
               t.amount_paid, t.created_at, v.username, v.password, v.price, v.profile
        FROM transactions t
        JOIN vouchers v ON t.voucher_id = v.id
        JOIN operators o ON t.operator_id = o.id
        WHERE t.created_at >= ? AND t.created_at < ?
    '''
    params = [start, end]
    if operator_id is not None:
        base_query += ' AND t.operator_id = ?'
        params.append(operator_id)
    base_query += ' ORDER BY t.created_at ASC'

    rows = conn.execute(base_query, params).fetchall()

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "المبيعات"
    ws.sheet_view.rightToLeft = True

    header_fill = PatternFill(start_color="4F46E5", end_color="4F46E5", fill_type="solid")
    header_font = Font(color="FFFFFF", bold=True, size=11)
    center = Alignment(horizontal="center", vertical="center")

    if operator_id is None:
        headers = ["المشغّل", "رقم العملية", "رقم الهاتف", "رمز المحفظة", "اسم المستخدم",
                   "كلمة المرور", "السعر", "الباقة", "التاريخ"]
    else:
        headers = ["رقم العملية", "رقم الهاتف", "رمز المحفظة", "اسم المستخدم",
                   "كلمة المرور", "السعر", "الباقة", "التاريخ"]

    ws.append(headers)
    for cell in ws[1]:
        cell.fill = header_fill
        cell.font = header_font
        cell.alignment = center

    total_revenue = 0.0
    for r in rows:
        total_revenue += r['amount_paid'] or 0
        if operator_id is None:
            ws.append([_xlsx_safe(r['operator_name']), _xlsx_safe(r['transaction_id']), _xlsx_safe(r['customer_phone']), _xlsx_safe(r['wallet_code']),
                       _xlsx_safe(r['username']), _xlsx_safe(r['password']), r['price'], _xlsx_safe(r['profile']), r['created_at']])
        else:
            ws.append([_xlsx_safe(r['transaction_id']), _xlsx_safe(r['customer_phone']), _xlsx_safe(r['wallet_code']),
                       _xlsx_safe(r['username']), _xlsx_safe(r['password']), r['price'], _xlsx_safe(r['profile']), r['created_at']])

    for col in ws.columns:
        max_len = max((len(str(c.value)) for c in col if c.value is not None), default=10)
        ws.column_dimensions[col[0].column_letter].width = min(max(max_len + 2, 10), 30)

    # ── صفحة ملخص ──
    summary = wb.create_sheet("ملخص")
    summary.sheet_view.rightToLeft = True
    summary.append(["الشهر", month_label(year, month)])
    summary.append(["عدد العمليات", len(rows)])
    summary.append(["إجمالي الإيرادات (₪)", round(total_revenue, 2)])
    if operator_id is None:
        by_op = {}
        for r in rows:
            by_op.setdefault(r['operator_name'], [0, 0.0])
            by_op[r['operator_name']][0] += 1
            by_op[r['operator_name']][1] += r['amount_paid'] or 0
        summary.append([])
        summary.append(["المشغّل", "عدد العمليات", "الإيراد (₪)"])
        for cell in summary[summary.max_row]:
            cell.font = Font(bold=True)
        for name, (count, rev) in sorted(by_op.items(), key=lambda x: -x[1][1]):
            summary.append([name, count, round(rev, 2)])
    for cell in summary['A']:
        cell.font = Font(bold=True)
    summary.column_dimensions['A'].width = 24
    summary.column_dimensions['B'].width = 18
    summary.column_dimensions['C'].width = 16

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def daily_revenue_series(conn, operator_id=None, days=30):
    """
    يرجّع قائمة (تاريخ, إيراد) لآخر N يوم — لعمل رسم بياني مبسّط.
    لو operator_id=None بيجمع إيرادات كل المشغّلين معاً.
    يقرأ من revenue_log (سجل دائم) وليس transactions، لأن الأخير يُحذف مع
    البطاقات المصفّرة من لوحة المشغّل، مما كان يجعل الرسم البياني يتغيّر
    بأثر رجعي ويناقض رقم "إجمالي الإيرادات" الثابت المعروض بجانبه.
    """
    query = '''
        SELECT date(occurred_at) AS d, COALESCE(SUM(amount), 0) AS rev
        FROM revenue_log
        WHERE occurred_at >= date('now', ?)
    '''
    params = [f'-{days - 1} days']
    if operator_id is not None:
        query += ' AND operator_id = ?'
        params.append(operator_id)
    query += ' GROUP BY date(occurred_at)'

    rows = {r['d']: r['rev'] for r in conn.execute(query, params).fetchall()}

    from datetime import date, timedelta
    today = date.today()
    series = []
    for i in range(days - 1, -1, -1):
        d = today - timedelta(days=i)
        key = d.isoformat()
        series.append({'date': d.strftime('%m-%d'), 'revenue': round(rows.get(key, 0), 2)})
    return series
