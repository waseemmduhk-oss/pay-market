from flask import request, session, redirect, url_for, current_app, g, jsonify, send_from_directory
import json as _json
import html as _html_mod
import os
import secrets as _secrets_mod
import re
import urllib.request as _urlreq
from urllib.parse import quote as _urlquote
from werkzeug.security import generate_password_hash as _gen_hash

from .common import admin_bp, get_db_connection, write_audit, _require_admin_login
from routes_payment import _expire_stale_orders, _confirm_order, _cancel_order, _pay_lock, RECEIPTS_DIR


def html_lib_escape(s):
    return _html_mod.escape(s, quote=True)


# ═══════════════════════════════════════════════════════════════════════════
# ── إدارة الدفعات (الطلبات المعلّقة) ─────────────────────────────────────────
# ═══════════════════════════════════════════════════════════════════════════
def _payments_page_html(operator, slug, orders, stats, csrf_token, message=None,
                         search_date=None, revenue_search=None):
    msg_html = ""
    if message:
        kind, text = message
        color = "emerald" if kind == "success" else "rose"
        icon  = "fa-circle-check" if kind == "success" else "fa-circle-exclamation"
        msg_html = f'''<div class="mb-5 bg-{color}-50 dark:bg-{color}-950/20 border border-{color}-100 dark:border-{color}-900/30 text-{color}-600 dark:text-{color}-400 text-sm font-bold px-4 py-3 rounded-xl flex items-center gap-2"><i class="fa-solid {icon}"></i>{text}</div>'''

    revenue_clear_link_html = ''
    if search_date:
        revenue_clear_link_html = f'<a href="/{slug}/admin/payments" class="bg-slate-100 dark:bg-[#0B1329] hover:bg-slate-200 text-slate-600 dark:text-slate-400 px-4 py-2.5 rounded-xl text-xs font-bold flex items-center gap-1"><i class="fa-solid fa-xmark"></i> مسح</a>'

    revenue_result_html = ''
    if revenue_search is not None:
        revenue_result_html = f'''<div class="mt-3 flex items-center gap-4 bg-indigo-50 dark:bg-indigo-950/20 border border-indigo-100 dark:border-indigo-900/30 rounded-xl px-4 py-3">
            <span class="text-xs font-bold text-indigo-500 dark:text-indigo-400"><i class="fa-solid fa-circle-check"></i> {revenue_search['count']} عملية مؤكَّدة بتاريخ {search_date}</span>
            <span class="text-sm font-extrabold text-indigo-700 dark:text-indigo-300" dir="ltr">{revenue_search['revenue']:.2f} ₪</span>
        </div>'''

    status_meta = {
        'pending':   ('bg-amber-50 dark:bg-amber-950/30 text-amber-600 dark:text-amber-400',   'fa-clock',        'بانتظار الإشعار'),
        'review':    ('bg-orange-50 dark:bg-orange-950/30 text-orange-600 dark:text-orange-400', 'fa-magnifying-glass', 'يحتاج مراجعة'),
        'confirmed': ('bg-emerald-50 dark:bg-emerald-950/30 text-emerald-600 dark:text-emerald-400', 'fa-circle-check', 'مؤكَّد'),
        'expired':   ('bg-slate-100 dark:bg-[#0B1329] text-slate-500 dark:text-slate-400',      'fa-hourglass-end', 'منتهي الصلاحية'),
        'cancelled': ('bg-rose-50 dark:bg-rose-950/30 text-rose-500 dark:text-rose-400',        'fa-ban',          'ملغى'),
    }

    ocr_status_label = {
        'matched':      'تطابق آلي',
        'mismatch':     'مبلغ غير مطابق',
        'no_amount':    'لم يُقرأ مبلغ',
        'duplicate':    'إشعار/تحويل مستخدَم سابقاً!',
        'old_receipt':  'تاريخ الإشعار قديم!',
        'manual_entry': 'إدخال يدوي (بدون صورة)',
    }

    rows_html = ""
    if not orders:
        rows_html = '''<tr><td colspan="7" class="text-center text-slate-400 dark:text-slate-500 text-sm py-12">
            <i class="fa-solid fa-inbox text-2xl mb-2 block"></i>لا توجد طلبات دفع حتى الآن</td></tr>'''
    else:
        for o in orders:
            cls, icon, label = status_meta.get(o['status'], ('bg-slate-100 text-slate-500', 'fa-question', o['status']))
            n_cards = len(_json.loads(o['voucher_ids']))
            receipt_html = '<span class="text-slate-300 dark:text-slate-600 text-xs">—</span>'
            if o['receipt_filename']:
                receipt_url = url_for('admin.view_receipt', slug=slug, order_id=o['id'])
                ocr_label = ocr_status_label.get(o['ocr_match_status'], o['ocr_match_status'] or '')
                ocr_amt = f" ({o['ocr_parsed_amount']:.2f}₪)" if o['ocr_parsed_amount'] is not None else ''
                receipt_html = f'''<a href="{receipt_url}" target="_blank" class="text-indigo-500 dark:text-indigo-400 text-[11px] font-bold flex flex-col items-start gap-0.5">
                    <span><i class="fa-solid fa-image"></i> عرض الإشعار</span>
                    <span class="text-[10px] text-slate-400 dark:text-slate-500 font-normal">{ocr_label}{ocr_amt}</span>
                </a>'''
            elif o['ocr_match_status'] == 'manual_entry' or (o['ocr_match_status'] == 'duplicate' and o['ocr_reference']):
                ocr_label = ocr_status_label.get(o['ocr_match_status'], o['ocr_match_status'] or '')
                receipt_html = f'''<div class="text-[11px] text-slate-600 dark:text-slate-300">
                    <span class="font-bold"><i class="fa-solid fa-keyboard"></i> {ocr_label}</span>
                    <span class="block text-[10px] text-slate-400 dark:text-slate-500 font-mono" dir="ltr">رمز: {html_lib_escape(o['ocr_reference'] or '')}</span>
                </div>'''
            actions = '<span class="text-slate-300 dark:text-slate-600 text-xs">—</span>'
            if o['status'] in ('pending', 'review'):
                actions = f'''
                <div class="flex gap-1.5 justify-end">
                    <form method="POST" action="{url_for('admin.confirm_pending_order', slug=slug, order_id=o['id'])}">
                        <input type="hidden" name="_csrf_token" value="{csrf_token}">
                        <button class="bg-emerald-500 hover:bg-emerald-600 text-white text-[11px] font-bold px-3 py-1.5 rounded-lg transition-colors"><i class="fa-solid fa-check"></i> تأكيد</button>
                    </form>
                    <form method="POST" action="{url_for('admin.cancel_pending_order', slug=slug, order_id=o['id'])}">
                        <input type="hidden" name="_csrf_token" value="{csrf_token}">
                        <button class="bg-rose-50 hover:bg-rose-100 dark:bg-rose-950/30 dark:hover:bg-rose-950/50 text-rose-600 dark:text-rose-400 text-[11px] font-bold px-3 py-1.5 rounded-lg transition-colors"><i class="fa-solid fa-xmark"></i> إلغاء</button>
                    </form>
                </div>'''
            rows_html += f'''
            <tr class="border-b border-slate-100 dark:border-[#3A506B]/20 last:border-0 hover:bg-slate-50 dark:hover:bg-[#1C2541]/40 transition-colors">
                <td class="py-3.5 px-3 text-xs font-mono font-bold text-slate-700 dark:text-slate-300" dir="ltr">{o['customer_phone']}</td>
                <td class="py-3.5 px-3 text-xs text-slate-500 dark:text-slate-400">{n_cards} بطاقة</td>
                <td class="py-3.5 px-3 text-sm font-mono font-extrabold text-indigo-600 dark:text-indigo-400" dir="ltr">{o['exact_amount']:.2f} ₪</td>
                <td class="py-3.5 px-3 text-[11px] text-slate-400 dark:text-slate-500 font-mono">{o['created_at']}</td>
                <td class="py-3.5 px-3"><span class="{cls} text-[10px] font-bold px-2.5 py-1 rounded-full inline-flex items-center gap-1"><i class="fa-solid {icon}"></i>{label}</span></td>
                <td class="py-3.5 px-3">{receipt_html}</td>
                <td class="py-3.5 px-3">{actions}</td>
            </tr>'''

    telegram_bot_token = html_lib_escape(operator['telegram_bot_token'] or '')
    telegram_chat_id   = html_lib_escape(operator['telegram_chat_id'] or '')
    telegram_connected = bool(operator['telegram_bot_token'] and operator['telegram_chat_id'])
    telegram_buttons_active = bool(operator['telegram_webhook_secret'])
    if telegram_connected:
        if telegram_buttons_active:
            telegram_status_html = '''<div class="bg-emerald-50 dark:bg-emerald-950/20 border border-emerald-100 dark:border-emerald-900/30 text-emerald-600 dark:text-emerald-400 text-[11px] font-bold px-3 py-2 rounded-lg mb-3 flex items-center gap-2">
                <i class="fa-solid fa-circle-check"></i> أزرار تأكيد/رفض مفعّلة وشغّالة.</div>'''
        else:
            telegram_status_html = '''<div class="bg-rose-50 dark:bg-rose-950/20 border border-rose-100 dark:border-rose-900/30 text-rose-600 dark:text-rose-400 text-[11px] font-bold px-3 py-2 rounded-lg mb-3 flex items-center gap-2">
                <i class="fa-solid fa-triangle-exclamation"></i> الرسائل بتوصل، بس أزرار تأكيد/رفض <u>غير مفعّلة</u> — اضغط "حفظ" تحت (حتى بدون تغيير شي) لإعادة محاولة التفعيل.</div>'''
    else:
        telegram_status_html = ''

    return f'''<!DOCTYPE html>
<html lang="ar" dir="rtl">
<head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>إدارة الدفعات</title>
<script src="https://cdn.tailwindcss.com"></script>
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css">
<style>@import url('https://fonts.googleapis.com/css2?family=Cairo:wght@400;500;600;700;800&display=swap'); body{{font-family:'Cairo',sans-serif;}}</style>
<script>
    tailwind.config = {{ darkMode: 'class' }}
    if (localStorage.getItem('theme') === 'dark' || (!('theme' in localStorage) && window.matchMedia('(prefers-color-scheme: dark)').matches)) {{
        document.documentElement.classList.add('dark');
    }}
    function toggleTheme() {{
        const html = document.documentElement;
        html.classList.toggle('dark');
        localStorage.setItem('theme', html.classList.contains('dark') ? 'dark' : 'light');
        document.getElementById('themeIcon').className = html.classList.contains('dark') ? 'fa-solid fa-sun' : 'fa-solid fa-moon';
    }}
    function openModal(id) {{ document.getElementById(id).classList.remove('hidden'); }}
    function closeModal(id) {{ document.getElementById(id).classList.add('hidden'); }}
</script>
</head>
<body class="bg-slate-50 dark:bg-[#0B1329] min-h-screen p-4 sm:p-8 transition-colors">
<div class="max-w-5xl mx-auto">
    <div class="flex items-center justify-between mb-4 flex-wrap gap-3">
        <div>
            <h1 class="text-xl font-extrabold text-slate-800 dark:text-white flex items-center gap-2"><i class="fa-solid fa-money-bill-transfer text-indigo-500"></i> إدارة الدفعات</h1>
            <p class="text-xs text-slate-400 dark:text-slate-500 mt-1">{operator['name']}</p>
        </div>
        <div class="flex items-center gap-2">
            <button onclick="toggleTheme()" class="bg-white dark:bg-[#1C2541] border border-slate-200 dark:border-[#3A506B]/40 text-slate-600 dark:text-indigo-400 w-9 h-9 rounded-lg text-xs"><i id="themeIcon" class="fa-solid fa-moon"></i></button>
            <a href="{url_for('admin.admin_dashboard', slug=slug)}" class="bg-white dark:bg-[#1C2541] border border-slate-200 dark:border-[#3A506B]/40 text-indigo-600 dark:text-indigo-400 px-4 py-2 rounded-lg text-xs font-bold"><i class="fa-solid fa-arrow-right"></i> العودة للوحة</a>
        </div>
    </div>

    <div class="flex flex-wrap gap-2 mb-6">
        <a href="{url_for('admin.admin_customers', slug=slug)}" class="bg-white dark:bg-[#1C2541] border border-slate-200 dark:border-[#3A506B]/40 text-slate-700 dark:text-slate-200 px-4 py-2.5 rounded-xl text-xs font-bold flex items-center gap-2 hover:border-indigo-300 dark:hover:border-indigo-500/50 transition-colors">
            <i class="fa-solid fa-users text-indigo-500"></i> حسابات الزبائن
        </a>
        <button onclick="openModal('telegramModal')" class="bg-white dark:bg-[#1C2541] border border-slate-200 dark:border-[#3A506B]/40 text-slate-700 dark:text-slate-200 px-4 py-2.5 rounded-xl text-xs font-bold flex items-center gap-2 hover:border-indigo-300 dark:hover:border-indigo-500/50 transition-colors">
            <i class="fa-brands fa-telegram text-indigo-500"></i> تنبيه تيليجرام
            {'<span class="bg-emerald-50 dark:bg-emerald-950/30 text-emerald-600 dark:text-emerald-400 text-[9px] font-bold px-1.5 py-0.5 rounded-full">مفعّل</span>' if telegram_connected else ''}
        </button>
    </div>

    {msg_html}

    <div class="grid grid-cols-2 sm:grid-cols-4 gap-3 mb-6">
        <div class="bg-white dark:bg-[#1C2541] rounded-2xl p-4 border border-slate-100 dark:border-[#3A506B]/30 shadow-sm">
            <p class="text-[10px] font-bold text-amber-500 mb-1"><i class="fa-solid fa-clock"></i> معلّقة الآن</p>
            <p class="text-2xl font-extrabold text-slate-800 dark:text-white">{stats['pending']}</p>
        </div>
        <div class="bg-white dark:bg-[#1C2541] rounded-2xl p-4 border border-slate-100 dark:border-[#3A506B]/30 shadow-sm">
            <p class="text-[10px] font-bold text-emerald-500 mb-1"><i class="fa-solid fa-circle-check"></i> مؤكَّدة اليوم</p>
            <p class="text-2xl font-extrabold text-slate-800 dark:text-white">{stats['confirmed_today']}</p>
        </div>
        <div class="bg-white dark:bg-[#1C2541] rounded-2xl p-4 border border-slate-100 dark:border-[#3A506B]/30 shadow-sm">
            <p class="text-[10px] font-bold text-indigo-500 mb-1"><i class="fa-solid fa-sack-dollar"></i> إيراد اليوم</p>
            <p class="text-2xl font-extrabold text-slate-800 dark:text-white" dir="ltr">{stats['revenue_today']:.2f} ₪</p>
        </div>
        <div class="bg-white dark:bg-[#1C2541] rounded-2xl p-4 border border-slate-100 dark:border-[#3A506B]/30 shadow-sm">
            <p class="text-[10px] font-bold text-orange-500 mb-1"><i class="fa-solid fa-triangle-exclamation"></i> تحتاج مراجعة</p>
            <p class="text-2xl font-extrabold text-slate-800 dark:text-white">{stats['needs_review']}</p>
        </div>
    </div>

    <!-- بحث إيرادات يوم معيّن -->
    <div class="bg-white dark:bg-[#1C2541] rounded-2xl p-4 border border-slate-100 dark:border-[#3A506B]/30 shadow-sm mb-6">
        <p class="text-xs font-bold text-slate-600 dark:text-slate-300 mb-3"><i class="fa-solid fa-calendar-days text-indigo-500"></i> بحث الإيراد بيوم معيّن</p>
        <form method="GET" action="/{slug}/admin/payments" class="flex flex-wrap gap-2">
            <input type="date" name="search_date" value="{search_date or ''}"
                class="px-3 py-2.5 bg-slate-50 dark:bg-[#0B1329] border border-slate-200 dark:border-[#3A506B]/50 rounded-xl text-xs font-semibold focus:outline-none focus:border-indigo-400 dark:text-slate-300">
            <button type="submit" class="bg-indigo-600 hover:bg-indigo-700 text-white px-5 py-2.5 rounded-xl text-xs font-bold flex items-center gap-1">
                <i class="fa-solid fa-magnifying-glass"></i> بحث
            </button>
            {revenue_clear_link_html}
        </form>
        {revenue_result_html}
        <p class="text-[10px] text-slate-400 mt-2">ملاحظة: التاريخ محسوب حسب توقيت UTC المخزّن بقاعدة البيانات، فقد يختلف حد بداية اليوم بضع ساعات عن توقيتك المحلي.</p>
    </div>
    <!-- Modal: تنبيه تيليجرام -->
    <div id="telegramModal" class="hidden fixed inset-0 bg-slate-900/60 backdrop-blur-sm flex items-center justify-center z-50 p-4">
        <div class="bg-white dark:bg-[#1C2541] rounded-2xl p-6 max-w-md w-full">
            <div class="flex items-center justify-between mb-3">
                <p class="text-sm font-bold text-slate-700 dark:text-slate-200">
                    <i class="fa-brands fa-telegram text-indigo-500"></i> تنبيهات تيليجرام
                </p>
                <button onclick="closeModal('telegramModal')" class="text-slate-400 hover:text-slate-600 dark:hover:text-slate-200"><i class="fa-solid fa-xmark"></i></button>
            </div>
            <p class="text-[11px] text-slate-400 dark:text-slate-500 mb-3">
                بعد ربط بوت تيليجرام هون، رح توصلك رسالة فورية بحالتين: (1) إشعار دفع مرفوع يحتاج مراجعة يدوية
                (لم يتطابق مبلغه آلياً، أو نفس الصورة استُخدمت من قبل) — <strong>مع صورة الإيصال نفسها وزرَّي
                "تأكيد" و"رفض" تقدر تضغطهم من المحادثة مباشرة</strong> بدون ما تفتح لوحة الأدمن، و(2) انخفاض
                مخزون أي فئة بطاقات عن الحد الأدنى.
                أنشئ بوت مجاني عبر <strong dir="ltr">@BotFather</strong> بتيليجرام،
                وخذ التوكن ورقم الـ Chat ID من <strong dir="ltr">@userinfobot</strong>، ثم الصقهما هنا.
            </p>
            {telegram_status_html}
            <form method="POST" action="{url_for('admin.save_telegram_settings', slug=slug)}" class="space-y-2">
                <input type="hidden" name="_csrf_token" value="{csrf_token}">
                <input type="text" name="telegram_bot_token" value="{telegram_bot_token}" placeholder="Bot Token (مثال: 123456:ABC-DEF...)"
                       class="w-full bg-slate-50 dark:bg-[#0B1329] border border-slate-200 dark:border-[#3A506B]/30 rounded-lg px-3 py-2 text-xs font-mono" dir="ltr">
                <input type="text" name="telegram_chat_id" value="{telegram_chat_id}" placeholder="Chat ID (مثال: 123456789)"
                       class="w-full bg-slate-50 dark:bg-[#0B1329] border border-slate-200 dark:border-[#3A506B]/30 rounded-lg px-3 py-2 text-xs font-mono" dir="ltr">
                <button class="bg-indigo-500 hover:bg-indigo-600 text-white text-xs font-bold px-4 py-2 rounded-lg"><i class="fa-solid fa-floppy-disk"></i> حفظ</button>
            </form>
        </div>
    </div>

    <div class="bg-white dark:bg-[#1C2541] rounded-2xl border border-slate-100 dark:border-[#3A506B]/30 shadow-sm overflow-hidden mb-6">
        <div class="px-5 py-4 border-b border-slate-100 dark:border-[#3A506B]/20">
            <h2 class="text-sm font-bold text-slate-700 dark:text-slate-200"><i class="fa-solid fa-list text-indigo-400"></i> آخر الطلبات</h2>
        </div>

        <div class="overflow-x-auto">
        <table class="w-full text-right min-w-[640px]">
            <thead><tr class="border-b border-slate-100 dark:border-[#3A506B]/20 text-[11px] text-slate-400 dark:text-slate-500 font-bold">
                <th class="py-3 px-3">الهاتف</th><th class="py-3 px-3">البطاقات</th><th class="py-3 px-3">المبلغ</th>
                <th class="py-3 px-3">التاريخ</th><th class="py-3 px-3">الحالة</th><th class="py-3 px-3">الإشعار</th><th class="py-3 px-3"></th>
            </tr></thead>
            <tbody>{rows_html}</tbody>
        </table>
        </div>
    </div>
</div>
</body></html>'''


