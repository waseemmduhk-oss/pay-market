"""
نظام التحقق من الدفع — رفع صورة/PDF لإشعار التحويل + قراءة آلية (OCR)
======================================================================
الفكرة:
  1) العميل يختار البطاقات ← نحجزها فوراً (status='reserved') وننشئ "طلب معلّق"
     pending_orders بالمبلغ الصحيح بالضبط (مجموع أسعار البطاقات، بدون أي
     كسور إضافية للمطابقة).
  2) نعرض للعميل صفحة انتظار فيها: المبلغ المطلوب + رقم جوال باي، وفورم
     لرفع صورة (أو PDF) لإشعار التحويل بعد إتمامه.
  3) عند الرفع: نتحقق من الملف فعلياً (امتداد + حجم + أنه صورة/PDF صالح فعلاً
     لا مجرد ملف بامتداد مطابق)، ثم نحاول قراءة المبلغ منه آلياً:
       - صورة  ← OCR عبر Tesseract (لو متوفر على السيرفر).
       - PDF   ← استخراج نص مباشر (يعمل فقط لو الملف نصياً وليس صورة ممسوحة).
     بما أن الرفع مرتبط أصلاً بطلب واحد محدد (عبر order_token بالرابط)،
     المطابقة هنا 1↔1 (لا حاجة لمطابقة مبلغ بين عدة طلبات كما كان بنظام SMS
     القديم): نقارن فقط المبلغ المقروء آلياً بمبلغ هذا الطلب تحديداً.
       - تطابق تام (بفارق أقل من 0.01) ← تأكيد فوري وتسليم البطاقات.
       - غير ذلك (مبلغ مختلف / لم يُقرأ مبلغ أصلاً / OCR غير متاح على
         السيرفر) ← حالة الطلب تصير 'review' لمراجعة يدوية من المشغّل،
         مع تنبيه تيليجرام اختياري.
     ملاحظة أمان: نحسب بصمة SHA-256 لكل صورة مرفوعة، ولو نفس البصمة استُخدمت
     من قبل بطلب "مؤكَّد" سابق (نفس المشغّل) ← نرفض التأكيد الآلي تلقائياً
     ونحوّلها لمراجعة يدوية فوراً (منع إعادة استخدام إشعار حقيقي سابق للاحتيال
     على طلبات جديدة).
  4) أي طلب لم يتأكد خلال المهلة الزمنية تُفكّ حجوزاته تلقائياً (تعود
     "available") ليُعاد عرضها لعملاء آخرين — يحدث هذا عند أي زيارة لصفحات
     هذا الموديول (تنظيف كسول، بدون Cron/Scheduler إضافي).
  5) للمشغّل خيار تأكيد/إلغاء يدوي من لوحته (routes_admin/payments.py) لكل
     الطلبات المعلّقة أو التي بحاجة مراجعة — يشاهد فيها صورة الإيصال المرفوعة
     والنص الذي قرأه الـOCR منها قبل اتخاذ القرار.
"""
from flask import Blueprint, request, redirect, url_for, current_app, g, abort, jsonify, session
import re, time, secrets, threading, html as html_lib, json, os, io, hashlib, urllib.request, urllib.error
import uuid as _uuid
from db_layer import IS_POSTGRES, get_db_connection as _base_get_db_connection, get_table_columns, add_column_if_missing
import rate_limit

# ── مكتبات اختيارية: التطبيق يجب ألا يتعطّل لو كانت غير مثبّتة على السيرفر —
# ببساطة كل إيصال يصير "قيد المراجعة اليدوية" تلقائياً بدل أي تأكيد آلي ──────
try:
    from PIL import Image
    _PIL_AVAILABLE = True
except ImportError:
    _PIL_AVAILABLE = False
try:
    import pytesseract
    _OCR_AVAILABLE = True
except ImportError:
    _OCR_AVAILABLE = False
try:
    from pypdf import PdfReader
    _PYPDF_AVAILABLE = True
except ImportError:
    _PYPDF_AVAILABLE = False

payment_bp = Blueprint('payment', __name__)

# مهلة صلاحية الطلب المعلّق (بالثواني) — بعدها تُفكّ حجوزات بطاقاته تلقائياً
PAYMENT_ORDER_TTL = 15 * 60   # 15 دقيقة

