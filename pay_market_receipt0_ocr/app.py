from flask import Flask, session, redirect, url_for, request, abort
import sqlite3
import os
import re
import secrets
import time
import json
from flask.sessions import SecureCookieSessionInterface
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import generate_password_hash

# استيراد الـ Blueprints
from routes_user import user_bp
from routes_admin import admin_bp
from routes_dev import dev_bp
from routes_payment import payment_bp

app = Flask(__name__)

# ── دعم البروكسي العكسي (nginx / PythonAnywhere وغيره) ──────────────────────
_PROXY_LAYERS = int(os.environ.get("PROXY_LAYERS", "0"))
if _PROXY_LAYERS > 0:
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=_PROXY_LAYERS, x_proto=_PROXY_LAYERS,
                             x_host=_PROXY_LAYERS, x_port=_PROXY_LAYERS)

# ── مفتاح سري قوي: يُقرأ من متغير البيئة، أو من ملف محلي، أو يُولَّد ويُحفظ تلقائياً ──
# هذا يضمن أن الجلسات لا تنتهي عند كل إعادة تشغيل
_SECRET_KEY_FILE = 'secret.key'
def _load_or_create_secret_key():
    if os.environ.get("SECRET_KEY"):
        return os.environ["SECRET_KEY"]
    if os.path.exists(_SECRET_KEY_FILE):
        with open(_SECRET_KEY_FILE, 'r') as f:
            return f.read().strip()
    key = secrets.token_hex(32)
    with open(_SECRET_KEY_FILE, 'w') as f:
        f.write(key)
    return key

app.secret_key = _load_or_create_secret_key()

# ── إعدادات الجلسة الآمنة ──────────────────────────────────────────────────
app.config['SESSION_COOKIE_HTTPONLY'] = True       # منع JS من قراءة الكوكي
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'      # حماية من CSRF
app.config['SESSION_COOKIE_SECURE'] = os.environ.get("HTTPS_ENABLED", "false").lower() == "true"
app.config['PERMANENT_SESSION_LIFETIME'] = 1800    #انتهاء الجلسة بعد 30 دقيقة

# ── فصل كوكي الجلسة حسب اللوحة ────────────────────────────────────────────
_ADMIN_PATH_RE = re.compile(r'^/([^/]+)/admin(?:/|$)')

class ScopedSessionInterface(SecureCookieSessionInterface):
    def get_cookie_name(self, app):
        path = request.path or '/'
        if path == '/dev' or path.startswith('/dev/'):
            return 'dev_session'
        m = _ADMIN_PATH_RE.match(path)
        if m:
            return f'admin_session_{m.group(1)}'
        return app.config.get('SESSION_COOKIE_NAME', 'session')

app.session_interface = ScopedSessionInterface()

#حد حجم الملفات المرفوعة (5MB كحد أقصى)
app.config['MAX_CONTENT_LENGTH'] = 5 * 1024 * 1024    # 5 ميغابايت

# تحديد المسار المطلق لمجلد المشروع لتجنب ضياع ملف قاعدة البيانات
BASE_DIR = os.path.abspath(os.path.dirname(__file__))

DATABASE_URL = os.environ.get('DATABASE_URL')
if DATABASE_URL and DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

# إذا لم يتوفر رابط سحابي، استخدم مساراً مطلقاً لملف الـ SQLite داخل مجلد المشروع
DATABASE = DATABASE_URL if DATABASE_URL else os.path.join(BASE_DIR, 'hotspot_cards.db')

#CSRF PROTECTION — توليد وفحص التوكن# ── CSRF ────────────────────────────────────────────────────────────────────
def generate_csrf_token():
    if '_csrf_token' not in session:
        session['_csrf_token'] = secrets.token_hex(32)
    return session['_csrf_token']

def validate_csrf():
    token      = session.get('_csrf_token')
    form_token = request.form.get('_csrf_token')
    if not token or not form_token or not secrets.compare_digest(token, form_token):
        abort(403)

# تسجيل الدالة لاستخدامها في القوالب وتمريرها للـ Blueprints
app.jinja_env.globals['csrf_token'] = generate_csrf_token
app.generate_csrf_token = generate_csrf_token
app.validate_csrf       = validate_csrf

#SESSION TIMEOUT — فحص انتهاء الجلسة عند كل طلب
SESSION_TIMEOUT = 1800  # 30 دقيقة بالثواني — مدة ثابتة من وقت الدخول، لا تتمدّد بالنشاط

