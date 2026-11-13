from flask import Flask, session, redirect, url_for, request, abort
import sqlite3
import os
import re
import secrets
import time
import json
from flask.sessions import SecureCookieSessionInterface
from werkzeug.middleware.proxy_fix import ProxyFix

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

# قراءة رابط قاعدة البيانات السحابية من Render، والرجوع للمحلية عند العمل على حاسوبك
DATABASE_URL = os.environ.get('DATABASE_URL')
if DATABASE_URL and DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

# إذا لم يتوفر رابط سحابي، استخدم SQLite المحلي
DATABASE = DATABASE_URL if DATABASE_URL else 'hotspot_cards.db'

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
    """تهيئة قاعدة البيانات وتحديث الهيكل تلقائياً"""
    conn   = sqlite3.connect(DATABASE)
    cursor = conn.cursor()

    # فحص ذكي: إذا كانت قاعدة البيانات قديمة ولا تحتوي على عمود username
    try:
        cursor.execute("SELECT username FROM vouchers LIMIT 1")
    except sqlite3.OperationalError:
        cursor.execute("DROP TABLE IF EXISTS vouchers")
        cursor.execute("DROP TABLE IF EXISTS transactions")

# إنشاء الجداول بطريقة آمنة ومستقلة عند الإقلاع
try:
    conn = sqlite3.connect('hotspot_cards.db')
    cursor = conn.cursor()
    
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS rate_limits (
            key TEXT PRIMARY KEY,
            count INTEGER,
            window_start REAL,
            locked_until REAL
        )
    ''')
    
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS dev_admins (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL
        )
    ''')
    conn.commit()
    conn.close()
except Exception as e:
    print("Database init error:", e)