# حد أقصى لعدد الطلبات المعلّقة (غير مؤكَّدة بعد) لنفس رقم الهاتف بنفس اللحظة
MAX_CONCURRENT_PENDING_PER_PHONE = 2

# ── إعدادات رفع صورة/PDF إشعار التحويل ───────────────────────────────────────
RECEIPT_ALLOWED_EXT = {'.jpg', '.jpeg', '.png', '.webp', '.pdf'}
RECEIPT_MAX_SIZE    = 4 * 1024 * 1024   # 4 ميغابايت
RECEIPTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'receipts')

# رمز سرّي ثابت لحماية مسار التنظيف الداخلي (/internal/cleanup-expired-orders)
_CLEANUP_TOKEN_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'cleanup_token.key')


def _load_or_create_cleanup_token():
    env_token = (os.environ.get("CLEANUP_TOKEN") or "").strip()
    if env_token:
        return env_token
    if os.path.exists(_CLEANUP_TOKEN_FILE):
        with open(_CLEANUP_TOKEN_FILE, 'r', encoding='utf-8') as f:
            existing = f.read().strip()
            if existing:
                return existing
    token = secrets.token_hex(24)
    try:
        with open(_CLEANUP_TOKEN_FILE, 'w', encoding='utf-8') as f:
            f.write(token)
    except OSError:
        pass
    return token


CLEANUP_TOKEN = _load_or_create_cleanup_token()

# نفس الحد الأدنى المستخدم بلوحتي الأدمن والمطوّر
LOW_STOCK_THRESHOLD = 10


def _hours_label(h):
    h = int(h or 0)
    if h <= 0:  return ''
    if h == 720: return 'بطاقة شهرية'   # البطاقة الشهرية = 30 يوم
    if h == 1:  return 'ساعة واحدة'
    if h == 2:  return 'ساعتان'
    if 3 <= h <= 10: return f'{h} ساعات'
    return f'{h} ساعة'


# ── إدارة المعاملات والاتصال المتوافق مع PostgreSQL و SQLite ───────────────
def _tx_begin(conn):
    if not IS_POSTGRES:
        conn.execute("BEGIN EXCLUSIVE")


def _tx_commit(conn):
    conn.commit()


def _tx_rollback(conn):
    try:
        conn.rollback()
    except Exception:
        pass


# ── تنبيه تيليجرام اختياري عند وصول إيصال يحتاج مراجعة يدوية ──────────────
def _notify_telegram_async(bot_token, chat_id, text):
    if not bot_token or not chat_id:
        return

    def _send():
        try:
            url  = f"https://api.telegram.org/bot{bot_token}/sendMessage"
            data = json.dumps({"chat_id": chat_id, "text": text}).encode('utf-8')
            req  = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=8)
        except Exception:
            pass

    threading.Thread(target=_send, daemon=True).start()


def _telegram_multipart_body(fields, file_field_name, file_path, file_mime):
    boundary = _uuid.uuid4().hex
    parts = []
    for key, value in fields.items():
        parts.append(f'--{boundary}'.encode())
        parts.append(f'Content-Disposition: form-data; name="{key}"'.encode())
        parts.append(b'')
        parts.append(str(value).encode('utf-8'))
    with open(file_path, 'rb') as f:
        file_bytes = f.read()
    filename = os.path.basename(file_path)
    parts.append(f'--{boundary}'.encode())
    parts.append(f'Content-Disposition: form-data; name="{file_field_name}"; filename="{filename}"'.encode())
    parts.append(f'Content-Type: {file_mime}'.encode())
    parts.append(b'')
    parts.append(file_bytes)
    parts.append(f'--{boundary}--'.encode())
    parts.append(b'')
    return b'\r\n'.join(parts), f'multipart/form-data; boundary={boundary}'


def _telegram_review_buttons(order_id):
    return {"inline_keyboard": [[
        {"text": "✅ تأكيد الطلب", "callback_data": f"rcpt_ok:{order_id}"},
        {"text": "❌ رفض الطلب",  "callback_data": f"rcpt_no:{order_id}"},
    ]]}