@app.before_request
def check_session_timeout():
    """
    تسجيل الخروج التلقائي بعد 30 دقيقة من وقت تسجيل الدخول (login_time) —
    مدة ثابتة absolute وليست منزلقة sliding. أي نشاط (تحديث صفحة، تصدير ملف،
    مسح سجل، نبضة session-ping بالخلفية...) لا يُمدّد الجلسة ولا يعيد ضبط
    العداد، لأن ضبطه على كل طلب كان يمنع انتهاء الجلسة فعلياً طالما فيه أي
    نشاط ولو بسيط ضمن كل نافذة 30 دقيقة.
    """
    if session.get('admin_logged_in') or session.get('dev_logged_in'):
        login_time = session.get('login_time', 0)
        if time.time() - login_time > SESSION_TIMEOUT:
            was_dev        = session.get('dev_logged_in')
            operator_slug  = session.get('operator_slug')
            session.clear()
            if was_dev:
                return redirect(url_for('dev.dev_login'))
            elif operator_slug:
                return redirect(url_for('admin.admin_login', slug=operator_slug))

#HTTPS ENFORCEMENT — إعادة التوجيه لـ HTTPS
@app.before_request
def enforce_https():
    """إجبار HTTPS في بيئة الإنتاج"""
    if os.environ.get("HTTPS_ENABLED", "false").lower() == "true":
        if not request.is_secure and request.headers.get('X-Forwarded-Proto', 'http') != 'https':
            url = request.url.replace('http://', 'https://', 1)
            return redirect(url, code=301)

# Security Headers — رؤوس أمان على كل استجابة
@app.after_request
def add_security_headers(response):
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['X-Frame-Options']        = 'DENY'
    response.headers['X-XSS-Protection']       = '1; mode=block'
    response.headers['Referrer-Policy']        = 'strict-origin-when-cross-origin'
    if os.environ.get("HTTPS_ENABLED", "false").lower() == "true":
        response.headers['Strict-Transport-Security'] = 'max-age=31536000; includeSubDomains'
    return response

