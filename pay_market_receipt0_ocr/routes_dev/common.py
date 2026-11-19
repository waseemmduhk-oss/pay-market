import os
import sqlite3
from flask import session, current_app, Blueprint
from werkzeug.security import generate_password_hash

# تحديد المسار المطلق للمجلد الذي يحتوي على المشروع (pay_market_receipt0_ocr)
BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
DATABASE = os.path.join(BASE_DIR, 'hotspot_cards.db')

BACKUP_DIR = os.path.join(BASE_DIR, 'backups')
os.makedirs(BACKUP_DIR, exist_ok=True)
os.makedirs(os.path.dirname(DATABASE), exist_ok=True)

def init_db_if_not_exists():
    """ضمان إنشاء جميع الجداول وقاعدة البيانات تلقائياً وفوراً عند أي اتصال"""
    conn = sqlite3.connect(DATABASE)
    cursor = conn.cursor()
    try:
        cursor.execute("CREATE TABLE IF NOT EXISTS rate_limits (key TEXT PRIMARY KEY, count INTEGER, window_start REAL, locked_until REAL)")
        cursor.execute("CREATE TABLE IF NOT EXISTS dev_admins (id INTEGER PRIMARY KEY CHECK (id = 1), username TEXT NOT NULL, password_hash TEXT NOT NULL, warning_dismissed INTEGER DEFAULT 0)")
        cursor.execute("CREATE TABLE IF NOT EXISTS operators (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, username TEXT, slug TEXT UNIQUE NOT NULL, balance REAL, lifetime_revenue REAL DEFAULT 0, is_active INTEGER DEFAULT 1, jawwal_pay_number TEXT, support_phone TEXT, hotspot_title TEXT, hotspot_login_url TEXT DEFAULT 'http://00.00.00.00/login', logo_size TEXT DEFAULT 'medium', logo_filename TEXT, telegram_bot_token TEXT, telegram_chat_id TEXT, telegram_webhook_secret TEXT, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)")
        cursor.execute("CREATE TABLE IF NOT EXISTS operator_admins (id INTEGER PRIMARY KEY AUTOINCREMENT, operator_id INTEGER NOT NULL UNIQUE, username TEXT NOT NULL, password_hash TEXT NOT NULL, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, FOREIGN KEY(operator_id) REFERENCES operators(id))")
        cursor.execute("CREATE TABLE IF NOT EXISTS vouchers (id INTEGER PRIMARY KEY AUTOINCREMENT, operator_id INTEGER, username TEXT NOT NULL, password TEXT NOT NULL, code TEXT, price REAL NOT NULL, status TEXT DEFAULT 'available', profile TEXT NOT NULL, duration_hours INTEGER NOT NULL DEFAULT 0, seq INTEGER, FOREIGN KEY(operator_id) REFERENCES operators(id))")
        cursor.execute("CREATE TABLE IF NOT EXISTS pending_orders (id INTEGER PRIMARY KEY AUTOINCREMENT, order_token TEXT UNIQUE NOT NULL, operator_id INTEGER NOT NULL, customer_phone TEXT NOT NULL, voucher_ids TEXT NOT NULL, wallet_code TEXT NOT NULL, base_amount REAL NOT NULL, exact_amount REAL NOT NULL, status TEXT NOT NULL DEFAULT 'pending', receipt_filename TEXT, receipt_hash TEXT, ocr_raw_text TEXT, ocr_parsed_amount REAL, ocr_match_status TEXT, ocr_reference TEXT, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, expires_at REAL NOT NULL, confirmed_at TIMESTAMP, FOREIGN KEY(operator_id) REFERENCES operators(id))")
        cursor.execute("CREATE TABLE IF NOT EXISTS transactions (id INTEGER PRIMARY KEY AUTOINCREMENT, operator_id INTEGER, amount REAL, amount_paid REAL, status TEXT, wallet_code TEXT, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, FOREIGN KEY(operator_id) REFERENCES operators(id))")
        cursor.execute("CREATE TABLE IF NOT EXISTS revenue_log (id INTEGER PRIMARY KEY AUTOINCREMENT, operator_id INTEGER NOT NULL, amount REAL NOT NULL, occurred_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, FOREIGN KEY(operator_id) REFERENCES operators(id))")
        cursor.execute("CREATE TABLE IF NOT EXISTS audit_log (id INTEGER PRIMARY KEY AUTOINCREMENT, operator_id INTEGER, actor TEXT, action TEXT NOT NULL, details TEXT, ip_address TEXT, event_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)")
        cursor.execute("CREATE TABLE IF NOT EXISTS sms_log (id INTEGER PRIMARY KEY AUTOINCREMENT, operator_id INTEGER NOT NULL, raw_message TEXT, parsed_amount REAL, parsed_phone TEXT, matched_order_id INTEGER, match_status TEXT NOT NULL, received_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)")

        # ترقية جدول المعاملات تلقائياً لإضافة الأعمدة المطلوبة في التقارير وصفحة "كروتي"
        tx_cols = {row[1] for row in cursor.execute("PRAGMA table_info(transactions)").fetchall()}
        for col_name, col_def in [
            ("customer_phone", "TEXT"),
            ("voucher_id", "INTEGER"),
            ("hidden_by_customer", "INTEGER DEFAULT 0"),
            ("transaction_id", "TEXT"),
        ]:
            if col_name not in tx_cols:
                cursor.execute(f"ALTER TABLE transactions ADD COLUMN {col_name} {col_def}")
                
        # إنشاء حساب المطور الافتراضي لو لم يكن موجوداً
        dev_existing = cursor.execute("SELECT id FROM dev_admins WHERE id=1").fetchone()
        if not dev_existing:
            cursor.execute("INSERT INTO dev_admins (id, username, password_hash) VALUES (1, ?, ?)", ("dev", generate_password_hash("ChangeMe#2026")))

        conn.commit()
    except Exception as e:
        print("Auto-Init DB Error:", e)
        conn.rollback()
    finally:
        conn.close()

# تنفيذ فحص وإنشاء الجداول فور تحميل ملف common.py
init_db_if_not_exists()

def get_db_connection():
    """فتح اتصال آمن مع قاعدة البيانات مع ضمان وجود الجداول دائماً"""
    init_db_if_not_exists()
    conn = sqlite3.connect(DATABASE)
    conn.row_factory = sqlite3.Row
    return conn

# إعدادات النظام ولوحة المطور
MAX_LOGIN_ATTEMPTS = 5
LOCKOUT_DURATION = 300
LOW_STOCK_THRESHOLD = 10

# Blueprint للمطور
dev_bp = Blueprint('dev', __name__)

def _require_dev_login():
    return session.get('dev_logged_in', False)

def write_dev_audit(action, details=""):
    try:
        conn = get_db_connection()
        ip = current_app.config.get('REMOTE_ADDR') or 'unknown'
        conn.execute('INSERT INTO audit_log (action, details, ip_address) VALUES (?, ?, ?)', (action, details, ip))
        conn.commit()
        conn.close()
    except Exception as e:
        print("Audit Log Error:", e)
