import os
import sqlite3
from flask import session, redirect, url_for, current_app

# تحديد المسار المطلق الثابت لضمان اتصال جميع ملفات المشروع بنفس قاعدة البيانات
BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
DATABASE_PATH = os.path.join(BASE_DIR, 'hotspot_cards.db')
DATABASE = os.environ.get('DATABASE_URL', 'hotspot_cards.db')

def get_db_connection():
    """فتح اتصال موحد مع قاعدة البيانات باستخدام المسار المطلق"""
    db_path = os.environ.get('DATABASE_URL', DATABASE_PATH)
    if db_path and db_path.startswith("postgres://"):
        db_path = db_path.replace("postgres://", "postgresql://", 1)
        
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn

# استيراد وتحديد متغيرات الحد الأقصى للمحاولات ومدّة الحظر
MAX_LOGIN_ATTEMPTS = 5
LOCKOUT_DURATION = 300  # 5 دقائق بالثواني

from flask import Blueprint

# تعريف Blueprint الخاصة بالمطورين إذا لم تكن معرفة هنا
dev_bp = Blueprint('dev', __name__)

def _require_dev_login():
    """التحقق من أن جلسة المطور نشطة وصحيحة"""
    if not session.get('dev_logged_in'):
        return False
    return True

def write_dev_audit(action, details=""):
    """تسجيل أحداث التدقيق والأمان الخاصة بالمطور"""
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