def _notify_telegram_review_async(operator, order_id, caption, file_path=None, is_pdf=False):
    bot_token = operator['telegram_bot_token']
    chat_id   = operator['telegram_chat_id']
    if not bot_token or not chat_id:
        return
    webhook_secret = operator['telegram_webhook_secret']
    reply_markup = _telegram_review_buttons(order_id) if webhook_secret else None

    def _send():
        try:
            if file_path and os.path.exists(file_path):
                if is_pdf:
                    endpoint, field_name, mime = "sendDocument", "document", "application/pdf"
                else:
                    endpoint, field_name, mime = "sendPhoto", "photo", "image/jpeg"
                fields = {"chat_id": chat_id, "caption": caption}
                if reply_markup:
                    fields["reply_markup"] = json.dumps(reply_markup)
                body, content_type = _telegram_multipart_body(fields, field_name, file_path, mime)
                url = f"https://api.telegram.org/bot{bot_token}/{endpoint}"
                req = urllib.request.Request(url, data=body, headers={"Content-Type": content_type})
            else:
                payload = {"chat_id": chat_id, "text": caption}
                if reply_markup:
                    payload["reply_markup"] = json.dumps(reply_markup)
                url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
                req = urllib.request.Request(
                    url, data=json.dumps(payload).encode('utf-8'),
                    headers={"Content-Type": "application/json"}
                )
            urllib.request.urlopen(req, timeout=15)
        except Exception:
            pass

    threading.Thread(target=_send, daemon=True).start()


def _telegram_answer_callback(bot_token, callback_id, text):
    if not bot_token or not callback_id:
        return
    try:
        url  = f"https://api.telegram.org/bot{bot_token}/answerCallbackQuery"
        data = json.dumps({"callback_query_id": callback_id, "text": (text or '')[:190]}).encode('utf-8')
        req  = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=8)
    except Exception:
        pass


def _telegram_edit_message(bot_token, chat_id, message_id, new_text):
    if not bot_token or not chat_id or not message_id:
        return
    for method, field in (('editMessageCaption', 'caption'), ('editMessageText', 'text')):
        try:
            url = f"https://api.telegram.org/bot{bot_token}/{method}"
            payload = {"chat_id": chat_id, "message_id": message_id, field: new_text,
                       "reply_markup": json.dumps({"inline_keyboard": []})}
            req = urllib.request.Request(
                url, data=json.dumps(payload).encode('utf-8'),
                headers={"Content-Type": "application/json"}
            )
            urllib.request.urlopen(req, timeout=8)
            return
        except Exception:
            continue


@payment_bp.route('/telegram/webhook/<webhook_secret>', methods=['POST'])
def telegram_webhook(webhook_secret):
    conn = get_db_connection()
    try:
        operator = conn.execute(
            "SELECT * FROM operators WHERE telegram_webhook_secret=?", (webhook_secret,)
        ).fetchone()
        if not operator or not operator['is_active']:
            abort(404)

        over_limit = rate_limit.hit_sliding_window(conn, f"tg_webhook:{webhook_secret}", 60, 60)
        if over_limit:
            return jsonify({'ok': False}), 429

        update = request.get_json(silent=True) or {}
        cq = update.get('callback_query')
        bot_token = operator['telegram_bot_token']
        operator_id = operator['id']
    finally:
        conn.close()

    if not cq or 'data' not in cq:
        return jsonify({'ok': True})

    callback_id = cq.get('id')
    message     = cq.get('message') or {}
    chat_id     = (message.get('chat') or {}).get('id')
    message_id  = message.get('message_id')

    parts = (cq.get('data') or '').split(':')
    action   = parts[0] if parts else ''
    order_id = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else None

    if action not in ('rcpt_ok', 'rcpt_no') or order_id is None:
        _telegram_answer_callback(bot_token, callback_id, "إجراء غير معروف")
        return jsonify({'ok': True})

    result_text = None
    with _pay_lock:
        conn2 = get_db_connection()
        try:
            _tx_begin(conn2)
            order = conn2.execute(
                "SELECT * FROM pending_orders WHERE id=? AND operator_id=? AND status IN ('pending','review')",
                (order_id, operator_id)
            ).fetchone()
            if not order:
                result_text = f"⚠️ الطلب #{order_id} لم يعد بانتظار مراجعة (رُبما اتُخذ إجراء بشأنه مسبقاً)."
            elif action == 'rcpt_ok':
                _confirm_order(conn2, order)
                result_text = f"✅ تم تأكيد الطلب #{order_id} عبر تيليجرام."
            else:
                _cancel_order(conn2, order)
                result_text = f"❌ تم رفض الطلب #{order_id} عبر تيليجرام، وفُكّ حجز بطاقاته."
            _tx_commit(conn2)
        except Exception:
            _tx_rollback(conn2)
            result_text = "⚠️ تعذّر تنفيذ الإجراء — راجع الطلب من لوحة الأدمن."
        finally:
            conn2.close()

    _telegram_answer_callback(bot_token, callback_id, result_text)
    _telegram_edit_message(bot_token, chat_id, message_id, result_text)
    return jsonify({'ok': True})