def init_db_auto():
    """تهيئة قاعدة البيانات وتحديث الهيكل تلقائياً بشكل شامل وآمن"""
    conn = sqlite3.connect(DATABASE)
    cursor = conn.cursor()

    try:
        # 1. جدول حماية الطلبات وتحديد المعدل (Rate Limiting)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS rate_limits (
                key TEXT PRIMARY KEY, 
                count INTEGER, 
                window_start REAL, 
                locked_until REAL
            )
        """)
        
        # 2. جدول مسؤولي المطورين (Superadmin)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS dev_admins (
                id INTEGER PRIMARY KEY CHECK (id = 1), 
                username TEXT NOT NULL, 
                password_hash TEXT NOT NULL,
                warning_dismissed INTEGER DEFAULT 0
            )
        """)
        
        # 3. جدول المشغلين والشبكات (Operators - Multi-Tenant)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS operators (
                id INTEGER PRIMARY KEY AUTOINCREMENT, 
                name TEXT, 
                username TEXT, 
                slug TEXT UNIQUE NOT NULL,
                balance REAL,
                lifetime_revenue REAL DEFAULT 0,
                is_active INTEGER DEFAULT 1,
                jawwal_pay_number TEXT,
                support_phone TEXT,
                hotspot_title TEXT,
                hotspot_login_url TEXT DEFAULT 'http://00.00.00.00/login',
                logo_size TEXT DEFAULT 'medium',
                logo_filename TEXT,
                telegram_bot_token TEXT,
                telegram_chat_id TEXT,
                telegram_webhook_secret TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        
        # 4. جدول مسؤولي المشغلين (Operator Admins)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS operator_admins (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                operator_id INTEGER NOT NULL UNIQUE,
                username TEXT NOT NULL,
                password_hash TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(operator_id) REFERENCES operators(id)
            )
        """)
        
        # 5. جدول البطاقات والكروت (Vouchers)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS vouchers (
                id INTEGER PRIMARY KEY AUTOINCREMENT, 
                operator_id INTEGER,
                username TEXT NOT NULL,
                password TEXT NOT NULL,
                code TEXT, 
                price REAL NOT NULL, 
                status TEXT DEFAULT 'available',
                profile TEXT NOT NULL,
                duration_hours INTEGER NOT NULL DEFAULT 0,
                seq INTEGER,
                FOREIGN KEY(operator_id) REFERENCES operators(id)
            )
        """)

        # 6. جدول الطلبات المعلقة والتحقق من الإيصالات (Pending Orders & OCR)
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS pending_orders (
                id                 INTEGER PRIMARY KEY AUTOINCREMENT,
                order_token        TEXT UNIQUE NOT NULL,
                operator_id        INTEGER NOT NULL,
                customer_phone     TEXT NOT NULL,
                voucher_ids        TEXT NOT NULL,
                wallet_code        TEXT NOT NULL,
                base_amount        REAL NOT NULL,
                exact_amount       REAL NOT NULL,
                status             TEXT NOT NULL DEFAULT 'pending',
                receipt_filename   TEXT,
                receipt_hash       TEXT,
                ocr_raw_text       TEXT,
                ocr_parsed_amount  REAL,
                ocr_match_status   TEXT,
                ocr_reference      TEXT,
                created_at         TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                expires_at         REAL NOT NULL,
                confirmed_at       TIMESTAMP,
                FOREIGN KEY(operator_id) REFERENCES operators(id)
            )
        ''')
        
        # 7. جدول المعاملات والمبالغ المدفوعة (Transactions)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS transactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                operator_id INTEGER,
                amount REAL,
                amount_paid REAL,
                status TEXT,
                wallet_code TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(operator_id) REFERENCES operators(id)
            )
        """)
        
        # 8. جدول سجلات الأرباح والإيرادات اليومية (Revenue Log)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS revenue_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                operator_id INTEGER NOT NULL,
                amount REAL NOT NULL,
                occurred_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(operator_id) REFERENCES operators(id)
            )
        """)
        
        # 9. سجلات التدقيق والأمان (Audit Log)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS audit_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                operator_id INTEGER,
                actor TEXT,
                action TEXT NOT NULL,
                details TEXT,
                ip_address TEXT,
                event_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # 10. جدول سجلات الرسائل القديم (للحفاظ على التوافقية التاريخية SMS Log)
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS sms_log (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            operator_id      INTEGER NOT NULL,
            raw_message      TEXT,
            parsed_amount    REAL,
            parsed_phone     TEXT,
            matched_order_id INTEGER,
            match_status     TEXT NOT NULL,
            received_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        ''')

        # ترقية جدول المعاملات (Transactions) تلقائياً إن كانت الأعمدة غير موجودة
        tx_cols = {
            row[1]
            for row in cursor.execute("PRAGMA table_info(transactions)").fetchall()
        }
        tx_migrations = [
            ("customer_phone", "TEXT"),
            ("voucher_id", "INTEGER"),
            ("hidden_by_customer", "INTEGER DEFAULT 0"),
            ("transaction_id", "TEXT"),
        ]
        for col_name, col_def in tx_migrations:
            if col_name not in tx_cols:
                cursor.execute(f"ALTER TABLE transactions ADD COLUMN {col_name} {col_def}")

        # ── إنشاء الفهارس (Indexes) لسرعة الأداء ───────────────────────────
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_vouchers_operator ON vouchers(operator_id, id)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_vouchers_lookup ON vouchers(operator_id, status, price, profile, duration_hours)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_pending_orders_receipt_hash ON pending_orders(operator_id, receipt_hash)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_pending_orders_ocr_reference ON pending_orders(operator_id, ocr_reference)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_pending_orders_lookup ON pending_orders(operator_id, status, exact_amount)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_sms_log_operator ON sms_log(operator_id, received_at)")

        # ── إنشاء الحسابات الافتراضية عند التشغيل لأول مرة ──────────────────
        
        # أ. حساب المشغل الافتراضي
        existing_operator = cursor.execute("SELECT id FROM operators LIMIT 1").fetchone()
        if not existing_operator:
            cursor.execute(
                "INSERT INTO operators (slug, name, is_active) VALUES (?, ?, 1)",
                ("default", "شبكة market للانترنت")
            )
            default_op_id = cursor.lastrowid

            cursor.execute(
                "INSERT INTO operator_admins (operator_id, username, password_hash) VALUES (?, ?, ?)",
                (default_op_id, "admin", generate_password_hash("2026"))
            )

            # ربط أي بيانات يتيمة بالمشغل الافتراضي
            cursor.execute("UPDATE vouchers SET operator_id=? WHERE operator_id IS NULL", (default_op_id,))
            cursor.execute("UPDATE transactions SET operator_id=? WHERE operator_id IS NULL", (default_op_id,))
            cursor.execute("UPDATE audit_log SET operator_id=? WHERE operator_id IS NULL", (default_op_id,))

        # ب. حساب المطور الافتراضي (Superadmin)
        dev_existing = cursor.execute("SELECT id FROM dev_admins WHERE id=1").fetchone()
        if not dev_existing:
            cursor.execute(
                "INSERT INTO dev_admins (id, username, password_hash) VALUES (1, ?, ?)",
                ("dev", generate_password_hash("ChangeMe#2026"))
            )

        conn.commit()
        print("✅ تم فحص وإنشاء جميع جداول وقواعد بيانات الموقع بنجاح تام وبدون أخطاء.")

    except Exception as e:
        print("❌ خطأ أثناء تهيئة قاعدة البيانات:", e)
        conn.rollback()
    finally:
        conn.close()
        