@admin_bp.route('/<slug>/admin/payments')
def admin_payments(slug):
    if not _require_admin_login(slug):
        return redirect(url_for('admin.admin_login', slug=slug))
    operator    = g.operator
    operator_id = operator['id']
    conn = get_db_connection()
    _expire_stale_orders(conn, operator_id)
    orders = conn.execute(
        "SELECT * FROM pending_orders WHERE operator_id=? ORDER BY id DESC LIMIT 100", (operator_id,)
    ).fetchall()
    stats = {
        'pending': conn.execute(
            "SELECT COUNT(*) FROM pending_orders WHERE operator_id=? AND status='pending'", (operator_id,)
        ).fetchone()[0],
        'confirmed_today': conn.execute(
            "SELECT COUNT(*) FROM pending_orders WHERE operator_id=? AND status='confirmed' AND date(confirmed_at)=date('now')",
            (operator_id,)
        ).fetchone()[0],
        'revenue_today': conn.execute(
            "SELECT COALESCE(SUM(exact_amount),0) FROM pending_orders WHERE operator_id=? AND status='confirmed' AND date(confirmed_at)=date('now')",
            (operator_id,)
        ).fetchone()[0],
        'needs_review': conn.execute(
            "SELECT COUNT(*) FROM pending_orders WHERE operator_id=? AND status='review'", (operator_id,)
        ).fetchone()[0],
    }
    search_date = request.args.get('search_date', '').strip()
    revenue_search = None
    if search_date:
        row = conn.execute(
            "SELECT COUNT(*) AS c, COALESCE(SUM(exact_amount),0) AS r FROM pending_orders "
            "WHERE operator_id=? AND status='confirmed' AND date(confirmed_at)=?",
            (operator_id, search_date)
        ).fetchone()
        revenue_search = {'count': row['c'], 'revenue': row['r']}
    conn.close()
    return _payments_page_html(operator, slug, orders, stats, current_app.generate_csrf_token(),
                                session.pop('payments_message', None),
                                search_date=search_date, revenue_search=revenue_search)


