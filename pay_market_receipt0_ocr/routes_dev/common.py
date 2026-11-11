from flask import Blueprint, request, session, abort
import sqlite3, os

dev_bp = Blueprint('dev', __name__, template_folder='templates')
DATABASE   = 'hotspot_cards.db'
BACKUP_DIR = 'backups'
LOW_STOCK_THRESHOLD = 10

MAX_LOGIN_ATTEMPTS = 5
LOCKOUT_DURATION   = 30
# ملاحظة: حدود محاولات تسجيل الدخول مخزَّنة بجدول rate_limits

# ── تقييد اختياري بعناوين IP للوحة المطوّر بالكامل (بما فيها /dev/login) ────
# لوحة المطوّر تملك أعلى صلاحية بالنظام، ومسارها ثابت
# ومعروف (/dev/login). لو ADMIN عيّن DEV_PANEL_ALLOWED_IPS، ما حدا يقدر
# حتى يوصل لصفحة تسجيل الدخول نفسها من خارج القائمة، بدل ما الحماية تبلش
# بس بعد إدخال كلمة السر. اتركه فاضياً بيئة تطوير محلية، وفعّله بالإنتاج.
DEV_PANEL_ALLOWED_IPS = {ip.strip() for ip in os.environ.get("DEV_PANEL_ALLOWED_IPS", "").split(',') if ip.strip()}


def _dev_panel_ip_allowed():
    if not DEV_PANEL_ALLOWED_IPS:
        return True
    return (request.remote_addr or '') in DEV_PANEL_ALLOWED_IPS


@dev_bp.before_request
def _enforce_dev_panel_ip():
    if not _dev_panel_ip_allowed():
        # 404 بدل 403 حتى لا نُلمّح لوجود لوحة المطوّر أصلاً لمن هو خارج القائمة
        abort(404)


def get_db_connection():
    conn = sqlite3.connect(DATABASE, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def write_dev_audit(action, details=""):
    ip = request.remote_addr or "unknown"
    try:
        conn = get_db_connection()
        conn.execute(
            "INSERT INTO audit_log (actor, action, details, ip_address, operator_id) VALUES (?,?,?,?,NULL)",
            ("dev", action, details, ip)
        )
        conn.commit()
        conn.close()
    except Exception:
        pass


def _require_dev_login():
    return session.get('dev_logged_in') is True
