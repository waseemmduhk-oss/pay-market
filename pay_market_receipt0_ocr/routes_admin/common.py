from flask import Blueprint, request, session, g, abort
import sqlite3, os
from werkzeug.security import check_password_hash

admin_bp = Blueprint('admin', __name__, template_folder='templates')
DATABASE = 'hotspot_cards.db'

# كلمة المرور الافتراضية اللي تُعطى لأول أدمن لكل مشغّل جديد (انظر app.py
# init_db_auto) — نستخدمها هون فقط للتنبيه لو المشغّل ما غيّرها بعد، وليست
# قيمة تُستخدم للمصادقة الفعلية بأي مكان آخر.
_DEFAULT_OPERATOR_PASSWORD = "2026"

ADMIN_SECRET_URL   = os.environ.get("ADMIN_SECRET_PATH", "login")
# لا يُشارك بين عمليات (workers) متعددة، الآن مخزَّنة بجدول rate_limits
MAX_LOGIN_ATTEMPTS = 5
LOCKOUT_DURATION   = 30      # 30 ثانية

ALLOWED_EXTENSIONS = {'.pdf', '.txt', '.xlsx', '.xls'}
MAX_FILE_SIZE      = 5 * 1024 * 1024    # 5MB
PAGE_SIZE          = 200   # عدد البطاقات في كل صفحة


# ── DB ──────────────────────────────────────────────────────────────────────
def get_db_connection():
    conn = sqlite3.connect(DATABASE, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


# ── تحديد المشغّل الحالي حسب الـ slug في الرابط ──────────────────────────────
def _get_operator_by_slug(slug):
    conn = get_db_connection()
    op = conn.execute('SELECT * FROM operators WHERE slug = ?', (slug,)).fetchone()
    conn.close()
    return op


@admin_bp.before_request
def _resolve_operator():
    """يشتغل قبل كل route فيه <slug> في مساره، يتأكد إن المشغّل موجود ومفعّل."""
    slug = (request.view_args or {}).get('slug')
    if slug is None:
        return
    operator = _get_operator_by_slug(slug)
    if not operator or not operator['is_active']:
        abort(404)
    g.operator = operator


def get_admin_credentials(operator_id):
    conn = get_db_connection()
    row  = conn.execute('SELECT username, password_hash FROM operator_admins WHERE operator_id=?', (operator_id,)).fetchone()
    conn.close()
    return (row['username'], row['password_hash']) if row else (None, None)


def verify_admin_password(operator_id, plain):
    _, pw_hash = get_admin_credentials(operator_id)
    if not pw_hash:
        return False
    return check_password_hash(pw_hash, plain)


def is_using_default_password(operator_id):
    """يرجّع True لو المشغّل لسه على كلمة المرور الافتراضية "2026" —
    تُستخدم فقط لعرض تنبيه أمني بلوحته، نفس فكرة using_default_password
    بلوحة المطوّر."""
    _, pw_hash = get_admin_credentials(operator_id)
    if not pw_hash:
        return False
    return check_password_hash(pw_hash, _DEFAULT_OPERATOR_PASSWORD)


# ── Audit ────────────────────────────────────────────────────────────────────
def write_audit(action, details="", actor="admin", operator_id=None):
    ip = request.remote_addr or "unknown"
    try:
        conn = get_db_connection()
        conn.execute(
            "INSERT INTO audit_log (actor, action, details, ip_address, operator_id) VALUES (?,?,?,?,?)",
            (actor, action, details, ip, operator_id)
        )
        conn.commit()
        conn.close()
    except Exception:
        pass


def _require_admin_login(slug):
    """يرجع True لو الأدمن مسجّل دخول فعلاً لنفس المشغّل المطلوب."""
    return bool(session.get('admin_logged_in') and session.get('operator_slug') == slug)