@admin_bp.route('/<slug>/admin/payments/<int:order_id>/receipt')
def view_receipt(slug, order_id):
    """يعرض صورة/PDF إشعار الدفع المرفوع لطلب معيّن — محمي بتسجيل دخول الأدمن
    فقط (الملفات مخزّنة خارج static/ عمداً لهذا السبب، انظر RECEIPTS_DIR)."""
    if not _require_admin_login(slug): return "غير مسموح", 403
    operator_id = g.operator['id']
    conn = get_db_connection()
    order = conn.execute(
        "SELECT receipt_filename FROM pending_orders WHERE id=? AND operator_id=?",
        (order_id, operator_id)
    ).fetchone()
    conn.close()
    if not order or not order['receipt_filename']:
        return "لا يوجد إشعار مرفوع لهذا الطلب", 404
    directory = os.path.join(RECEIPTS_DIR, str(operator_id))
    return send_from_directory(directory, order['receipt_filename'])


@admin_bp.route('/<slug>/admin/payments/<int:order_id>/confirm', methods=['POST'])
def confirm_pending_order(slug, order_id):
    if not _require_admin_login(slug): return "غير مسموح", 403
    current_app.validate_csrf()
    operator_id = g.operator['id']
    with _pay_lock:
        conn = get_db_connection()
        try:
            conn.execute("BEGIN EXCLUSIVE")
            order = conn.execute(
                "SELECT * FROM pending_orders WHERE id=? AND operator_id=? AND status IN ('pending','review')",
                (order_id, operator_id)
            ).fetchone()
            if order:
                _confirm_order(conn, order)
                write_audit("PAYMENT_MANUAL_CONFIRM", f"تأكيد يدوي لطلب #{order_id} بمبلغ {order['exact_amount']}₪", operator_id=operator_id)
                session['payments_message'] = ('success', f'تم تأكيد الطلب #{order_id} يدوياً وتسليم البطاقات.')
            else:
                session['payments_message'] = ('error', 'الطلب غير موجود أو تم التعامل معه مسبقاً.')
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            session['payments_message'] = ('error', 'حدث خطأ أثناء التأكيد.')
        finally:
            conn.close()
    return redirect(url_for('admin.admin_payments', slug=slug))