CHECKOUT_LIMIT  = 3
CHECKOUT_WINDOW = 60

_pay_lock = threading.Lock()
_account_col_ready = False


def _ensure_account_phone_column(conn):
    global _account_col_ready
    if _account_col_ready:
        return
    try:
        cols = get_table_columns(conn, 'pending_orders')
        if cols and 'account_phone' not in cols:
            add_column_if_missing(conn, 'pending_orders', 'account_phone', 'TEXT')
            conn.commit()
    except Exception:
        _tx_rollback(conn)
    _account_col_ready = True


def get_db_connection():
    conn = _base_get_db_connection()
    if not IS_POSTGRES:
        try:
            conn.execute("PRAGMA journal_mode=WAL")
        except Exception:
            pass
    _ensure_account_phone_column(conn)
    return conn


def _get_operator_by_slug(slug):
    conn = get_db_connection()
    try:
        return conn.execute('SELECT * FROM operators WHERE slug = ?', (slug,)).fetchone()
    finally:
        conn.close()


@payment_bp.before_request
def _resolve_operator():
    slug = (request.view_args or {}).get('slug')
    if slug is None:
        return
    operator = _get_operator_by_slug(slug)
    if not operator or not operator['is_active']:
        abort(404)
    g.operator = operator


def _toast_page(icon, icon_color, icon_bg, title, message, btn_label, btn_href, btn_color):
    return (
        '<!DOCTYPE html>\n<html lang="ar" dir="rtl">\n<head>\n'
        '<meta charset="UTF-8">\n<meta name="viewport" content="width=device-width, initial-scale=1.0">\n'
        f'<title>{title}</title>\n'
        '<script src="https://cdn.tailwindcss.com"></script>\n'
        '<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css">\n'
        "<style>@import url('https://fonts.googleapis.com/css2?family=Cairo:wght@400;600;700;800&display=swap'); body{font-family:'Cairo',sans-serif;}</style>\n"
        '</head>\n<body class="bg-slate-50 min-h-screen flex items-center justify-center p-4">\n'
        '<div class="bg-white rounded-3xl shadow-xl border border-slate-100 max-w-sm w-full p-8 text-center">\n'
        f'<div class="w-20 h-20 {icon_bg} rounded-2xl mx-auto mb-5 flex items-center justify-center">\n'
        f'<i class="fa-solid {icon} {icon_color} text-4xl"></i>\n</div>\n'
        f'<h2 class="text-xl font-extrabold text-slate-800 mb-2">{title}</h2>\n'
        f'<p class="text-sm text-slate-500 leading-relaxed mb-7">{message}</p>\n'
        f'<a href="{btn_href}" class="block w-full bg-gradient-to-r {btn_color} text-white font-bold py-3.5 px-4 rounded-xl shadow-md text-sm">\n'
        f'{btn_label}\n</a>\n</div>\n</body>\n</html>'
    )


def _expire_stale_orders(conn, operator_id):
    now = time.time()
    stale = conn.execute(
        "SELECT id, voucher_ids FROM pending_orders WHERE operator_id=? AND status='pending' AND expires_at < ?",
        (operator_id, now)
    ).fetchall()
    for row in stale:
        voucher_ids = json.loads(row['voucher_ids'])
        if voucher_ids:
            ph = ','.join('?' * len(voucher_ids))
            conn.execute(
                f"UPDATE vouchers SET status='available' WHERE id IN ({ph}) AND status='reserved'",
                voucher_ids
            )
        conn.execute("UPDATE pending_orders SET status='expired' WHERE id=?", (row['id'],))
    if stale:
        conn.commit()


