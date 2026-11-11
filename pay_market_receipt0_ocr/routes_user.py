from flask import Blueprint, request, redirect, url_for, session, current_app, g, abort
import sqlite3, re, time, secrets, os, html as html_lib
from urllib.parse import quote as _urlquote
from werkzeug.security import generate_password_hash, check_password_hash
import rate_limit

user_bp = Blueprint('user', __name__)
DATABASE = 'hotspot_cards.db'

# ── حساب الزبون (رقم هاتف + كلمة مرور) ───────────────────────────────────────
# التفاصيل الكاملة للتدفق بأول كتلة "حساب الزبون" أدناه (ترحيب/تسجيل/دخول/كروتي).
CUSTOMER_LOGIN_MAX_ATTEMPTS = 6
CUSTOMER_LOGIN_LOCKOUT      = 120   # ثانية
CUSTOMER_MIN_PASSWORD_LEN   = 6     # للحسابات الجديدة فقط (القديمة بـ4+ تبقى تدخل)

# ── تحديد المشغّل الحالي حسب الـ slug في الرابط ──────────────────────────────
def _get_operator_by_slug(slug):
    conn = sqlite3.connect(DATABASE, timeout=10)
    conn.row_factory = sqlite3.Row
    op = conn.execute('SELECT * FROM operators WHERE slug = ?', (slug,)).fetchone()
    conn.close()
    return op

@user_bp.before_request
def _resolve_operator():
    """يشتغل قبل كل route فيه <slug> في مساره، يتأكد إن المشغّل موجود ومفعّل."""
    slug = (request.view_args or {}).get('slug')
    if slug is None:
        return
    operator = _get_operator_by_slug(slug)
    if not operator or not operator['is_active']:
        abort(404)
    g.operator = operator

_customers_ready = False

def _ensure_customers_table(conn):
    """يضمن وجود جدول حسابات الزبائن ببنية سليمة، مرة واحدة لكل عملية تشغيل.

    السبب: إنشاء الجداول والترقيات بـ app.py (init_db_auto) ما بيشتغل إلا لما
    يُشغَّل الملف مباشرة (python app.py)، وما بيشتغل لما تحمّله استضافة WSGI مثل
    PythonAnywhere — فكان جدول customers ممكن ما ينعمل أبداً، وأي إنشاء حساب أو
    تسجيل دخول بينتهي بصفحة خطأ (OperationalError: no such table: customers).
    لو لقينا جدول قديم ببنية مختلفة (بدون الأعمدة الأساسية) نحتفظ فيه باسم جديد
    كأرشيف بدل حذفه وننشئ جدولاً سليماً."""
    global _customers_ready
    if _customers_ready:
        return
    cols = {r[1] for r in conn.execute("PRAGMA table_info(customers)").fetchall()}
    if cols and not {'operator_id', 'phone', 'password_hash'}.issubset(cols):
        conn.execute(f"ALTER TABLE customers RENAME TO customers_legacy_{int(time.time())}")
        cols = set()
    if not cols:
        conn.execute('''
            CREATE TABLE IF NOT EXISTS customers (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                operator_id    INTEGER NOT NULL,
                phone          TEXT    NOT NULL,
                password_hash  TEXT    NOT NULL,
                created_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(operator_id, phone),
                FOREIGN KEY(operator_id) REFERENCES operators(id)
            )
        ''')
        conn.commit()
    _customers_ready = True


_SPEED_RE = re.compile(r'^\s*(\d+(?:\.\d+)?)\s*(?:m|mb|mbps|mbit|mbits|meg|mega|ميجا)\s*$', re.IGNORECASE)

def _speed_label(profile):
    """يحوّل اسم بروفايل الميكروتيك لسرعة مقروءة للزبون: "2M" ← "سرعة 2 ميجا".
    لو الاسم مو بصيغة سرعة واضحة (مثل "Gold") بنعرضه كما هو بدون تخمين."""
    profile = (profile or '').strip()
    if not profile:
        return ''
    m = _SPEED_RE.match(profile)
    if m:
        n = m.group(1)
        n = n[:-2] if n.endswith('.0') else n
        return f'سرعة {n} ميجا'
    return profile


_hidden_col_ready = False

def _ensure_hidden_column(conn):
    """يضيف عمود transactions.hidden_by_customer مرة واحدة لكل عملية تشغيل.
    "حذف البطاقة" بصفحة «كروتي» إخفاء من قائمة الزبون فقط — سجل العملية والبطاقة
    نفسها لازم يضلوا بقاعدة البيانات لأنهم أساس تقارير مبيعات وإيرادات المشغّل.
    (نفس سبب _ensure_customers_table: init_db_auto ما بتشتغل على استضافة WSGI)."""
    global _hidden_col_ready
    if _hidden_col_ready:
        return
    cols = {r[1] for r in conn.execute("PRAGMA table_info(transactions)").fetchall()}
    if cols and 'hidden_by_customer' not in cols:
        conn.execute("ALTER TABLE transactions ADD COLUMN hidden_by_customer INTEGER DEFAULT 0")
        conn.commit()
    _hidden_col_ready = True