@admin_bp.route('/<slug>/admin/payments/<int:order_id>/cancel', methods=['POST'])
def cancel_pending_order(slug, order_id):
    if not _require_admin_login(slug): return "غير مسموح", 403
    current_app.validate_csrf()
    operator_id = g.operator['id']
    with _pay_lock:
        conn = get_db_connection()
        try:
            conn.execute("BEGIN EXCLUSIVE")
            order = conn.execute(
                "SELECT * FROM pending_orders WHERE id=? AND operator_id=? AND status IN ('pending','review')",
                (order_id, operator_id)
            ).fetchone()
            if order:
                _cancel_order(conn, order)
                write_audit("PAYMENT_MANUAL_CANCEL", f"إلغاء يدوي لطلب #{order_id}", operator_id=operator_id)
                session['payments_message'] = ('success', f'تم إلغاء الطلب #{order_id} وفكّ حجز بطاقاته.')
            else:
                session['payments_message'] = ('error', 'الطلب غير موجود أو تم التعامل معه مسبقاً.')
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            session['payments_message'] = ('error', 'حدث خطأ أثناء الإلغاء.')
        finally:
            conn.close()
    return redirect(url_for('admin.admin_payments', slug=slug))


@admin_bp.route('/<slug>/admin/payments/telegram-settings', methods=['POST'])
def save_telegram_settings(slug):
    if not _require_admin_login(slug): return "غير مسموح", 403
    current_app.validate_csrf()
    operator_id = g.operator['id']
    bot_token = request.form.get('telegram_bot_token', '').strip()
    chat_id   = request.form.get('telegram_chat_id', '').strip()

    conn = get_db_connection()
    current = conn.execute("SELECT telegram_webhook_secret FROM operators WHERE id=?", (operator_id,)).fetchone()
    webhook_secret = current['telegram_webhook_secret'] if current else None

    # نسجّل رابط webhook عند تيليجرام حتى تشتغل أزرار "تأكيد/رفض" اللي بترسل
    # مع صورة الإيصال — بدون هذا التسجيل، الأزرار تظهر بس الضغط عليها ما بوصل
    # للتطبيق أبداً (تيليجرام ما بعرف لوين يوجّه الضغطة)
    webhook_msg = ''
    webhook_ok  = False
    if bot_token and chat_id:
        if not webhook_secret:
            webhook_secret = _secrets_mod.token_hex(20)
        # _external=True + _scheme='https' يضمن رابط https:// دايماً بغض النظر
        # عن إعداد الـreverse proxy (request.host_url ممكن يرجّع http:// لو
        # ProxyFix/PROXY_LAYERS مو مضبوطين تماماً، وتيليجرام يرفض أي webhook
        # مو https بصمت تقريباً — هذا كان السبب الأرجح لعدم ظهور/عمل الأزرار)
        webhook_url = url_for('payment.telegram_webhook', webhook_secret=webhook_secret,
                               _external=True, _scheme='https')
        try:
            api_url = f"https://api.telegram.org/bot{bot_token}/setWebhook"
            data = _json.dumps({"url": webhook_url, "allowed_updates": ["callback_query"]}).encode('utf-8')
            req = _urlreq.Request(api_url, data=data, headers={"Content-Type": "application/json"})
            with _urlreq.urlopen(req, timeout=10) as resp:
                result = _json.loads(resp.read().decode('utf-8'))
            if result.get('ok'):
                webhook_ok = True
            else:
                desc = result.get('description', 'سبب غير معروف')
                webhook_msg = f' (تنبيه: تعذّر تفعيل أزرار تأكيد/رفض — رد تيليجرام: {desc})'
        except Exception as e:
            webhook_msg = f' (تنبيه: تعذّر الاتصال بتيليجرام لتفعيل الأزرار — {e})'
    else:
        webhook_secret = None   # ما في بوت مفعّل حالياً

    # لا نحفظ سرّاً محلياً يوهم إن الأزرار شغّالة لو التسجيل عند تيليجرام فشل
    # فعلياً — أفضل نوضّح إنها "غير مفعّلة" بدل أزرار تظهر وما بتستجيب
    if bot_token and chat_id and not webhook_ok:
        webhook_secret = None

    conn.execute("UPDATE operators SET telegram_bot_token=?, telegram_chat_id=?, telegram_webhook_secret=? WHERE id=?",
                 (bot_token or None, chat_id or None, webhook_secret, operator_id))
    conn.commit()
    conn.close()
    write_audit("TELEGRAM_SETTINGS_UPDATED", "تحديث إعدادات تنبيه تيليجرام", operator_id=operator_id)
    base_msg = 'تم حفظ إعدادات تيليجرام، والأزرار مفعّلة ✅.' if webhook_ok else 'تم حفظ إعدادات تيليجرام.'
    session['payments_message'] = ('success' if webhook_ok or not (bot_token and chat_id) else 'error', base_msg + webhook_msg)
    return redirect(url_for('admin.admin_payments', slug=slug))