@payment_bp.route('/shop/<slug>/checkout', methods=['POST'])
def checkout(slug):
    operator    = g.operator
    operator_id = operator['id']
    ip = request.remote_addr or 'unknown'

    if not (session.get('customer_logged_in') and session.get('customer_phone')):
        return redirect(url_for('user.index', slug=slug))

    current_app.validate_csrf()

    rl_conn = get_db_connection()
    try:
        over_limit = rate_limit.hit_sliding_window(rl_conn, f"checkout:{ip}", CHECKOUT_LIMIT, CHECKOUT_WINDOW)
    finally:
        rl_conn.close()
    if over_limit:
        return _toast_page("fa-ban", "text-rose-400", "bg-rose-50",
                           "طلبات كثيرة جداً",
                           "تجاوزت الحد المسموح به. انتظر دقيقة ثم حاول مجدداً.",
                           "العودة للرئيسية", url_for('user.index', slug=slug), "from-rose-500 to-purple-600")

    price_list_raw    = request.form.getlist('price[]')
    profile_list_raw  = request.form.getlist('profile[]')
    hours_list_raw    = request.form.getlist('hours[]')
    quantity_list_raw = request.form.getlist('quantity[]')

    account_phone = session['customer_phone']
    phone = (request.form.get('phone') or '').strip() or account_phone

    if not re.fullmatch(r'059\d{7}', phone):
        return _toast_page("fa-mobile-screen-button", "text-rose-400", "bg-rose-50",
                           "رقم هاتف غير صحيح",
                           "يجب أن يبدأ الرقم بـ 059 ويتكوّن من 10 أرقام.",
                           "العودة للرئيسية", url_for('user.index', slug=slug), "from-rose-500 to-purple-600")

    _phone_conn = get_db_connection()
    try:
        _expire_stale_orders(_phone_conn, operator_id)
        existing_pending = _phone_conn.execute(
            "SELECT COUNT(*) FROM pending_orders WHERE operator_id=? AND COALESCE(account_phone, customer_phone)=? AND status='pending'",
            (operator_id, account_phone)
        ).fetchone()[0]
    finally:
        _phone_conn.close()

    if existing_pending >= MAX_CONCURRENT_PENDING_PER_PHONE:
        return _toast_page("fa-hourglass-half", "text-orange-400", "bg-orange-50",
                           "لديك طلب دفع قيد الانتظار",
                           "أكمل دفع طلبك السابق أو ألغِه أولاً قبل بدء طلب جديد بنفس الرقم.",
                           "العودة للرئيسية", url_for('user.index', slug=slug), "from-indigo-500 to-purple-600")

    if not price_list_raw or not (len(price_list_raw) == len(quantity_list_raw) == len(profile_list_raw) == len(hours_list_raw)):
        return _toast_page("fa-triangle-exclamation", "text-amber-400", "bg-amber-50",
                           "بيانات غير صحيحة",
                           "تأكد من اختيار فئة الباقة والعدد بشكل صحيح لكل صنف.",
                           "العودة للرئيسية", url_for('user.index', slug=slug), "from-indigo-500 to-purple-600")

    items = {}
    _price_conn = get_db_connection()
    try:
        ALLOWED_CATEGORIES = {
            (round(float(row['price']), 2), row['profile'] or '', int(row['duration_hours'] or 0))
            for row in _price_conn.execute(
                "SELECT DISTINCT price, profile, duration_hours FROM vouchers WHERE status = 'available' AND operator_id = ?",
                (operator_id,)
            ).fetchall()
        }
    finally:
        _price_conn.close()

    try:
        for price_str, profile_str, hours_str, qty_str in zip(price_list_raw, profile_list_raw, hours_list_raw, quantity_list_raw):
            price = round(float(price_str), 2)
            profile = profile_str.strip()
            hours = int(hours_str)
            qty   = int(qty_str)
            cat = (price, profile, hours)
            if cat not in ALLOWED_CATEGORIES:
                raise ValueError("فئة غير مسموحة")
            if qty < 1 or qty > 100:
                raise ValueError
            items[cat] = items.get(cat, 0) + qty
            if items[cat] > 100:
                raise ValueError
    except (ValueError, TypeError):
        return _toast_page("fa-