# تسجيل Blueprints
app.register_blueprint(user_bp)
app.register_blueprint(admin_bp)
app.register_blueprint(dev_bp, url_prefix='/dev')
app.register_blueprint(payment_bp)

@app.route('/')
def home():
    # الجذر لا يُعرّف مشغّلاً معيّناً — نوجّه لبوابة المطوّر لاختيار/إدارة المشغّلين
    return redirect(url_for('dev.dev_login'))

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('home'))

# معالج خطأ حجم الملف
@app.errorhandler(413)
def file_too_large(e):
    back_url = request.referrer or ''
    if not back_url.startswith(request.host_url):
        back_url = url_for('home')
    sep = '&' if '?' in back_url else '?'
    safe_url = json.dumps(back_url + sep + 'error=file_too_large')
    return f'''
    <script>
    window.location.href = {safe_url};
    </script>
    ''', 413

# معالج خطأ 403 — CSRF أو محظور
@app.errorhandler(403)
def forbidden(e):
    return '''<!DOCTYPE html>
<html lang="ar" dir="rtl">
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>خطأ 403</title>
<script src="https://cdn.tailwindcss.com"></script>
<style>@import url('https://fonts.googleapis.com/css2?family=Cairo:wght@700;800&display=swap');body{font-family:'Cairo',sans-serif}</style>
</head>
<body class="bg-slate-50 min-h-screen flex items-center justify-center p-4">
<div class="bg-white rounded-3xl shadow-xl max-w-sm w-full p-8 text-center">
    <div class="w-20 h-20 bg-rose-50 rounded-2xl mx-auto mb-5 flex items-center justify-center">
        <i class="fa-solid fa-ban text-rose-400 text-4xl"></i>
    </div>
    <h2 class="text-xl font-extrabold text-slate-800 mb-2">انتهت صلاحية الطلب</h2>
    <p class="text-sm text-slate-500 mb-7">انتهت صلاحية الجلسة أو الطلب غير مصرح به. يرجى العودة والمحاولة مجدداً.</p>
    <a href="/" class="block w-full bg-gradient-to-r from-indigo-500 to-purple-600 text-white font-bold py-3.5 px-4 rounded-xl text-sm">العودة للرئيسية</a>
</div>
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css">
</body></html>''', 403

# معالج خطأ 404
@app.errorhandler(404)
def not_found(e):
    return '''<!DOCTYPE html>
<html lang="ar" dir="rtl">
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>صفحة غير موجودة</title>
<script src="https://cdn.tailwindcss.com"></script>
<style>@import url('https://fonts.googleapis.com/css2?family=Cairo:wght@700;800&display=swap');body{font-family:'Cairo',sans-serif}</style>
</head>
<body class="bg-slate-50 min-h-screen flex items-center justify-center p-4">
<div class="bg-white rounded-3xl shadow-xl max-w-sm w-full p-8 text-center">
    <div class="w-20 h-20 bg-amber-50 rounded-2xl mx-auto mb-5 flex items-center justify-center">
        <i class="fa-solid fa-map-location-dot text-amber-400 text-4xl"></i>
    </div>
    <h2 class="text-xl font-extrabold text-slate-800 mb-2">الصفحة غير موجودة</h2>
    <p class="text-sm text-slate-500 mb-7">الرابط الذي طلبته غير موجود أو تم نقله.</p>
    <a href="/" class="block w-full bg-gradient-to-r from-indigo-500 to-purple-600 text-white font-bold py-3.5 px-4 rounded-xl text-sm">العودة للرئيسية</a>
</div>
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css">
</body></html>''', 404

# تنفيذ دالة التهيئة فوراً عند تشغيل أو استيراد التطبيق (لكي تعمل مع Gunicorn على Render)
try:
    init_db_auto()
except Exception as _e:
    print("Startup DB Init Warning:", _e)

if __name__ == '__main__':
    app.run(host='0.0.0.0', debug=False, port=5000)