# ═══════════════════════════════════════════════════════════════════════════
# حسابات الزبائن: بحث + إعادة ضبط كلمة المرور
# ═══════════════════════════════════════════════════════════════════════════
# المنصة بلا خدمة SMS، وكلمات مرور الزبائن مخزَّنة مشفّرة (ما بتنرسل أصلاً) — فالزبون
# اللي نسي كلمة مروره بيتواصل مع الشبكة (واتساب/اتصال) والمشغّل يعيد ضبطها من هون
# بضغطة: تتولّد كلمة جديدة تظهر مرة واحدة، مع زر يرسلها للزبون على واتساب.
def _customers_page_html(operator, slug, rows, q, csrf_token, notice=None):
    reset_base = url_for('admin.admin_customers', slug=slug)
    notice_html = ''
    if notice:
        kind = notice.get('kind', 'info')
        color = {'success': 'emerald', 'error': 'rose'}.get(kind, 'indigo')
        body = html_lib_escape(notice.get('text', ''))
        extra = ''
        if notice.get('password'):
            pw = notice['password']
            extra = f'''
            <div class="mt-3 flex flex-wrap items-center gap-2">
                <span class="font-mono text-2xl font-extrabold tracking-widest bg-white dark:bg-[#0B1329] border border-{color}-200 dark:border-{color}-900/40 rounded-xl px-4 py-2" dir="ltr" id="newPw">{html_lib_escape(pw)}</span>
                <button type="button" onclick="navigator.clipboard.writeText('{html_lib_escape(pw)}'); this.innerHTML='<i class=&quot;fa-solid fa-check&quot;></i> انتسخت'" class="bg-white dark:bg-[#1C2541] border border-slate-200 dark:border-[#3A506B]/40 text-slate-700 dark:text-slate-200 px-3 py-2 rounded-lg text-xs font-bold"><i class="fa-regular fa-copy"></i> نسخ</button>
            </div>
            <p class="text-[11px] mt-2 opacity-80">هذه الكلمة تظهر مرة واحدة فقط — سلّمها للزبون الآن.</p>'''
        notice_html = f'''<div class="mb-5 bg-{color}-50 dark:bg-{color}-950/20 border border-{color}-100 dark:border-{color}-900/30 text-{color}-700 dark:text-{color}-300 text-sm font-bold px-4 py-3 rounded-xl">
            <i class="fa-solid fa-circle-info"></i> {body}{extra}</div>'''

    if rows:
        rows_html = ''
        for r in rows:
            rows_html += f'''
            <tr class="border-b border-slate-100 dark:border-[#3A506B]/20 last:border-0">
                <td class="py-3.5 px-3 text-sm font-mono font-bold text-slate-700 dark:text-slate-200" dir="ltr">{html_lib_escape(r['phone'])}</td>
                <td class="py-3.5 px-3 text-xs text-slate-500 dark:text-slate-400">{r['cards']} بطاقة</td>
                <td class="py-3.5 px-3 text-[11px] text-slate-400 dark:text-slate-500 font-mono">{html_lib_escape(str(r['created_at'] or ''))[:16]}</td>
                <td class="py-3.5 px-3">
                    <button type="button" data-id="{r['id']}" data-phone="{html_lib_escape(r['phone'])}" onclick="openResetModal(this)"
                            class="bg-amber-50 hover:bg-amber-100 dark:bg-amber-950/30 dark:hover:bg-amber-950/50 text-amber-600 dark:text-amber-400 text-[11px] font-bold px-3 py-1.5 rounded-lg transition-colors"><i class="fa-solid fa-key"></i> إعادة ضبط كلمة المرور</button>
                </td>
            </tr>'''
    else:
        rows_html = '''<tr><td colspan="4" class="text-center text-slate-400 dark:text-slate-500 text-sm py-12">
            <i class="fa-solid fa-user-slash text-2xl mb-2 block"></i>لا يوجد زبائن مطابقون</td></tr>'''

    return f'''<!DOCTYPE html>
<html lang="ar" dir="rtl">
<head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>حسابات الزبائن</title>
<script src="https://cdn.tailwindcss.com"></script>
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css">
<style>@import url('https://fonts.googleapis.com/css2?family=Cairo:wght@400;500;600;700;800&display=swap'); body{{font-family:'Cairo',sans-serif;}}</style>
<script>
    tailwind.config = {{ darkMode: 'class' }}
    if (localStorage.getItem('theme') === 'dark' || (!('theme' in localStorage) && window.matchMedia('(prefers-color-scheme: dark)').matches)) {{
        document.documentElement.classList.add('dark');
    }}
    function openModal(id) {{ document.getElementById(id).classList.remove('hidden'); }}
    function closeModal(id) {{ document.getElementById(id).classList.add('hidden'); }}
    function openResetModal(btn) {{
        document.getElementById('resetPhone').textContent = btn.dataset.phone;
        document.getElementById('resetForm').action = "{reset_base}/" + btn.dataset.id + "/reset-password";
        openModal('resetModal');
    }}
    document.addEventListener('keydown', function(e) {{ if (e.key === 'Escape') closeModal('resetModal'); }});
</script>
</head>
<body class="bg-slate-50 dark:bg-[#0B1329] min-h-screen p-4 sm:p-8 transition-colors">
<div class="max-w-3xl mx-auto">
    <div class="flex items-center justify-between mb-5 flex-wrap gap-3">
        <div>
            <h1 class="text-xl font-extrabold text-slate-800 dark:text-white flex items-center gap-2"><i class="fa-solid fa-users text-indigo-500"></i> حسابات الزبائن</h1>
            <p class="text-xs text-slate-400 dark:text-slate-500 mt-1">{html_lib_escape(operator['name'] or '')}</p>
        </div>
        <a href="{url_for('admin.admin_payments', slug=slug)}" class="bg-white dark:bg-[#1C2541] border border-slate-200 dark:border-[#3A506B]/40 text-indigo-600 dark:text-indigo-400 px-4 py-2 rounded-lg text-xs font-bold"><i class="fa-solid fa-arrow-right"></i> العودة</a>
    </div>

    {notice_html}

    <form method="GET" action="{reset_base}" class="flex gap-2 mb-4">
        <input type="text" name="q" value="{html_lib_escape(q)}" inputmode="numeric" dir="ltr" placeholder="ابحث برقم الجوال (059...)"
               class="flex-1 min-w-0 px-4 py-2.5 bg-white dark:bg-[#1C2541] border border-slate-200 dark:border-[#3A506B]/40 rounded-xl text-sm text-slate-700 dark:text-slate-200">
        <button class="bg-indigo-600 hover:bg-indigo-700 text-white px-5 rounded-xl text-sm font-bold"><i class="fa-solid fa-magnifying-glass"></i> بحث</button>
    </form>

    <div class="bg-white dark:bg-[#1C2541] rounded-2xl border border-slate-100 dark:border-[#3A506B]/30 shadow-sm overflow-hidden">
        <div class="overflow-x-auto">
        <table class="w-full text-right min-w-[460px]">
            <thead><tr class="border-b border-slate-100 dark:border-[#3A506B]/20 text-[11px] text-slate-400 dark:text-slate-500 font-bold">
                <th class="py-3 px-3">رقم الجوال</th><th class="py-3 px-3">المشتريات</th><th class="py-3 px-3">تاريخ التسجيل</th><th class="py-3 px-3"></th>
            </tr></thead>
            <tbody>{rows_html}</tbody>
        </table>
        </div>
    </div>
    <p class="text-[11px] text-slate-400 dark:text-slate-500 mt-3">يُعرض آخر 50 حساباً. الزبون اللي نسي كلمة مروره بيتواصل معك (واتساب/اتصال)، وبتعيد ضبطها من هون وبتسلّمه الكلمة الجديدة.</p>
</div>

<div id="resetModal" class="hidden fixed inset-0 bg-slate-900/60 backdrop-blur-sm flex items-center justify-center z-50 p-4" onclick="if(event.target===this) closeModal('resetModal')">
    <div class="bg-white dark:bg-[#1C2541] rounded-2xl max-w-sm w-full p-6 shadow-2xl text-right border border-slate-100 dark:border-[#3A506B]/30">
        <div class="w-12 h-12 rounded-2xl bg-amber-50 dark:bg-amber-950/30 text-amber-500 flex items-center justify-center text-xl mb-4"><i class="fa-solid fa-key"></i></div>
        <h3 class="text-lg font-bold text-slate-800 dark:text-white mb-2">إعادة ضبط كلمة المرور</h3>
        <p class="text-sm text-slate-500 dark:text-slate-400 mb-1 leading-relaxed">سيتم توليد كلمة مرور جديدة لحساب الزبون:</p>
        <p class="font-mono font-extrabold text-slate-800 dark:text-slate-100 text-base mb-3" dir="ltr" id="resetPhone"></p>
        <p class="text-xs text-slate-400 dark:text-slate-500 mb-6 leading-relaxed">تتعطّل كلمة مروره الحالية فوراً، وتظهر لك الكلمة الجديدة مرة واحدة فقط لتسلّمها له.</p>
        <form id="resetForm" method="POST" action="">
            <input type="hidden" name="_csrf_token" value="{csrf_token}">
            <div class="flex gap-2 justify-end">
                <button type="button" onclick="closeModal('resetModal')" class="bg-slate-100 dark:bg-[#0B1329] hover:bg-slate-200 dark:hover:bg-[#3A506B]/30 px-4 py-2 rounded-lg text-sm font-bold text-slate-600 dark:text-slate-400">إلغاء</button>
                <button type="submit" class="bg-amber-500 hover:bg-amber-600 text-white px-4 py-2 rounded-lg text-sm font-bold"><i class="fa-solid fa-key"></i> تأكيد وإعادة الضبط</button>
            </div>
        </form>
    </div>
</div>
</body></html>'''