def get_db_connection():
    conn = sqlite3.connect(DATABASE, timeout=10, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    #تفعيل WAL mode لدعم العمليات المتزامنة
    conn.execute("PRAGMA journal_mode=WAL")
    _ensure_customers_table(conn)
    _ensure_hidden_column(conn)
    return conn

def _toast_page(icon, icon_color, icon_bg, title, message, btn_label, btn_href, btn_color):
    return f'''<!DOCTYPE html>
<html lang="ar" dir="rtl">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{title}</title>
    <script src="https://cdn.tailwindcss.com"></script>
    <script>
        tailwind.config = {{ darkMode: 'class' }}
        if (localStorage.getItem('theme') === 'dark' || (!('theme' in localStorage) && window.matchMedia('(prefers-color-scheme: dark)').matches)) {{
            document.documentElement.classList.add('dark');
        }} else {{
            document.documentElement.classList.remove('dark');
        }}
    </script>
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css">
    <style>@import url('https://fonts.googleapis.com/css2?family=Cairo:wght@400;600;700;800&display=swap'); body{{font-family:'Cairo',sans-serif;}}</style>
</head>
<body class="bg-slate-50 dark:bg-[#0B1329] text-slate-800 dark:text-slate-200 min-h-screen flex items-center justify-center p-4 transition-colors duration-200">
    <div class="bg-white dark:bg-[#1C2541] rounded-3xl shadow-xl border border-slate-100 dark:border-[#3A506B]/30 max-w-sm w-full p-8 text-center">
        <div class="w-20 h-20 {icon_bg} rounded-2xl mx-auto mb-5 flex items-center justify-center">
            <i class="fa-solid {icon} {icon_color} text-4xl"></i>
        </div>
        <h2 class="text-xl font-extrabold text-slate-800 dark:text-white mb-2">{title}</h2>
        <p class="text-sm text-slate-500 dark:text-slate-400 leading-relaxed mb-7">{message}</p>
        <a href="{btn_href}" class="block w-full bg-gradient-to-r {btn_color} text-white font-bold py-3.5 px-4 rounded-xl shadow-md transition-all transform hover:-translate-y-0.5 text-sm">
            {btn_label}
        </a>
    </div>
</body>
</html>'''

# ═══════════════════════════════════════════════════════════════════════════
# حساب الزبون: صفحة ترحيب ← إنشاء حساب (مرة واحدة) ← دخول ← صفحة "كروتي"
# ═══════════════════════════════════════════════════════════════════════════
# التدفق:
#   زبون جديد (غير مسجَّل دخول)  → صفحة ترحيب بخيارين (لا شراء بدون حساب):
#       1) إنشاء حساب جديد  (/register: رقم هاتف + كلمة مرور + تأكيدها)
#       2) لدي حساب        (/login: رقم + كلمة مرور + "تذكّرني على هذا الجهاز")
#   بعد التسجيل/الدخول مع "تذكّرني" (الافتراضي) تبقى الجلسة سنة كاملة على نفس
#   الجهاز، فلا يُطلب منه شيء بزياراته القادمة. كل بطاقة يشتريها مرتبطة برقم
#   حسابه وتظهر تلقائياً بصفحة "كروتي" (/account).
#   نسيان كلمة المرور: استعادة ذاتية بإثبات الشراء (اسم مستخدم بطاقة اشتراها
#   فعلاً بنفس الرقم) — لا يوجد SMS بالمنصة للتحقق من الرقم.

_AR_MONTHS = ['يناير', 'فبراير', 'مارس', 'أبريل', 'مايو', 'يونيو',
              'يوليو', 'أغسطس', 'سبتمبر', 'أكتوبر', 'نوفمبر', 'ديسمبر']
try:
    from zoneinfo import ZoneInfo
    _GAZA_TZ = ZoneInfo('Asia/Gaza')
except Exception:
    _GAZA_TZ = None

CUSTOMER_REGISTER_LIMIT   = 8      # عدد التسجيلات المسموح لكل IP
CUSTOMER_REGISTER_WINDOW  = 3600   # خلال ساعة
CUSTOMER_RESET_MAX_FAILS  = 5      # محاولات استعادة فاشلة لكل رقم هاتف
CUSTOMER_RESET_LOCKOUT    = 1800   # ثم قفل نصف ساعة (يمنع تخمين أسماء البطاقات)


def _fmt_dt(ts):
    """'2026-09-07 21:53:33' (UTC من SQLite) → '7 سبتمبر 2026 · 12:53 ص' بتوقيت غزة."""
    try:
        import datetime as _dt
        d = _dt.datetime.strptime(str(ts)[:19], '%Y-%m-%d %H:%M:%S').replace(tzinfo=_dt.timezone.utc)
        d = d.astimezone(_GAZA_TZ) if _GAZA_TZ else d + _dt.timedelta(hours=3)
        h = d.hour % 12 or 12
        return f"{d.day} {_AR_MONTHS[d.month - 1]} {d.year} · {h}:{d.minute:02d} {'م' if d.hour >= 12 else 'ص'}"
    except Exception:
        return str(ts or '')


def _hours_label(h):
    h = int(h or 0)
    if h <= 0:  return ''
    if h == 720: return 'بطاقة شهرية'   # البطاقة الشهرية = 30 يوم
    if h == 1:  return 'ساعة واحدة'
    if h == 2:  return 'ساعتان'
    if 3 <= h <= 10: return f'{h} ساعات'
    return f'{h} ساعة'


def _start_customer_session(phone, remember=True):
    """يفتح جلسة زبون جديدة. remember=True ← جلسة طويلة (سنة) على هذا الجهاز؛
    غير ذلك ← جلسة متصفح تنتهي بإغلاقه."""
    session.clear()
    session['customer_logged_in'] = True
    session['customer_phone']     = phone
    if remember:
        session['_long_lived'] = True
        session.permanent      = True
    else:
        session.permanent = False


# ── مكوّنات واجهة مشتركة بصفحات الحساب ───────────────────────────────────────
_SHOP_CSS = "body{font-family:'Cairo',sans-serif;}"

_SHOP_SCRIPT = '''
tailwind.config = { darkMode: 'class' }
if (localStorage.getItem('theme') === 'dark' || (!('theme' in localStorage) && window.matchMedia('(prefers-color-scheme: dark)').matches)) {
    document.documentElement.classList.add('dark');
} else {
    document.documentElement.classList.remove('dark');
}
function toggleTheme() {
    const html = document.documentElement;
    const icon = document.getElementById('themeIcon');
    if (html.classList.contains('dark')) {
        html.classList.remove('dark'); localStorage.setItem('theme', 'light'); icon.className = "fa-solid fa-moon";
    } else {
        html.classList.add('dark'); localStorage.setItem('theme', 'dark'); icon.className = "fa-solid fa-sun";
    }
}
document.addEventListener("DOMContentLoaded", function() {
    const icon = document.getElementById('themeIcon');
    if (icon) icon.className = document.documentElement.classList.contains('dark') ? "fa-solid fa-sun" : "fa-solid fa-moon";
});
function togglePw(id, btn) {
    const inp = document.getElementById(id);
    const show = inp.type === 'password';
    inp.type = show ? 'text' : 'password';
    btn.querySelector('i').className = show ? 'fa-regular fa-eye-slash' : 'fa-regular fa-eye';
}
function copyText(text, btn, label) {
    const done = () => {
        const toast = document.getElementById('copyToast');
        document.getElementById('copyToastMsg').textContent = 'تم نسخ ' + label;
        toast.classList.remove('hidden');
        setTimeout(() => { toast.classList.add('hidden'); }, 2000);
    };
    if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(text).then(done).catch(() => {});
    } else {
        const ta = document.createElement('textarea');
        ta.value = text; document.body.appendChild(ta); ta.select();
        try { document.execCommand('copy'); done(); } catch (e) {}
        document.body.removeChild(ta);
    }
}
'''

_INPUT_CLS = ("w-full px-4 py-3 bg-slate-50 dark:bg-[#0B1329] border border-slate-200 dark:border-[#3A506B]/30 "
              "rounded-xl text-sm font-semibold text-slate-800 dark:text-white placeholder-slate-300 "
              "dark:placeholder-slate-600 focus:outline-none focus:border-indigo-500 transition-all text-center")
_BTN_PRIMARY = ("w-full bg-gradient-to-r from-indigo-500 via-indigo-600 to-purple-600 hover:from-indigo-600 "
                "hover:to-purple-700 text-white font-bold py-3.5 px-4 rounded-xl shadow-lg shadow-indigo-100 "
                "dark:shadow-none transition-all active:scale-95 flex items-center justify-center gap-2 text-sm")


def _shop_doc(title, body_html):
    """هيكل صفحة كامل (رأس + تبديل الوضع الليلي + تنبيه النسخ + تذييل)."""
    return f'''<!DOCTYPE html>
<html lang="ar" dir="rtl">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{title}</title>
    <script src="https://cdn.tailwindcss.com"></script>
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css">
    <style>@import url('https://fonts.googleapis.com/css2?family=Cairo:wght@400;500;600;700;800&display=swap'); {_SHOP_CSS}</style>
    <script>{_SHOP_SCRIPT}</script>
</head>
<body class="bg-slate-50 dark:bg-[#0B1329] text-slate-800 dark:text-slate-200 min-h-screen flex flex-col justify-between transition-colors duration-200 relative">
    <button onclick="toggleTheme()" type="button" class="fixed top-6 left-6 z-50 w-12 h-12 rounded-xl bg-white dark:bg-[#1C2541] border border-slate-200 dark:border-[#3A506B]/50 shadow-xl text-slate-600 dark:text-indigo-400 flex items-center justify-center transition-all hover:scale-105 active:scale-95 text-md">
        <i id="themeIcon" class="fa-solid fa-moon"></i>
    </button>
    <div id="copyToast" class="hidden fixed top-4 left-1/2 -translate-x-1/2 z-50 bg-slate-800 dark:bg-[#1C2541] text-white text-xs font-bold px-4 py-2 rounded-full shadow-lg border dark:border-[#3A506B]/30 flex items-center gap-2">
        <i class="fa-solid fa-check text-indigo-400"></i><span id="copyToastMsg">تم النسخ</span>
    </div>
    <div class="w-full max-w-lg mx-auto px-4 py-8">
        {body_html}
    </div>
    <footer class="text-center py-4 text-[10px] font-bold text-slate-400 dark:text-slate-500 border-t border-slate-200/40 dark:border-[#3A506B]/20 w-full bg-white dark:bg-[#1C2541]">
        جميع الحقوق محفوظة © شبكة market hotspot 2026
    </footer>
</body>
</html>'''


def _brand_header(operator, subtitle='', show_name=True):
    """شعار + اسم المتجر (نفس منطق الصفحة الرئيسية للمتجر).
    show_name=False يعرض الشعار فقط — لشاشة الترحيب اللي بتكتب الاسم بنفسها
    تحت جملة "مرحباً بك في" (وإلا ظهر اسم الشبكة مرتين)."""
    name_safe = html_lib.escape(operator['hotspot_title'] or operator['name'] or 'شبكة market للانترنت')
    sizes = {'small': ('w-12 h-12', 'text-lg'), 'medium': ('w-16 h-16', 'text-2xl'),
             'large': ('w-24 h-24', 'text-3xl'), 'xlarge': ('w-32 h-32', 'text-4xl')}
    wh, txt = sizes.get(operator['logo_size'] or 'medium', sizes['medium'])
    if operator['logo_filename']:
        fn = html_lib.escape(operator['logo_filename'], quote=True)
        logo = f'<img src="/static/logos/{fn}" alt="شعار الشبكة" class="{wh} rounded-2xl object-cover shadow-lg shadow-indigo-100 dark:shadow-none mb-3 mx-auto">'
    else:
        logo = (f'<div class="inline-flex items-center justify-center {wh} rounded-2xl bg-gradient-to-tr from-indigo-500 '
                f'to-purple-600 text-white {txt} shadow-lg shadow-indigo-100 dark:shadow-none mb-3">'
                f'<i class="fa-solid fa-wifi"></i></div>')
    if not show_name:
        return f'<div class="text-center mb-4">{logo}</div>'
    sub = f'<p class="text-xs font-semibold text-slate-400 dark:text-slate-500 mt-1">{subtitle}</p>' if subtitle else ''
    return f'''<div class="text-center mb-6">{logo}
        <h1 class="text-2xl font-extrabold text-slate-900 dark:text-white tracking-tight">{name_safe}</h1>{sub}</div>'''


def _support_block(operator):
    phone = (operator['support_phone'] or '0590000000').strip()
    safe  = html_lib.escape(phone)
    disp  = html_lib.escape('-'.join([phone[:3], phone[3:6], phone[6:]]) if len(phone) == 10 else phone)
    return f'''
    <div class="bg-white dark:bg-[#1C2541] rounded-3xl p-5 mt-6 border border-slate-100 dark:border-[#3A506B]/30 shadow-xl shadow-slate-100/50 dark:shadow-none">
        <div class="flex items-center gap-2 pb-3 mb-3 border-b border-slate-100 dark:border-[#3A506B]/20 text-slate-800 dark:text-white font-bold text-sm">
            <i class="fa-solid fa-headset text-indigo-500"></i><h2>الدعم الفني</h2>
        </div>
        <a href="tel:{safe}" class="flex items-center gap-3 w-full bg-indigo-50 dark:bg-indigo-950/20 border border-indigo-100 dark:border-indigo-900/30 py-3 px-4 rounded-xl active:scale-95 transition-all">
            <div class="w-9 h-9 rounded-xl bg-indigo-500 text-white flex items-center justify-center shrink-0"><i class="fa-solid fa-phone"></i></div>
            <div class="text-right flex-1">
                <div class="text-xs font-bold text-slate-500 dark:text-slate-400">رقم الجوال</div>
                <div class="text-base font-extrabold text-indigo-600 dark:text-indigo-400 tracking-widest font-mono" dir="ltr">{disp}</div>
            </div>
        </a>
    </div>'''


def _flash(msg, kind='error'):
    if not msg:
        return ''
    c = {'error': ('rose', 'fa-circle-exclamation'), 'success': ('emerald', 'fa-circle-check')}.get(kind, ('rose', 'fa-circle-exclamation'))
    return (f'<div class="bg-{c[0]}-50 dark:bg-{c[0]}-950/20 border border-{c[0]}-100 dark:border-{c[0]}-900/30 '
            f'text-{c[0]}-600 dark:text-{c[0]}-400 text-xs font-bold px-3 py-2.5 rounded-xl mb-4 flex items-center gap-2">'
            f'<i class="fa-solid {c[1]}"></i><span>{html_lib.escape(msg)}</span></div>')


def _phone_field(value='', hint='', field_id='phone'):
    h = f'<p class="text-[11px] text-slate-400 dark:text-slate-500 font-semibold mt-1.5">{hint}</p>' if hint else ''
    return f'''<div>
        <label class="block text-xs font-bold text-slate-600 dark:text-slate-300 mb-1.5"><i class="fa-solid fa-phone text-[11px]"></i> رقم الجوال</label>
        <input id="{field_id}" type="text" name="phone" inputmode="numeric" dir="ltr" autocomplete="username"
               placeholder="059XXXXXXX" pattern="059[0-9]{{7}}" required value="{html_lib.escape(value, quote=True)}"
               class="{_INPUT_CLS}">{h}</div>'''


def _pw_field(name, label, autocomplete, minlen=0, hint=''):
    pid = f'pw_{name}'
    h = f'<p class="text-[11px] text-slate-400 dark:text-slate-500 font-semibold mt-1.5">{hint}</p>' if hint else ''
    ml = f' minlength="{minlen}"' if minlen else ''
    return f'''<div>
        <label class="block text-xs font-bold text-slate-600 dark:text-slate-300 mb-1.5"><i class="fa-solid fa-lock text-[11px]"></i> {label}</label>
        <div class="relative">
            <input id="{pid}" type="password" name="{name}" dir="ltr" required{ml} autocomplete="{autocomplete}" class="{_INPUT_CLS} pl-11">
            <button type="button" onclick="togglePw('{pid}', this)" tabindex="-1" class="absolute inset-y-0 left-0 px-3.5 text-slate-400 hover:text-indigo-500"><i class="fa-regular fa-eye"></i></button>
        </div>{h}</div>'''


def _auth_card(inner):
    return f'''<div class="bg-white dark:bg-[#1C2541] rounded-3xl p-6 border border-slate-100 dark:border-[#3A506B]/30 shadow-xl shadow-slate-100/50 dark:shadow-none">{inner}</div>'''


# ── صفحة الترحيب (زبون غير مسجَّل دخول) ─────────────────────────────────────
def _landing_page(operator, slug):
    name_safe = html_lib.escape(operator['hotspot_title'] or operator['name'] or 'شبكة market للانترنت')
    reg, login = url_for('user.register', slug=slug), url_for('user.login', slug=slug)

    def _row(href, icon, title, sub, primary=False):
        if primary:
            cls = 'bg-gradient-to-r from-indigo-500 via-indigo-600 to-purple-600 text-white shadow-lg shadow-indigo-100 dark:shadow-none'
            icon_cls, sub_cls = 'bg-white/20 text-white', 'text-indigo-100'
        else:
            cls = 'bg-white dark:bg-[#1C2541] border border-slate-200 dark:border-[#3A506B]/40 text-slate-800 dark:text-white'
            icon_cls, sub_cls = 'bg-indigo-50 dark:bg-indigo-950/30 text-indigo-500 dark:text-indigo-400', 'text-slate-400 dark:text-slate-400'
        return f'''<a href="{href}" class="flex items-center gap-3 rounded-2xl p-4 transition-all active:scale-95 {cls}">
            <div class="w-11 h-11 shrink-0 rounded-xl {icon_cls} flex items-center justify-center text-lg"><i class="fa-solid {icon}"></i></div>
            <div class="flex-1 text-right"><div class="text-sm font-extrabold">{title}</div><div class="text-[11px] font-semibold {sub_cls}">{sub}</div></div>
            <i class="fa-solid fa-chevron-left text-xs opacity-60"></i></a>'''

    def _feat(icon, title, text):
        return f'''<div class="flex items-start gap-3">
            <div class="w-10 h-10 shrink-0 rounded-xl bg-indigo-50 dark:bg-indigo-950/30 text-indigo-500 dark:text-indigo-400 flex items-center justify-center"><i class="fa-solid {icon}"></i></div>
            <div class="text-right"><div class="text-sm font-extrabold text-slate-800 dark:text-white">{title}</div>
            <div class="text-xs font-medium text-slate-500 dark:text-slate-400 leading-relaxed">{text}</div></div></div>'''

    body = f'''
        {_brand_header(operator, show_name=False)}
        <p class="text-center text-xs font-semibold text-slate-400 dark:text-slate-500 mb-1">مرحباً بك في</p>
        <h2 class="text-center text-xl font-extrabold text-indigo-600 dark:text-indigo-400 mb-1">{name_safe}</h2>
        <p class="text-center text-sm font-medium text-slate-500 dark:text-slate-400 mb-6">اشترِ كرت الشبكة من جوالك مباشرة</p>
        <div class="space-y-3 mb-8">
            {_row(reg, 'fa-user-plus', 'أنشئ حساب جديد', 'زيارتك الأولى؟ التسجيل يستغرق ثواني', primary=True)}
            {_row(login, 'fa-right-to-bracket', 'لدي حساب', 'ادخل برقمك وكلمة المرور')}
        </div>
        <div class="bg-white dark:bg-[#1C2541] rounded-3xl p-5 border border-slate-100 dark:border-[#3A506B]/30 space-y-4">
            <h3 class="text-center text-xs font-bold text-slate-400 dark:text-slate-500">ماذا يميّز متجرنا</h3>
            {_feat('fa-bolt', 'استلام سريع', 'استلم بطاقتك مباشرة بعد التحقق من الدفع، بدون انتظار ولا وسيط.')}
            {_feat('fa-box-archive', 'كل كروتك بمكان واحد', 'أي بطاقة تشتريها بتنحفظ بحسابك وبترجع تلاقيها بأي وقت.')}
            {_feat('fa-hand-pointer', 'دخول سريع بضغطة', 'يفتح صفحة الشبكة ويسجّل دخولك بالكرت — بلا كتابة يوزر ولا كلمة مرور.')}
        </div>
        {_support_block(operator)}'''
    return _shop_doc('أهلاً بك', body)


# ── إنشاء حساب ──────────────────────────────────────────────────────────────
@user_bp.route('/shop/<slug>/register', methods=['GET', 'POST'])
def register(slug):
    operator    = g.operator
    operator_id = operator['id']
    if session.get('customer_logged_in'):
        return redirect(url_for('user.index', slug=slug))

    if request.method == 'POST':
        current_app.validate_csrf()
        phone    = (request.form.get('phone') or '').strip()
        password = (request.form.get('password') or '')
        confirm  = (request.form.get('confirm_password') or '')

        def _fail(msg):
            session['auth_message'] = msg
            session['auth_phone']   = phone
            return redirect(url_for('user.register', slug=slug))

        conn = get_db_connection()
        # حدّ لعدد الحسابات المُنشأة من نفس الـIP (يمنع حجز أرقام هواتف بالجملة)
        if rate_limit.hit_sliding_window(conn, f"customer_register:{request.remote_addr or 'unknown'}:{operator_id}",
                                          CUSTOMER_REGISTER_LIMIT, CUSTOMER_REGISTER_WINDOW):
            conn.close()
            return _fail('محاولات تسجيل كثيرة جداً. حاول لاحقاً.')
        if not re.fullmatch(r'059\d{7}', phone):
            conn.close()
            return _fail('رقم الجوال غير صالح — يجب أن يبدأ بـ059 ومكوّن من 10 أرقام.')
        if len(password) < CUSTOMER_MIN_PASSWORD_LEN:
            conn.close()
            return _fail(f'كلمة المرور يجب أن تكون {CUSTOMER_MIN_PASSWORD_LEN} أحرف على الأقل.')
        if password != confirm:
            conn.close()
            return _fail('كلمة المرور وتأكيدها غير متطابقتين.')
        exists = conn.execute("SELECT 1 FROM customers WHERE operator_id=? AND phone=?", (operator_id, phone)).fetchone()
        if exists:
            conn.close()
            return _fail('هذا الرقم مسجَّل مسبقاً — سجّل دخولك بدل إنشاء حساب جديد.')
        try:
            conn.execute("INSERT INTO customers (operator_id, phone, password_hash) VALUES (?,?,?)",
                         (operator_id, phone, generate_password_hash(password)))
            conn.commit()
        except sqlite3.IntegrityError:
            conn.close()
            return _fail('هذا الرقم مسجَّل مسبقاً — سجّل دخولك بدل إنشاء حساب جديد.')
        conn.close()
        _start_customer_session(phone, remember=True)
        return redirect(url_for('user.index', slug=slug))

    msg   = session.pop('auth_message', None)
    phone = session.pop('auth_phone', '')
    csrf  = current_app.generate_csrf_token()
    inner = f'''
        <h2 class="text-center text-2xl font-extrabold text-slate-900 dark:text-white mb-1">أنشئ حسابك</h2>
        <p class="text-center text-sm font-medium text-slate-500 dark:text-slate-400 mb-6">مرة واحدة فقط، ثم تدخل به في كل مرة تشتري.</p>
        {_flash(msg)}
        <form method="POST" action="{url_for('user.register', slug=slug)}" class="space-y-4">
            <input type="hidden" name="_csrf_token" value="{csrf}">
            {_phone_field(phone, 'رقمك سيكون اسم المستخدم لدخولك إلى المتجر.')}
            {_pw_field('password', 'كلمة المرور', 'new-password', CUSTOMER_MIN_PASSWORD_LEN,
                       '<strong>ليست</strong> الرقم السري لمحفظة جوال باي أو بال باي.')}
            {_pw_field('confirm_password', 'تأكيد كلمة المرور', 'new-password', CUSTOMER_MIN_PASSWORD_LEN)}
            <p class="text-[11px] font-bold text-amber-500 leading-relaxed">احفظ بيانات دخولك في مكان آمن، ستحتاجها في كل مرة تدخل بها إلى المتجر من جهاز جديد.</p>
            <button type="submit" class="{_BTN_PRIMARY}"><i class="fa-solid fa-arrow-left"></i> إنشاء الحساب</button>
        </form>'''
    body = f'''{_brand_header(operator, 'إنشاء حساب جديد')}{_auth_card(inner)}
        <p class="text-center text-sm font-medium text-slate-500 dark:text-slate-400 mt-5">لديك حساب بالفعل؟
            <a href="{url_for('user.login', slug=slug)}" class="font-extrabold text-indigo-600 dark:text-indigo-400">سجّل الدخول</a></p>'''
    return _shop_doc('إنشاء حساب', body)


# ── تسجيل الدخول ────────────────────────────────────────────────────────────
@user_bp.route('/shop/<slug>/login', methods=['GET', 'POST'])
def login(slug):
    operator    = g.operator
    operator_id = operator['id']
    if session.get('customer_logged_in'):
        return redirect(url_for('user.index', slug=slug))

    if request.method == 'POST':
        current_app.validate_csrf()
        phone    = (request.form.get('phone') or '').strip()
        password = (request.form.get('password') or '')
        remember = request.form.get('remember') == '1'

        def _fail(msg):
            session['auth_message'] = msg
            session['auth_phone']   = phone
            return redirect(url_for('user.login', slug=slug))

        rl_key = f"customer_login:{request.remote_addr or 'unknown'}:{operator_id}"
        conn = get_db_connection()
        locked, remaining = rate_limit.check_locked(conn, rl_key)
        if locked:
            conn.close()
            return _fail(f'محاولات كثيرة جداً. انتظر {remaining} ثانية وحاول مجدداً.')

        cust = conn.execute("SELECT * FROM customers WHERE operator_id=? AND phone=?", (operator_id, phone)).fetchone()
        # رسالة موحَّدة للرقم غير المسجَّل وكلمة المرور الخاطئة (لا نكشف أي الأرقام مسجَّلة)
        if not cust or not check_password_hash(cust['password_hash'], password):
            rate_limit.register_failure(conn, rl_key, CUSTOMER_LOGIN_MAX_ATTEMPTS, CUSTOMER_LOGIN_LOCKOUT)
            conn.close()
            return _fail('رقم الجوال أو كلمة المرور غير صحيحة.')
        rate_limit.reset(conn, rl_key)
        conn.close()
        _start_customer_session(phone, remember=remember)
        return redirect(url_for('user.index', slug=slug))

    msg    = session.pop('auth_message', None)
    notice = session.pop('auth_notice', None)
    phone  = session.pop('auth_phone', '')
    csrf   = current_app.generate_csrf_token()
    inner = f'''
        <h2 class="text-center text-2xl font-extrabold text-slate-900 dark:text-white mb-1">أهلاً بعودتك</h2>
        <p class="text-center text-sm font-medium text-slate-500 dark:text-slate-400 mb-6">سجّل دخولك لمتابعة كروتك في هذا المتجر.</p>
        {_flash(notice, 'success')}{_flash(msg)}
        <form method="POST" action="{url_for('user.login', slug=slug)}" class="space-y-4">
            <input type="hidden" name="_csrf_token" value="{csrf}">
            {_phone_field(phone)}
            {_pw_field('password', 'كلمة المرور', 'current-password')}
            <a href="{url_for('user.forgot_password', slug=slug)}" class="block text-xs font-bold text-slate-400 dark:text-slate-500 hover:text-indigo-500">نسيت كلمة المرور؟</a>
            <label class="flex items-center gap-2.5 text-sm font-semibold text-slate-600 dark:text-slate-300 cursor-pointer">
                <input type="checkbox" name="remember" value="1" checked class="w-5 h-5 rounded accent-indigo-500">
                تذكّرني على هذا الجهاز
            </label>
            <button type="submit" class="{_BTN_PRIMARY}"><i class="fa-solid fa-arrow-left"></i> دخول</button>
        </form>'''
    body = f'''{_brand_header(operator, 'حسابك في الشبكة')}{_auth_card(inner)}
        <p class="text-center text-sm font-medium text-slate-500 dark:text-slate-400 mt-5">أول مرة معنا؟
            <a href="{url_for('user.register', slug=slug)}" class="font-extrabold text-indigo-600 dark:text-indigo-400">أنشئ حسابك</a></p>'''
    return _shop_doc('تسجيل الدخول', body)


def _wa_number(phone):
    """رقم الدعم → صيغة واتساب الدولية (970 + الرقم بدون الصفر). None لو مو رقم جوال صالح."""
    d = re.sub(r'\D', '', phone or '')
    if d.startswith('00'):
        d = d[2:]
    if d.startswith('0'):
        d = '970' + d[1:]
    return d if re.fullmatch(r'9705\d{8}', d) else None


# ── نسيت كلمة المرور: واتساب للشبكة (الأساسي) + استعادة ذاتية بإثبات الشراء ───
@user_bp.route('/shop/<slug>/forgot', methods=['GET', 'POST'])
def forgot_password(slug):
    operator    = g.operator
    operator_id = operator['id']

    if request.method == 'POST':
        current_app.validate_csrf()
        phone    = (request.form.get('phone') or '').strip()
        card_usr = (request.form.get('card_username') or '').strip()
        password = (request.form.get('password') or '')
        confirm  = (request.form.get('confirm_password') or '')

        def _fail(msg):
            session['auth_message'] = msg
            session['auth_phone']   = phone
            return redirect(url_for('user.forgot_password', slug=slug))

        if not re.fullmatch(r'059\d{7}', phone):
            return _fail('رقم الجوال غير صالح.')
        if len(password) < CUSTOMER_MIN_PASSWORD_LEN:
            return _fail(f'كلمة المرور الجديدة يجب أن تكون {CUSTOMER_MIN_PASSWORD_LEN} أحرف على الأقل.')
        if password != confirm:
            return _fail('كلمة المرور وتأكيدها غير متطابقتين.')

        ip_key    = f"customer_reset_ip:{request.remote_addr or 'unknown'}:{operator_id}"
        phone_key = f"customer_reset_phone:{operator_id}:{phone}"
        conn = get_db_connection()
        for k in (ip_key, phone_key):
            locked, remaining = rate_limit.check_locked(conn, k)
            if locked:
                conn.close()
                mins = max(1, remaining // 60)
                return _fail(f'محاولات كثيرة. حاول بعد {mins} دقيقة أو تواصل مع الدعم.')

        cust = conn.execute("SELECT 1 FROM customers WHERE operator_id=? AND phone=?", (operator_id, phone)).fetchone()
        # إثبات الشراء: اسم مستخدم بطاقة اشتراها هذا الرقم فعلاً من هذا المتجر
        proof = conn.execute('''
            SELECT 1 FROM transactions t JOIN vouchers v ON t.voucher_id = v.id
            WHERE t.customer_phone = ? AND t.operator_id = ? AND v.username = ? LIMIT 1
        ''', (phone, operator_id, card_usr)).fetchone() if (cust and card_usr) else None
        if not proof:
            rate_limit.register_failure(conn, ip_key, 10, CUSTOMER_RESET_LOCKOUT)
            rate_limit.register_failure(conn, phone_key, CUSTOMER_RESET_MAX_FAILS, CUSTOMER_RESET_LOCKOUT)
            conn.close()
            return _fail('البيانات غير صحيحة — تأكد من الرقم واسم مستخدم بطاقة اشتريتها فعلاً.')

        conn.execute("UPDATE customers SET password_hash=? WHERE operator_id=? AND phone=?",
                     (generate_password_hash(password), operator_id, phone))
        conn.commit()
        rate_limit.reset(conn, phone_key)
        rate_limit.reset(conn, ip_key)
        conn.close()
        session['auth_notice'] = 'تم تغيير كلمة المرور. سجّل دخولك بالكلمة الجديدة.'
        session['auth_phone']  = phone
        return redirect(url_for('user.login', slug=slug))

    msg   = session.pop('auth_message', None)
    phone = session.pop('auth_phone', '')
    csrf  = current_app.generate_csrf_token()
    support_raw = (operator['support_phone'] or '').strip()
    support = html_lib.escape(support_raw)
    net_name = (operator['hotspot_title'] or operator['name'] or '').strip()
    wa = _wa_number(support_raw)
    wa_text = _urlquote(f"مرحباً، نسيت كلمة المرور لحسابي في {net_name}. رجاءً أعد ضبطها لي.\nرقم حسابي: ")

    contact_html = ''
    if wa:
        contact_html += f'''
        <a href="https://wa.me/{wa}?text={wa_text}" target="_blank" rel="noopener"
           class="w-full bg-emerald-500 hover:bg-emerald-600 text-white font-bold py-3.5 px-4 rounded-xl shadow-lg shadow-emerald-100 dark:shadow-none transition-all active:scale-95 flex items-center justify-center gap-2 text-sm">
            <i class="fa-brands fa-whatsapp text-lg"></i> تواصل مع الشبكة عبر واتساب</a>'''
    if support_raw:
        contact_html += f'''
        <div class="text-center text-xs font-semibold text-slate-500 dark:text-slate-400 mt-3">
            أو اتصل بالدعم الفني: <a href="tel:{support}" dir="ltr" class="font-mono font-extrabold text-indigo-600 dark:text-indigo-400">{support}</a></div>'''
    else:
        contact_html = '<p class="text-center text-sm font-semibold text-slate-500 dark:text-slate-400">تواصل مع الشبكة لإعادة ضبط كلمة مرورك.</p>'

    inner = f'''
        <h2 class="text-center text-2xl font-extrabold text-slate-900 dark:text-white mb-1">استعادة كلمة المرور</h2>
        <p class="text-center text-sm font-medium text-slate-500 dark:text-slate-400 mb-6 leading-relaxed">
            تواصل مع الشبكة وسيعيدون ضبط كلمة مرور حسابك ويرسلون لك كلمة جديدة.</p>
        {_flash(msg)}
        {contact_html}
        <details class="mt-6 border-t border-slate-200 dark:border-[#3A506B]/30 pt-4" {'open' if msg else ''}>
            <summary class="cursor-pointer text-center text-xs font-bold text-slate-500 dark:text-slate-400 select-none">
                أو استرجعها بنفسك باسم مستخدم كرت اشتريته</summary>
            <form method="POST" action="{url_for('user.forgot_password', slug=slug)}" class="space-y-4 mt-4">
                <input type="hidden" name="_csrf_token" value="{csrf}">
                {_phone_field(phone)}
                <div>
                    <label class="block text-xs font-bold text-slate-600 dark:text-slate-300 mb-1.5"><i class="fa-solid fa-ticket text-[11px]"></i> اسم مستخدم كرت اشتريته</label>
                    <input type="text" name="card_username" dir="ltr" required autocomplete="off" placeholder="مثال: 615156" class="{_INPUT_CLS}">
                </div>
                {_pw_field('password', 'كلمة المرور الجديدة', 'new-password', CUSTOMER_MIN_PASSWORD_LEN)}
                {_pw_field('confirm_password', 'تأكيد كلمة المرور الجديدة', 'new-password', CUSTOMER_MIN_PASSWORD_LEN)}
                <button type="submit" class="{_BTN_PRIMARY}"><i class="fa-solid fa-key"></i> تغيير كلمة المرور</button>
            </form>
        </details>'''
    body = f'''{_brand_header(operator)}{_auth_card(inner)}
        <p class="text-center text-sm font-medium mt-5"><a href="{url_for('user.login', slug=slug)}" class="font-extrabold text-indigo-600 dark:text-indigo-400">العودة لتسجيل الدخول</a></p>'''
    return _shop_doc('استعادة كلمة المرور', body)


# ── صفحة "كروتي" ────────────────────────────────────────────────────────────
@user_bp.route('/shop/<slug>/account', methods=['GET'])
def account(slug):
    operator    = g.operator
    operator_id = operator['id']
    if not (session.get('customer_logged_in') and session.get('customer_phone')):
        return redirect(url_for('user.login', slug=slug))
    phone = session['customer_phone']

    conn = get_db_connection()
    all_rows = conn.execute('''
        SELECT t.id AS tid, t.transaction_id, t.created_at, v.username, v.password, v.price, v.profile,
               v.duration_hours, COALESCE(t.hidden_by_customer, 0) AS hidden
        FROM transactions t JOIN vouchers v ON t.voucher_id = v.id
        WHERE t.customer_phone = ? AND t.operator_id = ?
        ORDER BY t.id DESC
    ''', (phone, operator_id)).fetchall()
    conn.close()
    # المحذوفة (المخفية) ما بتنعرض ولا بتنحسب بعدّاد الكروت، لكن "إجمالي المدفوع"
    # بيشمل كل مشترياته لأنه مبلغ دفعه فعلاً حتى لو حذف البطاقة من قائمته
    rows = [r for r in all_rows if not r['hidden']]

    hotspot_login_url = html_lib.escape((operator['hotspot_login_url'] or 'http://00.00.00.00/login').strip(), quote=True)
    csrf = current_app.generate_csrf_token()
    total_spent = sum((r['price'] or 0) for r in all_rows)
    spent_label = int(total_spent) if total_spent == int(total_spent) else round(total_spent, 2)

    def _card(r):
        title = html_lib.escape(_hours_label(r['duration_hours']) or ('بروفايل ' + (r['profile'] or '')))
        prof  = html_lib.escape(r['profile'] or '')
        price = int(r['price']) if r['price'] == int(r['price']) else r['price']
        usr, pwd = html_lib.escape(r['username'] or '', quote=True), html_lib.escape(r['password'] or '', quote=True)
        ref = html_lib.escape(str(r['transaction_id'] or ''))
        del_url = url_for('user.delete_card', slug=slug, tid=r['tid'])
        del_label = html_lib.escape(f"{_hours_label(r['duration_hours']) or ('بروفايل ' + (r['profile'] or ''))} — {price} شيكل", quote=True)
        return f'''
        <div class="bg-white dark:bg-[#1C2541] rounded-3xl p-5 border border-slate-100 dark:border-[#3A506B]/30 shadow-xl shadow-slate-100/50 dark:shadow-none">
            <div class="flex items-start justify-between gap-2 pb-3 mb-3 border-b border-slate-100 dark:border-[#3A506B]/20">
                <div class="text-right">
                    <div class="text-base font-extrabold text-slate-800 dark:text-white">{title}{(' · ' + prof) if prof else ''}</div>
                    <div class="text-[11px] font-semibold text-slate-400 dark:text-slate-500 mt-0.5"><i class="fa-regular fa-clock"></i> {_fmt_dt(r['created_at'])}</div>
                </div>
                <div class="text-left shrink-0">
                    <div class="text-lg font-extrabold text-indigo-600 dark:text-indigo-400">{price} <span class="text-xs">شيكل</span></div>
                    <div class="text-[10px] font-mono text-slate-400 dark:text-slate-500" dir="ltr">#{ref}</div>
                </div>
            </div>
            <div class="grid grid-cols-2 gap-2 mb-3">
                <div class="bg-slate-50 dark:bg-[#0B1329] rounded-xl p-3 text-right">
                    <div class="text-[10px] font-bold text-slate-400 dark:text-slate-500 mb-1">اسم المستخدم</div>
                    <div class="text-lg font-mono font-extrabold text-indigo-600 dark:text-indigo-400 tracking-wider select-all" dir="ltr">{usr}</div>
                </div>
                <div class="bg-slate-50 dark:bg-[#0B1329] rounded-xl p-3 text-right">
                    <div class="text-[10px] font-bold text-slate-400 dark:text-slate-500 mb-1">كلمة المرور</div>
                    <div class="text-lg font-mono font-extrabold text-indigo-600 dark:text-indigo-400 tracking-wider select-all" dir="ltr">{pwd}</div>
                </div>
            </div>
            <div class="flex gap-2">
                <form method="POST" action="{hotspot_login_url}" target="_blank" class="flex-1">
                    <input type="hidden" name="username" value="{usr}">
                    <input type="hidden" name="password" value="{pwd}">
                    <button type="submit" class="w-full bg-gradient-to-r from-indigo-500 to-purple-600 text-white font-bold py-2.5 rounded-xl text-xs flex items-center justify-center gap-1.5 active:scale-95 transition-all">
                        <i class="fa-solid fa-bolt"></i> دخول سريع للشبكة</button>
                </form>
                <button type="button" onclick="copyText('{usr}', this, 'اسم المستخدم')" class="px-3.5 rounded-xl bg-slate-100 dark:bg-[#0B1329] text-slate-600 dark:text-slate-300 text-xs font-bold" title="نسخ اسم المستخدم"><i class="fa-regular fa-copy"></i> يوزر</button>
                <button type="button" onclick="copyText('{pwd}', this, 'كلمة المرور')" class="px-3.5 rounded-xl bg-slate-100 dark:bg-[#0B1329] text-slate-600 dark:text-slate-300 text-xs font-bold" title="نسخ كلمة المرور"><i class="fa-regular fa-copy"></i> باسورد</button>
                <button type="button" onclick="askDeleteCard(this)" data-action="{del_url}" data-label="{del_label}" class="px-3.5 rounded-xl bg-rose-50 dark:bg-rose-950/30 text-rose-500 dark:text-rose-400 text-xs font-bold active:scale-95 transition-all" title="حذف البطاقة"><i class="fa-regular fa-trash-can"></i></button>
            </div>
        </div>'''

    if rows:
        cards_html = '<div class="space-y-4">' + ''.join(_card(r) for r in rows) + '</div>'
    else:
        cards_html = '''<div class="bg-white dark:bg-[#1C2541] border border-slate-100 dark:border-[#3A506B]/30 text-slate-400 dark:text-slate-500 text-center py-10 rounded-3xl text-sm font-bold">
            <i class="fa-solid fa-inbox text-2xl mb-2 block"></i>ما في بطاقات بعد — أول عملية شراء رح تظهر هون تلقائياً.</div>'''

    notice = session.pop('account_notice', None)
    notice_html = ''
    if notice:
        kind, text = notice
        cls = ('bg-emerald-50 dark:bg-emerald-950/20 border-emerald-100 dark:border-emerald-900/30 text-emerald-600 dark:text-emerald-400'
               if kind == 'success' else
               'bg-rose-50 dark:bg-rose-950/20 border-rose-100 dark:border-rose-900/30 text-rose-600 dark:text-rose-400')
        notice_html = f'<div class="border {cls} text-xs font-bold px-4 py-3 rounded-2xl mb-4 text-right">{html_lib.escape(text)}</div>'

    delete_modal = f'''
        <div id="delCardModal" class="hidden fixed inset-0 bg-slate-900/60 backdrop-blur-sm flex items-center justify-center z-50 p-4">
            <div class="bg-white dark:bg-[#1C2541] rounded-3xl p-6 max-w-sm w-full text-center shadow-2xl border border-slate-100 dark:border-[#3A506B]/30">
                <div class="w-14 h-14 rounded-2xl bg-rose-50 dark:bg-rose-950/30 text-rose-500 mx-auto mb-3 flex items-center justify-center text-xl"><i class="fa-regular fa-trash-can"></i></div>
                <h3 class="text-base font-extrabold text-slate-800 dark:text-white mb-1">حذف هذه البطاقة؟</h3>
                <p id="delCardLabel" class="text-sm font-bold text-indigo-600 dark:text-indigo-400 mb-2"></p>
                <p class="text-[11px] font-semibold text-slate-400 dark:text-slate-500 leading-relaxed mb-5">رح تختفي من قائمة «كروتي». احذفها بس إذا انتهت أو ما عاد بدك إياها.</p>
                <form id="delCardForm" method="POST" action="">
                    <input type="hidden" name="_csrf_token" value="{csrf}">
                    <div class="flex gap-2">
                        <button type="button" onclick="closeDeleteCard()" class="flex-1 bg-slate-100 dark:bg-[#0B1329] text-slate-600 dark:text-slate-300 font-bold py-3 rounded-xl text-sm">إلغاء</button>
                        <button type="submit" class="flex-1 bg-rose-500 hover:bg-rose-600 text-white font-bold py-3 rounded-xl text-sm">نعم، احذفها</button>
                    </div>
                </form>
            </div>
        </div>
        <script>
        function askDeleteCard(btn) {{
            document.getElementById('delCardForm').action = btn.dataset.action;
            document.getElementById('delCardLabel').textContent = btn.dataset.label;
            document.getElementById('delCardModal').classList.remove('hidden');
        }}
        function closeDeleteCard() {{ document.getElementById('delCardModal').classList.add('hidden'); }}
        document.addEventListener('keydown', function(e) {{ if (e.key === 'Escape') closeDeleteCard(); }});
        document.getElementById('delCardModal').addEventListener('click', function(e) {{ if (e.target === this) closeDeleteCard(); }});
        </script>'''

    body = f'''
        <div class="flex items-center justify-between gap-3 mb-5 pt-10">
            <form method="POST" action="{url_for('user.wallet_logout', slug=slug)}">
                <input type="hidden" name="_csrf_token" value="{csrf}">
                <button type="submit" class="flex items-center gap-1.5 text-xs font-bold text-slate-500 dark:text-slate-400 border border-slate-200 dark:border-[#3A506B]/40 rounded-full px-4 py-2 hover:text-rose-500">
                    <i class="fa-solid fa-right-from-bracket"></i> خروج</button>
            </form>
            <div class="text-right min-w-0">
                <div class="text-sm font-extrabold text-indigo-600 dark:text-indigo-400 truncate">{html_lib.escape(operator['hotspot_title'] or operator['name'] or '')}</div>
                <div class="text-xs font-semibold text-slate-500 dark:text-slate-400"><i class="fa-regular fa-user"></i> حسابك: <span class="font-mono" dir="ltr">{html_lib.escape(phone)}</span></div>
            </div>
        </div>
        <div class="bg-white dark:bg-[#1C2541] rounded-3xl p-5 mb-4 border border-slate-100 dark:border-[#3A506B]/30 shadow-xl shadow-slate-100/50 dark:shadow-none">
            <div class="flex items-end justify-between">
                <div class="text-right">
                    <div class="text-xs font-bold text-slate-400 dark:text-slate-500">إجمالي الكروت</div>
                    <div class="text-4xl font-extrabold text-indigo-600 dark:text-indigo-400">{len(rows)}</div>
                </div>
                <div class="text-left">
                    <div class="text-xs font-bold text-slate-400 dark:text-slate-500">إجمالي المدفوع</div>
                    <div class="text-xl font-extrabold text-slate-700 dark:text-slate-200">{spent_label} <span class="text-xs">شيكل</span></div>
                </div>
            </div>
        </div>
        <a href="{url_for('user.index', slug=slug)}" class="{_BTN_PRIMARY} mb-6"><i class="fa-solid fa-plus"></i> شراء المزيد</a>
        <h2 class="flex items-center gap-2 text-base font-extrabold text-slate-800 dark:text-white mb-3"><i class="fa-solid fa-ticket text-indigo-500"></i> كروتي</h2>
        {notice_html}
        {cards_html}
        {_support_block(operator)}
        {delete_modal}'''
    return _shop_doc('كروتي', body)


@user_bp.route('/shop/<slug>/account/card/<int:tid>/delete', methods=['POST'])
def delete_card(slug, tid):
    """حذف بطاقة من قائمة «كروتي» للزبون = إخفاؤها فقط (لا يُحذف أي سجل بيع)."""
    operator_id = g.operator['id']
    if not (session.get('customer_logged_in') and session.get('customer_phone')):
        return redirect(url_for('user.login', slug=slug))
    current_app.validate_csrf()
    conn = get_db_connection()
    # الشرط الثلاثي (المعرّف + رقم حسابه + المشغّل) يمنع إخفاء بطاقة زبون آخر
    cur = conn.execute(
        "UPDATE transactions SET hidden_by_customer = 1 "
        "WHERE id = ? AND customer_phone = ? AND operator_id = ? AND COALESCE(hidden_by_customer, 0) = 0",
        (tid, session['customer_phone'], operator_id))
    conn.commit()
    done = cur.rowcount > 0
    conn.close()
    session['account_notice'] = ('success', 'تم حذف البطاقة من قائمتك.') if done else ('error', 'ما لقينا هالبطاقة بحسابك.')
    return redirect(url_for('user.account', slug=slug))


@user_bp.route('/shop/<slug>/wallet-logout', methods=['POST'])
def wallet_logout(slug):
    current_app.validate_csrf()
    session.clear()
    return redirect(url_for('user.index', slug=slug))


# الصفحة الرئيسية (المتجر)
@user_bp.route('/shop/<slug>/', methods=['GET'])
def index(slug):
    operator    = g.operator
    operator_id = operator['id']
    customer_logged_in = bool(session.get('customer_logged_in') and session.get('customer_phone'))

    # زبون غير مسجَّل دخول ← شاشة الترحيب (إنشاء حساب / لدي حساب). المسجَّل دخول
    # (جلسة سنة كاملة على جهازه) يدخل المتجر مباشرة، فلا يُطلب منه شيء بزياراته القادمة.
    if not customer_logged_in:
        return _landing_page(operator, slug)

    conn = get_db_connection()
    customer_phone = session.get('customer_phone', '') if customer_logged_in else ''

    # تنظيف القيم قبل إدراجها داخل الـ HTML (حماية من XSS)
    customer_phone_safe = html_lib.escape(customer_phone, quote=True)

    recent_codes = session.pop('recent_bought_codes', None)
    # توليد CSRF token لنماذج الصفحة
    csrf_token = current_app.generate_csrf_token()

    # عنوان صفحة الهوتسبوت مستقل عن اسم المشغّل الإداري: لو ما انحدد نرجع للاسم
    operator_name_safe  = html_lib.escape(operator['hotspot_title'] or operator['name'] or 'شبكة market للانترنت')
    # عنوان (رابط) جهاز الهوتسبوت الخاص بهذا المشغّل تحديداً — يُستخدم بزر
    # "دخول فوري سريع للشبكة"، لأن كل متجر مربوط بجهاز مختلف فعلياً
    hotspot_login_url  = html_lib.escape((operator['hotspot_login_url'] or 'http://00.00.00.00/login').strip(), quote=True)
    support_phone       = (operator['support_phone'] or '0590000000').strip()
    support_phone_safe  = html_lib.escape(support_phone)
    support_phone_disp  = html_lib.escape('-'.join([support_phone[:3], support_phone[3:6], support_phone[6:]])
                                            if len(support_phone) == 10 else support_phone)
    LOGO_SIZE_CLASSES = {
        'small':  'w-12 h-12 text-lg',
        'medium': 'w-16 h-16 text-2xl',
        'large':  'w-24 h-24 text-3xl',
        'xlarge': 'w-32 h-32 text-4xl',
    }
    logo_size_classes = LOGO_SIZE_CLASSES.get(operator['logo_size'] or 'medium', LOGO_SIZE_CLASSES['medium'])
    logo_size_wh, logo_size_text = logo_size_classes.rsplit(' ', 1)[0], logo_size_classes.rsplit(' ', 1)[1]
    if operator['logo_filename']:
        logo_filename_safe = html_lib.escape(operator['logo_filename'], quote=True)
        logo_html = f'<img src="/static/logos/{logo_filename_safe}" alt="شعار الشبكة" class="{logo_size_wh} rounded-2xl object-cover shadow-lg shadow-indigo-100 dark:shadow-none mb-3 mx-auto">'
    else:
        logo_html = f'''<div class="cursor-pointer inline-flex items-center justify-center {logo_size_wh} rounded-2xl bg-gradient-to-tr from-indigo-500 to-purple-600 text-white {logo_size_text} shadow-lg shadow-indigo-100 dark:shadow-none mb-3">
                    <i class="fa-solid fa-wifi"></i>
                </div>'''
    buy_action     = url_for('payment.checkout', slug=slug)

    # جلب الباقات المتاحة قبل إغلاق الاتصال (خاصة بهذا المشغّل فقط)
    available_prices = conn.execute(
        'SELECT DISTINCT price, profile, duration_hours FROM vouchers WHERE status = "available" AND operator_id = ? ORDER BY price ASC',
        (operator_id,)
    ).fetchall()
    conn.close()

    def _hours_label(h):
        h = int(h or 0)
        if h <= 0:  return ''
        if h == 720: return 'بطاقة شهرية'   # البطاقة الشهرية = 30 يوم
        if h == 1:  return 'ساعة واحدة'
        if h == 2:  return 'ساعتان'
        if 3 <= h <= 10: return f'{h} ساعات'
        return f'{h} ساعة'

    if available_prices:
        def _price_row(idx, row):
            price = round(row['price'], 2)
            label = int(row['price']) if row['price'] == int(row['price']) else row['price']
            profile_safe = html_lib.escape(row['profile'] or '')
            hours_label  = _hours_label(row['duration_hours'])
            # السطر الأول: عدد الساعات والسعر جنب بعض بخط واضح. السطر الثاني: السرعة.
            title_safe   = html_lib.escape(hours_label) if hours_label else 'بطاقة'
            speed_safe   = html_lib.escape(_speed_label(row['profile']))
            speed_html   = (f'''<div class="mt-1 flex items-center gap-1.5 text-xs font-bold text-slate-500 dark:text-slate-400">
                            <i class="fa-solid fa-gauge-high text-[11px] text-indigo-400"></i><span class="truncate">{speed_safe}</span></div>'''
                            if speed_safe else '')
            return f'''
                <div class="qty-row flex items-center gap-2.5 bg-slate-50 dark:bg-[#0B1329] border border-slate-200 dark:border-[#3A506B]/30 rounded-2xl p-2.5" data-price="{price}">
                    <div class="flex-1 text-right min-w-0 px-1">
                        <div class="flex items-center flex-wrap gap-x-2 gap-y-1">
                            <span class="text-base font-black text-slate-800 dark:text-white whitespace-nowrap">{title_safe}</span>
                            <span class="inline-flex items-baseline gap-1 px-2 py-0.5 rounded-lg bg-indigo-50 dark:bg-indigo-950/40 text-indigo-600 dark:text-indigo-300 font-black text-base whitespace-nowrap">{label}<span class="text-[10px] font-bold">شيكل</span></span>
                        </div>
                        {speed_html}
                    </div>
                    <button type="button" onclick="changeQty('{idx}', -1)" title="إنقاص"
                        class="w-8 h-8 shrink-0 rounded-full border-2 border-slate-300 dark:border-[#3A506B]/60 text-slate-400 dark:text-slate-500 flex items-center justify-center text-xs transition-all active:scale-90">
                        <i class="fa-solid fa-minus"></i>
                    </button>
                    <input type="text" inputmode="numeric" pattern="[0-9]*" id="qtyDisplay_{idx}" value="0"
                        oninput="onQtyTyped('{idx}', this.value)" onfocus="this.select()"
                        class="w-9 h-9 shrink-0 rounded-full bg-slate-800 dark:bg-[#3A506B]/50 text-white text-sm font-extrabold text-center border-none outline-none focus:ring-2 focus:ring-indigo-400">
                    <button type="button" onclick="changeQty('{idx}', 1)" title="إضافة"
                        class="w-10 h-10 shrink-0 rounded-full bg-indigo-500 hover:bg-indigo-600 text-white flex items-center justify-center text-sm transition-all active:scale-90">
                        <i class="fa-solid fa-plus"></i>
                    </button>
                    <input type="hidden" name="price[]" id="priceInput_{idx}" value="{price}" disabled>
                    <input type="hidden" name="profile[]" id="profileInput_{idx}" value="{profile_safe}" disabled>
                    <input type="hidden" name="hours[]" id="hoursInput_{idx}" value="{row['duration_hours'] or 0}" disabled>
                    <input type="hidden" name="quantity[]" id="qtyInput_{idx}" value="0" disabled>
                </div>'''
        options_html = '\n'.join(_price_row(idx, row) for idx, row in enumerate(available_prices))
    else:
        options_html = '<p class="text-xs font-bold text-slate-400 dark:text-slate-500 text-center py-4">لا توجد باقات متاحة حالياً</p>'

    # ── HTML بطاقات الشراء الناجح ──
    recent_html = ""
    if recent_codes:
        purchase_phone = html_lib.escape(recent_codes[0].get('customer_phone', ''))
        cards_boxes = "".join([f'''
            <div class="bg-white dark:bg-[#1C2541] border-2 border-indigo-500 dark:border-indigo-400 rounded-xl p-4 flex flex-col items-center justify-between shadow-sm relative overflow-hidden">
                <div class="absolute top-0 right-0 bg-indigo-500 dark:bg-indigo-600 text-white text-[10px] px-2 py-0.5 rounded-bl font-bold">{(_hours_label(c.get("duration_hours")) or ("بروفايل " + (c.get("profile") or ""))) + " — " if (c.get("duration_hours") or c.get("profile")) else ""}{c['price']} شيكل</div>
                <div class="w-full space-y-2 my-2 text-right">
                    <div>
                        <span class="text-[11px] font-bold text-slate-400 dark:text-slate-400 block mb-0.5">👤 اسم المستخدم (User):</span>
                        <div class="flex gap-1 items-center bg-slate-50 dark:bg-[#0B1329] p-2 rounded-lg border border-slate-100 dark:border-[#3A506B]/30">
                            <span class="text-md font-mono font-bold text-slate-800 dark:text-white flex-1 select-all text-center tracking-wider">{c['username']}</span>
                            <button class="text-slate-400 hover:text-indigo-500 p-1 text-xs" onclick="copyToClipboard('{c['username']}', this)"><i class="fa-regular fa-copy"></i></button>
                        </div>
                    </div>
                    <div>
                        <span class="text-[11px] font-bold text-slate-400 dark:text-slate-400 block mb-0.5">🔑 كلمة المرور (Pass):</span>
                        <div class="flex gap-1 items-center bg-slate-50 dark:bg-[#0B1329] p-2 rounded-lg border border-slate-100 dark:border-[#3A506B]/30">
                            <span class="text-md font-mono font-bold text-slate-800 dark:text-white flex-1 select-all text-center tracking-wider">{c['password']}</span>
                            <button class="text-slate-400 hover:text-indigo-500 p-1 text-xs" onclick="copyToClipboard('{c['password']}', this)"><i class="fa-regular fa-copy"></i></button>
                        </div>
                    </div>
                </div>
                <div class="text-[10px] text-indigo-600 dark:text-indigo-300 font-bold mt-1 text-center bg-indigo-50 dark:bg-indigo-950/30 px-2 py-1 rounded w-full mb-2"><i class="fa-solid fa-circle-info"></i> تم حفظ البطاقة في محفظتك تلقائياً</div>
                <form method="POST" action="{hotspot_login_url}" target="_blank">
                    <input type="hidden" name="username" value="{c['username']}">
                    <input type="hidden" name="password" value="{c['password']}">
                    <button type="submit" class="w-full bg-gradient-to-r from-indigo-500 to-purple-600 hover:from-indigo-600 hover:to-purple-700 text-white font-bold py-2 px-4 rounded-xl shadow-md text-xs text-center transition-all flex items-center justify-center gap-1 transform active:scale-95">
                        <i class="fa-solid fa-bolt"></i> <span>⚡ دخول فوري سريع للشبكة</span>
                    </button>
                </form>
            </div>
        ''' for c in recent_codes])
        recent_html = f'''
        <div class="bg-gradient-to-br from-indigo-50/60 to-purple-50/60 dark:from-[#1C2541] dark:to-[#1f2447] border border-indigo-100 dark:border-indigo-900/50 rounded-2xl p-6 mb-6 shadow-sm">
            <div class="flex items-center gap-2 mb-4 text-indigo-900 dark:text-indigo-300">
                <i class="fa-solid fa-circle-check text-2xl text-indigo-500"></i>
                <h3 class="text-lg font-bold m-0">تم الدفع بنجاح! استلم بطاقاتك الآن:</h3>
            </div>
            <div class="bg-white dark:bg-[#0B1329] border-2 border-dashed border-indigo-300 dark:border-indigo-800 rounded-xl p-4 mb-4 text-center">
                <p class="text-xs font-bold text-slate-500 dark:text-slate-400 mb-1">
                    <i class="fa-solid fa-box-archive text-indigo-500"></i> بطاقاتك محفوظة على رقم
                    <span class="font-mono text-indigo-600 dark:text-indigo-400" dir="ltr">{purchase_phone}</span>
                </p>
                <p class="text-[10px] text-slate-400 dark:text-slate-500 font-bold mt-2">
                    <i class="fa-solid fa-circle-info"></i> سجّل دخول بنفس هذا الرقم بمحفظتك بالأسفل (مرة وحدة بس) لترجع تشوف كل بطاقاتك وقتما بدك
                </p>
            </div>
            <div class="grid grid-cols-1 sm:grid-cols-2 gap-3">{cards_boxes}</div>
        </div>'''

    # ── شريط الحساب بأعلى المتجر: زر "كروتي" + رقم الحساب (المتجر للمسجَّلين فقط) ──
    account_url = url_for('user.account', slug=slug)
    account_bar_html = f'''
        <div class="flex items-center gap-2 mb-5 pt-1">
            <a href="{account_url}" class="flex items-center gap-2 bg-white dark:bg-[#1C2541] border border-slate-200 dark:border-[#3A506B]/40 text-indigo-600 dark:text-indigo-400 font-bold text-xs rounded-full px-4 py-2.5 shadow-sm active:scale-95 transition-all">
                <i class="fa-solid fa-ticket"></i> كروتي</a>
            <span class="text-[11px] font-semibold text-slate-500 dark:text-slate-400"><i class="fa-regular fa-user"></i> <span class="font-mono" dir="ltr">{customer_phone_safe}</span></span>
        </div>'''

    return f'''
    <!DOCTYPE html>
    <html lang="ar" dir="rtl">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>بيع البطاقات اونلاين</title>
        <script src="https://cdn.tailwindcss.com"></script>
        <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css">
        <style>@import url('https://fonts.googleapis.com/css2?family=Cairo:wght@400;500;600;700;800&display=swap'); body{{font-family:'Cairo',sans-serif;}}</style>
        <script>
            tailwind.config = {{ darkMode: 'class' }}

            // التهيأ المبكر لمنع الوميض الأبيض الصامت
            if (localStorage.getItem('theme') === 'dark' || (!('theme' in localStorage) && window.matchMedia('(prefers-color-scheme: dark)').matches)) {{
                document.documentElement.classList.add('dark');
            }} else {{
                document.documentElement.classList.remove('dark');
            }}

            function toggleTheme() {{
                const html = document.documentElement;
                const icon = document.getElementById('themeIcon');
                if (html.classList.contains('dark')) {{
                    html.classList.remove('dark');
                    localStorage.setItem('theme', 'light');
                    icon.className = "fa-solid fa-moon";
                }} else {{
                    html.classList.add('dark');
                    localStorage.setItem('theme', 'dark');
                    icon.className = "fa-solid fa-sun";
                }}
            }}

            document.addEventListener("DOMContentLoaded", function() {{
                const icon = document.getElementById('themeIcon');
                if (document.documentElement.classList.contains('dark')) {{
                    if(icon) icon.className = "fa-solid fa-sun";
                }} else {{
                    if(icon) icon.className = "fa-solid fa-moon";
                }}
            }});

            function copyToClipboard(text, btn) {{
                navigator.clipboard.writeText(text);
                const icon = btn.querySelector('i');
                icon.className = "fa-solid fa-check text-indigo-500";
                setTimeout(() => {{ icon.className = "fa-regular fa-copy"; }}, 2000);
            }}

            function copyText(text, btn, label) {{
                navigator.clipboard.writeText(text).then(() => {{
                    const toast = document.getElementById('copyToast');
                    document.getElementById('copyToastMsg').textContent = `تم نسخ ${{label}}`;
                    toast.classList.remove('hidden');
                    setTimeout(() => {{ toast.classList.add('hidden'); }}, 2000);
                }});
            }}

            function applyQty(idx, qty) {{
                qty = Math.max(0, Math.min(100, isNaN(qty) ? 0 : qty));
                const display      = document.getElementById('qtyDisplay_' + idx);
                const qtyInput     = document.getElementById('qtyInput_' + idx);
                const priceInput   = document.getElementById('priceInput_' + idx);
                const profileInput = document.getElementById('profileInput_' + idx);
                const hoursInput   = document.getElementById('hoursInput_' + idx);
                display.value  = qty;
                qtyInput.value = qty;
                const chosen = qty > 0;
                // عناصر معطّلة (disabled) لا تُرسَل مع الفورم إطلاقاً — هيك بس
                // الباقات اللي فعلاً اختارها الزبون (عددها > 0) بتوصل للسيرفر،
                // والأربع حقول (سعر/بروفايل/ساعات/عدد) لازم تنفعّل/تنعطّل مع بعض
                // دايماً حتى تبقى مصفوفاتها الأربعة متوازية بالضبط عند الإرسال
                qtyInput.disabled     = !chosen;
                priceInput.disabled   = !chosen;
                profileInput.disabled = !chosen;
                hoursInput.disabled   = !chosen;
                document.getElementById('itemsError').classList.add('hidden');
                updateOrderSummary();
            }}

            function changeQty(idx, delta) {{
                const qtyInput = document.getElementById('qtyInput_' + idx);
                const current  = parseInt(qtyInput.value || '0', 10);
                applyQty(idx, current + delta);
            }}

            function onQtyTyped(idx, rawValue) {{
                // نسمح بس بأرقام (لو كتب حرف أو رمز يتجاهله)، وبنطبّق الحد 0-100 مباشرة
                const digitsOnly = rawValue.replace(/[^0-9]/g, '');
                applyQty(idx, parseInt(digitsOnly || '0', 10));
            }}

            function updateOrderSummary() {{
                let totalCount = 0, totalPrice = 0;
                document.querySelectorAll('.qty-row').forEach(row => {{
                    const price = parseFloat(row.dataset.price);
                    const qty   = parseInt(row.querySelector('input[name="quantity[]"]').value || '0', 10);
                    totalCount += qty;
                    totalPrice += qty * price;
                }});
                const summary = document.getElementById('orderSummary');
                if (summary) {{
                    summary.classList.toggle('hidden', totalCount === 0);
                    document.getElementById('summaryCount').textContent = totalCount;
                    document.getElementById('summaryPrice').textContent =
                        (totalPrice % 1 === 0) ? totalPrice : totalPrice.toFixed(2);
                }}
            }}

            // ملاحظة: الإرسال العادي (form POST مباشر) كان بيترك الزر عالق على
            // "جارٍ المعالجة..." للأبد لو انقطع الإنترنت وقت الضغط — المتصفح
            // بيحاول يتنقّل للصفحة التالية وما بينجح، وما في أي كود JS يشتغل
            // بعدها ليصلّح حالة الزر أو يوضّح للعميل شو صار. نتحكم بالإرسال
            // عبر fetch() عشان نلتقط فشل الشبكة فعلياً ونعطي رسالة واضحة.
            async function handleBuySubmit(e) {{
                e.preventDefault();

                const anyChosen = Array.from(document.querySelectorAll('input[name="quantity[]"]'))
                    .some(inp => !inp.disabled && parseInt(inp.value || '0', 10) > 0);
                if (!anyChosen) {{
                    document.getElementById('itemsError').classList.remove('hidden');
                    return;
                }}
                document.getElementById('itemsError').classList.add('hidden');

                const phone = document.getElementById('phoneInput').value.trim();
                const phonePattern = /^059[0-9]{{7}}$/;
                if (!phonePattern.test(phone)) {{
                    document.getElementById('phoneError').classList.remove('hidden');
                    return;
                }}
                document.getElementById('phoneError').classList.add('hidden');

                const netErr = document.getElementById('netError');
                netErr.classList.add('hidden');

                if (!navigator.onLine) {{
                    netErr.classList.remove('hidden');
                    return;
                }}

                const btn = document.getElementById('buyBtn');
                const originalHtml = btn.innerHTML;
                btn.disabled = true;
                btn.innerHTML = '<i class="fa-solid fa-spinner fa-spin"></i> <span>جارٍ المعالجة...</span>';
                btn.classList.add('opacity-70', 'cursor-not-allowed');

                try {{
                    const form = document.getElementById('buyForm');
                    const res = await fetch(form.action, {{ method: 'POST', body: new FormData(form) }});
                    window.location.href = res.url || window.location.href;
                }} catch (err) {{
                    // فشل فعلي بالشبكة (انقطاع إنترنت، DNS، إلخ) — نرجّع الزر لحالته
                    // الطبيعية ونوضّح للعميل بدل ما يضل عالق بدون أي تفسير
                    netErr.classList.remove('hidden');
                    btn.disabled = false;
                    btn.innerHTML = originalHtml;
                    btn.classList.remove('opacity-70', 'cursor-not-allowed');
                }}
            }}

            // تعطيل زر الشراء فوراً لو انقطع الإنترنت أثناء تصفّح الصفحة (مو بس
            // وقت الضغط)، وإعادة تفعيله تلقائياً لما يرجع الاتصال
            window.addEventListener('offline', function() {{
                const btn = document.getElementById('buyBtn');
                if (btn) btn.disabled = true;
            }});
            window.addEventListener('online', function() {{
                const btn = document.getElementById('buyBtn');
                if (btn) btn.disabled = false;
                const netErr = document.getElementById('netError');
                if (netErr) netErr.classList.add('hidden');
            }});
        </script>
    </head>
    <body class="bg-slate-50 dark:bg-[#0B1329] text-slate-800 dark:text-slate-200 min-h-screen flex flex-col justify-between transition-colors duration-200 relative">

        <button onclick="toggleTheme()" type="button" class="fixed top-6 left-6 z-50 w-12 h-12 rounded-xl bg-white dark:bg-[#1C2541] border border-slate-200 dark:border-[#3A506B]/50 shadow-xl text-slate-600 dark:text-indigo-400 flex items-center justify-center transition-all hover:scale-105 active:scale-95 text-md">
            <i id="themeIcon" class="fa-solid fa-moon"></i>
        </button>

        <div id="copyToast" class="hidden fixed top-4 left-1/2 -translate-x-1/2 z-50 bg-slate-800 dark:bg-[#1C2541] text-white text-xs font-bold px-4 py-2 rounded-full shadow-lg border dark:border-[#3A506B]/30 flex items-center gap-2">
            <i class="fa-solid fa-check text-indigo-400"></i>
            <span id="copyToastMsg">تم النسخ</span>
        </div>

        <div class="w-full max-w-lg mx-auto px-4 py-8">
            {account_bar_html}
        
            <div class="text-center mb-8">
                {logo_html}
                <h1 class="text-2xl font-extrabold text-slate-900 dark:text-white tracking-tight">{operator_name_safe} ⚡</h1>
                <p class="text-xs font-semibold text-slate-400 dark:text-slate-500 mt-1">بوابتك السريعة لشحن الإنترنت فورياً وبأمان</p>
            </div>

            {recent_html}

            <div class="bg-white dark:bg-[#1C2541] rounded-3xl p-6 mb-6 border border-slate-100 dark:border-[#3A506B]/30 shadow-xl shadow-slate-100/50 dark:shadow-none">
                <div class="flex items-center gap-2 pb-4 mb-5 border-b border-slate-100 dark:border-[#3A506B]/20 text-slate-800 dark:text-white font-bold text-md">
                    <i class="fa-solid fa-basket-shopping text-indigo-500"></i>
                    <h2>طلب كرت جديد</h2>
                </div>

                <form id="buyForm" action="{buy_action}" method="POST" class="space-y-4" onsubmit="handleBuySubmit(event)">
                    <!-- [FIX 2] CSRF Token -->
                    <input type="hidden" name="_csrf_token" value="{csrf_token}">
                    
                    <div id="itemsContainer" class="space-y-2.5">
                        {options_html}
                    </div>
                    <span id="itemsError" class="hidden text-[11px] text-rose-500 font-bold block">
                        <i class="fa-solid fa-circle-exclamation"></i> اختر باقة واحدة على الأقل قبل المتابعة
                    </span>

                    <div id="orderSummary" class="hidden flex items-center justify-between bg-indigo-50 dark:bg-indigo-950/20 border border-indigo-100 dark:border-indigo-900/30 rounded-xl px-4 py-2.5">
                        <span class="text-xs font-bold text-indigo-500 dark:text-indigo-400">
                            <i class="fa-solid fa-cart-shopping"></i> <span id="summaryCount">0</span> كرت مختار
                        </span>
                        <span class="text-sm font-extrabold text-indigo-700 dark:text-indigo-300"><span id="summaryPrice">0</span> شيكل</span>
                    </div>

                    <div>
                        <label class="block text-xs font-bold text-slate-500 dark:text-slate-400 mb-1.5">
                            <i class="fa-solid fa-mobile-screen-button text-[11px]"></i> رقم هاتف محفظة Jawwal Pay
                        </label>
                        <div class="relative">
                            <span class="absolute inset-y-0 right-0 flex items-center pr-3.5 text-slate-400 text-sm"><i class="fa-solid fa-phone"></i></span>
                            <input id="phoneInput" type="text" name="phone" placeholder="059xxxxxxx"
                                required pattern="059[0-9]{{7}}" inputmode="numeric" autocomplete="off"
                                class="w-full pr-10 pl-4 py-3 bg-slate-50 dark:bg-[#0B1329] border border-slate-200 dark:border-[#3A506B]/30 rounded-xl text-sm font-semibold tracking-wide text-slate-700 dark:text-slate-300 placeholder-slate-300 dark:placeholder-slate-600 focus:outline-none focus:border-indigo-500 focus:bg-white dark:focus:bg-[#0B1329] transition-all text-center"
                                oninput="document.getElementById('phoneError').classList.add('hidden')">
                        </div>
                        <span id="phoneError" class="hidden text-[11px] text-rose-500 font-bold mt-1 block">
                            <i class="fa-solid fa-circle-exclamation"></i> الرقم يجب أن يبدأ بـ 059 ومكوّن من 10 أرقام
                        </span>
                        <p class="text-[10px] text-slate-400 dark:text-slate-500 font-bold mt-1.5">
                            <i class="fa-solid fa-circle-info"></i> اكتب رقم محفظة Jawwal Pay اللي رح تحوّل منها (مش لازم يكون رقم حسابك). بطاقتك بتظهر دايماً بصفحة «كروتي» بحسابك.
                        </p>
                    </div>

                    <div>
                        <label class="block text-xs font-bold text-slate-500 dark:text-slate-400 mb-1.5">
                            <i class="fa-solid fa-credit-card text-[11px]"></i> بوابة الدفع المعتمدة
                        </label>
                        <div class="border-2 border-indigo-500/70 dark:border-indigo-500/50 bg-indigo-50/40 dark:bg-indigo-950/20 rounded-2xl p-3 flex items-center justify-between">
                            <div class="flex items-center gap-2.5">
                                <div class="w-8 h-8 rounded-lg bg-indigo-500 text-white flex items-center justify-center text-xs font-bold"><i class="fa-solid fa-wallet"></i></div>
                                <div>
                                    <div class="text-xs font-bold text-indigo-700 dark:text-indigo-400">Jawwal Pay</div>
                                    <div class="text-[10px] font-semibold text-indigo-400 dark:text-indigo-500">دفع فوري آمن ومباشر</div>
                                </div>
                            </div>
                            <span class="bg-indigo-500 text-white text-[10px] font-bold px-2 py-0.5 rounded-full flex items-center gap-1"><i class="fa-solid fa-check"></i> نشط</span>
                        </div>
                    </div>

                    <span id="netError" class="hidden text-[11px] text-rose-500 font-bold block">
                        <i class="fa-solid fa-wifi"></i> لا يوجد اتصال بالإنترنت حالياً. تأكد من الاتصال وحاول مرة أخرى.
                    </span>

                    <button id="buyBtn" type="submit"
                        class="w-full bg-gradient-to-r from-indigo-500 via-indigo-600 to-purple-600 hover:from-indigo-600 hover:to-purple-700 text-white font-bold py-3.5 px-4 rounded-xl shadow-lg shadow-indigo-100 dark:shadow-none hover:shadow-xl transition-all transform hover:-translate-y-0.5 flex items-center justify-center gap-2 text-sm mt-2 disabled:opacity-50 disabled:cursor-not-allowed">
                        <i class="fa-solid fa-shield-halved"></i>
                        <span>الانتقال للدفع الآمن واستلام الكروت</span>
                    </button>
                </form>
            </div>

            <!-- ── مربع الدعم الفني ── -->
            <div class="bg-white dark:bg-[#1C2541] rounded-3xl p-5 mb-6 border border-slate-100 dark:border-[#3A506B]/30 shadow-xl shadow-slate-100/50 dark:shadow-none mt-6">
                <div class="flex items-center gap-2 pb-4 mb-4 border-b border-slate-100 dark:border-[#3A506B]/20 text-slate-800 dark:text-white font-bold text-md">
                    <i class="fa-solid fa-headset text-indigo-500"></i>
                    <h2>🛠️ الدعم الفني</h2>
                </div>    
                <p class="text-xs text-slate-500 dark:text-slate-400 mb-4 leading-relaxed font-medium">
                    هل واجهت مشكلة في الشراء أو لم تصلك بطاقتك؟ تواصل معنا مباشرة وسنساعدك فوراً.
                </p>
                <a href="tel:{support_phone_safe}"
                    class="flex items-center gap-3 w-full bg-indigo-50 dark:bg-indigo-950/20 border border-indigo-100 dark:border-indigo-900/30 hover:bg-indigo-100 dark:hover:bg-indigo-950/40 py-3 px-4 rounded-xl transition-all transform active:scale-95">
                    <div class="w-9 h-9 rounded-xl bg-indigo-500 text-white flex items-center justify-center text-base shrink-0">
                        <i class="fa-solid fa-phone"></i>
                    </div>
                    <div class="text-right flex-1">
                        <div class="text-xs font-bold text-slate-500 dark:text-slate-400">رقم الجوال</div>
                        <div class="text-base font-extrabold text-indigo-600 dark:text-indigo-400 tracking-widest font-mono">{support_phone_disp}</div>
                    </div>
                    <i class="fa-solid fa-chevron-left text-xs text-indigo-400"></i>
                </a>
            </div>
        </div>

        <footer class="text-center py-4 text-[10px] font-bold text-slate-400 dark:text-slate-500 border-t border-slate-200/40 dark:border-[#3A506B]/20 w-full bg-white dark:bg-[#1C2541]">
            جميع الحقوق محفوظة © شبكة market hotspot 2026
        </footer>
    </body>
    </html>
    '''