# استدعاء الدالة عند تشغيل التطبيق
with app.app_context():
    init_sqlite_db()
    
    # جدول البطاقات
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS vouchers (
        id       INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT    NOT NULL,
        password TEXT    NOT NULL,
        price    REAL    NOT NULL,
        profile  TEXT    NOT NULL,
        status   TEXT    DEFAULT 'available',
        UNIQUE(username, password)
    )
    ''')

    # جدول العمليات
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS transactions (
        id             INTEGER PRIMARY KEY AUTOINCREMENT,
        transaction_id TEXT    UNIQUE,
        customer_phone TEXT,
        voucher_id     INTEGER,
        amount_paid    REAL,
        wallet_code    TEXT,
        created_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY(voucher_id) REFERENCES vouchers(id)
    )
    ''')

    # ترقية: إضافة wallet_code لقواعد البيانات القديمة
    try:
        cursor.execute("SELECT wallet_code FROM transactions LIMIT 1")
    except sqlite3.OperationalError:
        cursor.execute("ALTER TABLE transactions ADD COLUMN wallet_code TEXT")

    # ── جدول سجل النشاط (Audit Log) ────────────────────────────────────────
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS audit_log (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        event_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        actor      TEXT NOT NULL,
        action     TEXT NOT NULL,
        details    TEXT,
        ip_address TEXT
    )
    ''')

    # إضافة operator_id للجداول القديمة إذا لم يكن موجوداً — يجب أن يحدث هنا
    for _table in ('vouchers', 'transactions', 'audit_log'):
        try:
            cursor.execute(f"SELECT operator_id FROM {_table} LIMIT 1")
        except sqlite3.OperationalError:
            cursor.execute(f"ALTER TABLE {_table} ADD COLUMN operator_id INTEGER")

    # ── جدول Rate Limiting مشترك (بديل القواميس بالذاكرة) ──────────────────
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS rate_limits (
        key          TEXT PRIMARY KEY,
        count        INTEGER NOT NULL DEFAULT 0,
        window_start REAL    NOT NULL DEFAULT 0,
        locked_until REAL    NOT NULL DEFAULT 0
    )
    ''')

    # تنظيف: حذف أي جدول قديم غير مستخدم متبقٍّ من إصدارات سابقة من قاعدة
    # البيانات الفعلية (لا يوجد أي كود آخر يُنشئه أو يقرأ منه بعد الآن)
    cursor.execute("DROP TABLE IF EXISTS admin_settings")

    # ═══════════════════════════════════════════════════════════════════════
    # ── دعم تعدد المشغّلين (Multi-Tenant) ──────────────────────────────────
    # ═══════════════════════════════════════════════════════════════════════

    # جدول المشغّلين (كل مشغّل = شبكة/نقطة بيع مستقلة بواجهتها الخاصة)
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS operators (
        id                 INTEGER PRIMARY KEY AUTOINCREMENT,
        slug               TEXT UNIQUE NOT NULL,
        name               TEXT NOT NULL,
        hotspot_title      TEXT DEFAULT '',
        hotspot_login_url  TEXT DEFAULT 'http://00.00.00.00/login',
        jawwal_pay_number  TEXT DEFAULT '',
        support_phone      TEXT DEFAULT '',
        is_active          INTEGER DEFAULT 1,
        lifetime_revenue   REAL DEFAULT 0,
        created_at         TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    ''')

    # ترقية: عنوان (رابط) صفحة تسجيل الدخول بجهاز الهوتسبوت (MikroTik) الخاص
    try:
        cursor.execute("SELECT hotspot_login_url FROM operators LIMIT 1")
    except sqlite3.OperationalError:
        cursor.execute("ALTER TABLE operators ADD COLUMN hotspot_login_url TEXT DEFAULT 'http://00.00.00.00/login'")

    # ترقية: أعمدة جديدة على جدول operators لقواعد البيانات القديمة
    try:
        cursor.execute("SELECT hotspot_title FROM operators LIMIT 1")
    except sqlite3.OperationalError:
        cursor.execute("ALTER TABLE operators ADD COLUMN hotspot_title TEXT DEFAULT ''")
    try:
        cursor.execute("SELECT lifetime_revenue FROM operators LIMIT 1")
    except sqlite3.OperationalError:
        cursor.execute("ALTER TABLE operators ADD COLUMN lifetime_revenue REAL DEFAULT 0")
        # ملاحظة: تعبئة الرصيد الأولي من transactions تحتاج operator_id مربوطاً
        # فعلياً بكل عملية، وهذا لا يتوفر إلا بعد اكتمال تهيئة المشغّل
        # الافتراضي أدناه — لذلك التعبئة الفعلية تحصل لاحقاً، وليس هنا.

    # ترقية: إعدادات تنبيه تيليجرام الاختيارية — عند وصول إشعار دفع يحتاج مراجعة يدوية
    try:
        cursor.execute("SELECT telegram_bot_token FROM operators LIMIT 1")
    except sqlite3.OperationalError:
        cursor.execute("ALTER TABLE operators ADD COLUMN telegram_bot_token TEXT")
        cursor.execute("ALTER TABLE operators ADD COLUMN telegram_chat_id TEXT")

    # ترقية: رمز سرّي فريد لكل مشغّل يُستخدم برابط استقبال ضغطات أزرار
    # "تأكيد/رفض" من تيليجرام (/telegram/webhook/<secret>) — يُولَّد تلقائياً
    # أول مرة يحفظ فيها المشغّل إعدادات بوت تيليجرام
    try:
        cursor.execute("SELECT telegram_webhook_secret FROM operators LIMIT 1")
    except sqlite3.OperationalError:
        cursor.execute("ALTER TABLE operators ADD COLUMN telegram_webhook_secret TEXT")

    # ترقية: شعار (لوجو) خاص بكل شبكة يظهر بمتجرها بدل أيقونة الواي فاي الافتراضية
    try:
        cursor.execute("SELECT logo_filename FROM operators LIMIT 1")
    except sqlite3.OperationalError:
        cursor.execute("ALTER TABLE operators ADD COLUMN logo_filename TEXT DEFAULT ''")

    # ترقية منفصلة: حجم عرض اللوجو (قد تكون logo_filename موجودة من تحديث سابق
    # بدون logo_size، فلازم فحص مستقل حتى تُضاف لقواعد البيانات المُرقّاة سابقاً)
    try:
        cursor.execute("SELECT logo_size FROM operators LIMIT 1")
    except sqlite3.OperationalError:
        cursor.execute("ALTER TABLE operators ADD COLUMN logo_size TEXT DEFAULT 'medium'")

    # ترقية: عدد ساعات الاستخدام لكل فئة بطاقات (منفصل عن بروفايل السرعة) —
    # يُستخدم لعرض "X ساعات" بوضوح للزبون في المتجر بدل الاعتماد على بروفايل السرعة فقط
    try:
        cursor.execute("SELECT duration_hours FROM vouchers LIMIT 1")
    except sqlite3.OperationalError:
        cursor.execute("ALTER TABLE vouchers ADD COLUMN duration_hours INTEGER NOT NULL DEFAULT 0")
        # تعبئة فورية لعدد الساعات للكروت المرفوعة مسبقاً (قبل وجود هذا العمود)
        # حسب نفس الجدول الثابت (بروفايل + سعر) المستخدم بالكود الآن
        for _profile, _price, _hours in [("2M", 1, 8), ("3M", 2, 10), ("5M", 2, 2), ("2M", 3, 24)]:
            cursor.execute("UPDATE vouchers SET duration_hours=? WHERE profile=? AND price=?", (_hours, _profile, _price))

    # ═══════════════════════════════════════════════════════════════════════
    # ── نظام التحقق من الدفع عبر رفع صورة الإشعار + قراءة آلية (OCR) ─────────
    # ═══════════════════════════════════════════════════════════════════════
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS pending_orders (
        id                 INTEGER PRIMARY KEY AUTOINCREMENT,
        order_token        TEXT    UNIQUE NOT NULL,
        operator_id        INTEGER NOT NULL,
        customer_phone     TEXT    NOT NULL,
        voucher_ids        TEXT    NOT NULL,
        wallet_code        TEXT    NOT NULL,
        base_amount        REAL    NOT NULL,
        exact_amount       REAL    NOT NULL,
        status             TEXT    NOT NULL DEFAULT 'pending',
        receipt_filename   TEXT,
        receipt_hash       TEXT,
        ocr_raw_text       TEXT,
        ocr_parsed_amount  REAL,
        ocr_match_status   TEXT,
        ocr_reference      TEXT,
        created_at         TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        expires_at         REAL    NOT NULL,
        confirmed_at       TIMESTAMP,
        FOREIGN KEY(operator_id) REFERENCES operators(id)
    )
    ''')
    # ترقية: أعمدة صورة الإشعار وقراءة الـOCR لقواعد بيانات مُنشأة قبل التحويل
    # من نظام تحويل رسائل SMS إلى رفع صورة الإشعار. ocr_reference أُضيف لاحقاً
    # (رمز الحركة المستخرَج من الإيصال) لمنع إعادة استخدام نفس التحويل الحقيقي
    # عبر صورة مختلفة (بعكس receipt_hash اللي بيتغيّر لو انضغطت/انقصّت الصورة)
    for _col, _decl in [
        ('receipt_filename',  'TEXT'),
        ('receipt_hash',      'TEXT'),
        ('ocr_raw_text',      'TEXT'),
        ('ocr_parsed_amount', 'REAL'),
        ('ocr_match_status',  'TEXT'),
        ('ocr_reference',     'TEXT'),
    ]:
        try:
            cursor.execute(f"SELECT {_col} FROM pending_orders LIMIT 1")
        except sqlite3.OperationalError:
            cursor.execute(f"ALTER TABLE pending_orders ADD COLUMN {_col} {_decl}")
    # فهرس: نبحث بـ receipt_hash عن إعادة استخدام نفس صورة الإشعار (احتيال محتمل)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_pending_orders_receipt_hash ON pending_orders(operator_id, receipt_hash)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_pending_orders_ocr_reference ON pending_orders(operator_id, ocr_reference)")
    # فهارس: نبحث كثيراً بـ (operator_id, status) لجلب الطلبات المعلّقة عند
    # المطابقة، وبـ order_token لصفحة الانتظار
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_pending_orders_lookup ON pending_orders(operator_id, status, exact_amount)")

    # تنظيف: نظام تحويل رسائل SMS أُزيل نهائياً — نحذف عمود التوكن الخاص به
    # لو كان موجوداً بقاعدة بيانات مُرقّاة من نسخة سابقة (يتطلب SQLite 3.35+
    # لدعم DROP COLUMN؛ الاستثناء يحمي أي نسخة أقدم فقط، والعمود يبقى غير
    # مُستخدَم بالكود بأي حال لو تعذّر حذفه). جدول sms_log القديم يبقى كسجل
    # تاريخي كما هو (بيانات وليست كوداً) ولا يُكتب إليه بعد الآن.
    try:
        cursor.execute("SELECT webhook_token FROM operators LIMIT 1")
        try:
            cursor.execute("ALTER TABLE operators DROP COLUMN webhook_token")
        except sqlite3.OperationalError:
            pass
    except sqlite3.OperationalError:
        pass

    # جدول قديم من نظام تحويل رسائل SMS (مُستبدَل برفع صورة الإشعار + OCR).
    # يبقى هنا فقط لقواعد البيانات المُرقّاة حتى لا نفقد سجلها التاريخي —
    # لا شيء بالكود يكتب إليه بعد الآن.
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
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_sms_log_operator ON sms_log(operator_id, received_at)")

    # ── سجل إيراد يومي دائم (revenue_log) ───────────────────────────────────
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS revenue_log (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        operator_id  INTEGER NOT NULL,
        occurred_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        amount       REAL NOT NULL
    )
    ''')
    # ملاحظة: تعبئة هذا الجدول من transactions تحتاج operator_id مربوطاً فعلياً
    # بكل عملية — تحصل لاحقاً بعد اكتمال تهيئة المشغّل الافتراضي أدناه، وليس هنا

    # حساب أدمن واحد لكل مشغّل
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS operator_admins (
        id             INTEGER PRIMARY KEY AUTOINCREMENT,
        operator_id    INTEGER NOT NULL UNIQUE,
        username       TEXT NOT NULL,
        password_hash  TEXT NOT NULL,
        FOREIGN KEY(operator_id) REFERENCES operators(id)
    )
    ''')

    # حساب المطوّر (Superadmin) — منفصل تماماً عن حسابات المشغّلين
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS dev_admins (
        id                 INTEGER PRIMARY KEY CHECK (id = 1),
        username           TEXT NOT NULL,
        password_hash      TEXT NOT NULL,
        warning_dismissed  INTEGER DEFAULT 0
    )
    ''')

    # ترقية: عمود لإخفاء تنبيه "كلمة المرور الافتراضية" — بدون هذا كان التنبيه
    # يظهر بشكل ثابت في كل زيارة للوحة المطوّر لحين تغيير كلمة المرور فعلياً،
    try:
        cursor.execute("SELECT warning_dismissed FROM dev_admins LIMIT 1")
    except sqlite3.OperationalError:
        cursor.execute("ALTER TABLE dev_admins ADD COLUMN warning_dismissed INTEGER DEFAULT 0")

    # تنظيف: محرر SQL أُزيل نهائياً من لوحة المطوّر — نحذف عمود كلمة سره
    # القديم لو كان موجوداً بقاعدة بيانات مُرقّاة من نسخة سابقة (يتطلب
    # SQLite 3.35+ لدعم DROP COLUMN؛ الاستثناء يحمي أي نسخة أقدم فقط)
    try:
        cursor.execute("SELECT sql_password_hash FROM dev_admins LIMIT 1")
        try:
            cursor.execute("ALTER TABLE dev_admins DROP COLUMN sql_password_hash")
        except sqlite3.OperationalError:
            pass   # نسخة SQLite قديمة لا تدعم DROP COLUMN — العمود يبقى غير مُستخدَم بالكود بأي حال
    except sqlite3.OperationalError:
        pass   # العمود غير موجود أصلاً (تثبيت جديد)

    # ── تهيئة أول مشغّل افتراضي (أول تشغيل فقط) ─────────────────────────────
    # إذا لم يكن هناك أي مشغّل مسجّل بعد، ننشئ مشغّلاً افتراضياً بحساب أدمن
    existing_operator = cursor.execute("SELECT id FROM operators LIMIT 1").fetchone()
    if not existing_operator:
        cursor.execute(
            "INSERT INTO operators (slug, name, is_active) VALUES (?, ?, 1)",
            ("default", "شبكة market للانترنت")
        )
        default_op_id = cursor.lastrowid

        from werkzeug.security import generate_password_hash
        cursor.execute(
            "INSERT INTO operator_admins (operator_id, username, password_hash) VALUES (?, ?, ?)",
            (default_op_id, "admin", generate_password_hash("2026"))
        )

        # ربط أي بيانات يتيمة (operator_id فارغ) بالمشغّل الافتراضي
        cursor.execute("UPDATE vouchers SET operator_id=? WHERE operator_id IS NULL", (default_op_id,))
        cursor.execute("UPDATE transactions SET operator_id=? WHERE operator_id IS NULL", (default_op_id,))
        cursor.execute("UPDATE audit_log SET operator_id=? WHERE operator_id IS NULL", (default_op_id,))

    # ── تعبئة lifetime_revenue و revenue_log من البيانات القديمة ───────────
    cursor.execute('''
        UPDATE operators SET lifetime_revenue = (
            SELECT COALESCE(SUM(amount_paid), 0) FROM transactions
            WHERE transactions.operator_id = operators.id
        ) WHERE lifetime_revenue = 0
    ''')
    existing_log_rows = cursor.execute("SELECT COUNT(*) FROM revenue_log").fetchone()[0]
    if existing_log_rows == 0:
        existing_txns = cursor.execute(
            "SELECT COUNT(*) FROM transactions WHERE operator_id IS NOT NULL"
        ).fetchone()[0]
        if existing_txns > 0:
            cursor.execute('''
                INSERT INTO revenue_log (operator_id, occurred_at, amount)
                SELECT operator_id, created_at, amount_paid FROM transactions
                WHERE operator_id IS NOT NULL
            ''')

    # ── إنشاء حساب المطوّر الافتراضي (أول تشغيل فقط) ───────────────────────
    dev_existing = cursor.execute("SELECT id FROM dev_admins WHERE id=1").fetchone()
    if not dev_existing:
        from werkzeug.security import generate_password_hash
        cursor.execute(
            "INSERT INTO dev_admins (id, username, password_hash) VALUES (1, ?, ?)",
            ("dev", generate_password_hash("ChangeMe#2026"))
        )

    # فهرس لتسريع ترقيم البطاقات لكل مشغّل على حدة (انظر vouchers-json)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_vouchers_operator ON vouchers(operator_id, id)")

    # فهرس مركّب لاستعلام الحجز الأساسي بالدفع (checkout) الذي يبحث عن بطاقات
    cursor.execute('''
        CREATE INDEX IF NOT EXISTS idx_vouchers_lookup
        ON vouchers(operator_id, status, price, profile, duration_hours)
    ''')

    conn.commit()
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

if __name__ == '__main__':
    init_db_auto()
    app.run(host='0.0.0.0', debug=False, port=5000)