def _query_customers(operator_id, q):
    from routes_user import _ensure_customers_table   # استيراد متأخر لتفادي الاستيراد الدائري
    conn = get_db_connection()
    _ensure_customers_table(conn)
    digits = re.sub(r'\D', '', q or '')
    sql = ('''SELECT c.id, c.phone, c.created_at,
                  (SELECT COUNT(*) FROM transactions t WHERE t.customer_phone = c.phone AND t.operator_id = c.operator_id) AS cards
           FROM customers c WHERE c.operator_id = ?''')
    params = [operator_id]
    if digits:
        sql += " AND c.phone LIKE ?"
        params.append(f"%{digits}%")
    sql += " ORDER BY c.id DESC LIMIT 50"
    rows = conn.execute(sql, params).fetchall()
    conn.close()
    return rows


@admin_bp.route('/<slug>/admin/customers', methods=['GET'])
def admin_customers(slug):
    if not _require_admin_login(slug): return "غير مسموح", 403
    operator = g.operator
    q = (request.args.get('q') or '').strip()
    rows = _query_customers(operator['id'], q)
    return _customers_page_html(operator, slug, rows, q, current_app.generate_csrf_token())


@admin_bp.route('/<slug>/admin/customers/<int:customer_id>/reset-password', methods=['POST'])
def admin_reset_customer_password(slug, customer_id):
    if not _require_admin_login(slug): return "غير مسموح", 403
    current_app.validate_csrf()
    import rate_limit
    from routes_user import _ensure_customers_table
    operator = g.operator
    operator_id = operator['id']

    conn = get_db_connection()
    _ensure_customers_table(conn)
    cust = conn.execute("SELECT id, phone FROM customers WHERE id=? AND operator_id=?", (customer_id, operator_id)).fetchone()
    if not cust:
        conn.close()
        rows = _query_customers(operator_id, '')
        return _customers_page_html(operator, slug, rows, '', current_app.generate_csrf_token(),
                                    {'kind': 'error', 'text': 'الحساب غير موجود.'})

    new_pw = ''.join(_secrets_mod.choice('0123456789') for _ in range(8))
    conn.execute("UPDATE customers SET password_hash=? WHERE id=? AND operator_id=?",
                 (_gen_hash(new_pw), customer_id, operator_id))
    conn.commit()
    # تصفير قفل محاولات الاسترجاع الذاتي لنفس الرقم (لو كان محظوراً بسبب محاولات فاشلة)
    try:
        rate_limit.reset(conn, f"customer_reset_phone:{operator_id}:{cust['phone']}")
    except Exception:
        pass
    phone = cust['phone']
    conn.close()
    write_audit("CUSTOMER_PASSWORD_RESET", f"إعادة ضبط كلمة مرور الزبون {phone}", operator_id=operator_id)

    rows = _query_customers(operator_id, phone)
    return _customers_page_html(operator, slug, rows, phone, current_app.generate_csrf_token(),
                                {'kind': 'success', 'text': f'تمت إعادة ضبط كلمة مرور الزبون {phone}. كلمة المرور الجديدة:',
                                 'password': new_pw, 'phone': phone})