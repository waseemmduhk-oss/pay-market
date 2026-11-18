import os
import sqlite3
from flask import session, current_app, Blueprint

# تحديد المسار المطلق للمجلد الذي يحتوي على المشروع (pay_market_receipt0_ocr)
BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))

# التأكد من أن مجلد التطبيق موجود وصالح للكتابة
os.makedirs(BASE_DIR, exist_ok=True)

# مسار قاعدة البيانات المطلق الثامن
DATABASE = os.path.join(BASE_DIR, 'hotspot_cards.db')

BACKUP_DIR = os.path.join(BASE_DIR, 'backups')
os.makedirs(BACKUP_DIR, exist_ok=True)

def get_db_connection():
    """فتح اتصال آمن ومضمون مع قاعدة البيانات مع ضمان وجود المسار"""
    os.makedirs(os.path.dirname(DATABASE), exist_ok=True)
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
