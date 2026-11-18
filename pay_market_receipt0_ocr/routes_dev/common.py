import os
import sqlite3
from flask import session, current_app, Blueprint

# تحديد اسم ملف قاعدة البيانات محلياً وبشكل مضمون داخل المجلد الحالي
DATABASE = os.environ.get('DATABASE_URL', 'hotspot_cards.db')
if DATABASE and DATABASE.startswith("postgres://"):
    DATABASE = DATABASE.replace("postgres://", "postgresql://", 1)

BACKUP_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), '../../backups'))
os.makedirs(BACKUP_DIR, exist_ok=True)

def get_db_connection():
    """فتح اتصال آمن ومحلي مع قاعدة البيانات"""
    conn = sqlite3.connect(DATABASE)
    conn.row_factory = sqlite3.Row
    return conn

# إعدادات النظام ولوحة المطور
MAX_LOGIN_ATTEMPTS = 5
LOCKOUT_DURATION = 300  # 5 دقائق
LOW_STOCK_THRESHOLD = 10

# تعريف Blueprint الخاصة بالمطورين
dev_bp = Blueprint('dev', __name__)

def _require_dev_login():
    """التحقق من أن جلسة المطور نشطة وصحيحة"""
    return session.get('dev_logged_in', False)

def write_dev_audit(action, details=""):
    """تسجيل أحداث التدقيق والأمان للمطور"""
    try:
        conn = get_db_connection()
        ip = current_app.config.get('REMOTE_ADDR') or 'unknown'
        conn.execute(
            'INSERT INTO audit_log (action, details, ip_address) VALUES (?, ?, ?)',
            (action, details, ip)
        )
        conn.commit()
        conn.close()
    except Exception as e:
        print("Audit Log Error:", e)
