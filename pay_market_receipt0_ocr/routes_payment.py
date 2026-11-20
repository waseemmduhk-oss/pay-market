"""
نظام التحقق من الدفع — رفع صورة/PDF لإشعار التحويل + قراءة آلية (OCR)
"""
from flask import Blueprint, request, redirect, url_for, current_app, g, abort, jsonify, session
import re, time, secrets, threading, html as html_lib, json, os, io, hashlib, urllib.request
import uuid as _uuid
from db_layer import (
    IS_POSTGRES,
    get_db_connection as _base_get_db_connection,
    get_table_columns,
    add_column_if_missing,
)
import rate_limit

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

PAYMENT_ORDER_TTL = 15 * 60
MAX_CONCURRENT_PENDING_PER_PHONE = 2

RECEIPT_ALLOWED_EXT = {'.jpg', '.jpeg', '.png', '.webp', '.pdf'}
RECEIPT_MAX_SIZE = 4 * 1024 * 1024
RECEIPTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'receipts')

_CLEANUP_TOKEN_FILE = 'cleanup_token.key'


def _load_or_create_cleanup_token():
    if os.environ.get("CLEANUP_TOKEN"):
        return os.environ["CLEANUP_TOKEN"]
    if os.path.exists(_CLEANUP_TOKEN_FILE):
        with open(_CLEANUP_TOKEN_FILE, 'r', encoding='utf-8') as f:
            return f.read().strip()
    token = secrets.token_hex(24)
    try:
        with open(_CLEANUP_TOKEN_FILE, 'w', encoding='utf-8') as f:
            f.write(token)
    except OSError:
        pass
    return token


CLEANUP_TOKEN = _load_or_create_cleanup_token()
LOW_STOCK_THRESHOLD = 10
CHECKOUT_LIMIT = 3
CHECKOUT_WINDOW = 60

_pay_lock = threading.Lock()
_account_col_ready = False


def _hours_label(h):
    h = int(h or 0)
    if h <= 0:
        return ''
    if h == 720:
        return 'بطاقة شهرية'
    if h == 1:
        return 'ساعة واحدة'
    if h == 2:
        return 'ساعتان'
    if 3 <= h <= 10:
        return f'{h} ساعات'
    return f'{h} ساعة'


def _ensure_account_phone_column(conn):
    global _account_col_ready
    if _account_col_ready:
        return
    try:
        cols = get_table_columns(conn, 'pending_orders')
        if cols and 'account_phone' not in cols:
            add_column_if_missing(conn, 'pending_orders', 'account_phone', 'TEXT')
            conn.commit()
        _account_col_ready = True
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass


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


def _notify_telegram_async(bot_token, chat_id, text):
    if not bot_token or not chat_id:
        return

    def _send():
        try:
            url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
            data = json.dumps({"chat_id": chat_id, "text": text}).encode('utf-8')
            req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
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
        {"text": "❌ رفض الطلب", "callback_data": f"rcpt_no:{order_id}"},
    ]]}


def _notify_telegram_review_async(operator, order_id, caption, file_path=None, is_pdf=False):
    bot_token = operator['telegram_bot_token']
    chat_id = operator['telegram_chat_id']
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
        url = f"https://api.telegram.org/bot{bot_token}/answerCallbackQuery"
        data = json.dumps({"callback_query_id": callback_id, "text": (text or '')[:190]}).encode('utf-8')
        req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=8)
    except Exception:
        pass


def _telegram_edit_message(bot_token, chat_id, message_id, new_text):
    if not bot_token or not chat_id or not message_id:
        return
    for method, field in (('editMessageCaption', 'caption'), ('editMessageText', 'text')):
        try:
            url = f"https://api.telegram.org/bot{bot_token}/{method}"
            payload = {
                "chat_id": chat_id,
                "message_id": message_id,
                field: new_text,
                "reply_markup": json.dumps({"inline_keyboard": []}),
            }
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
            "SELECT * FROM operators WHERE telegram_webhook_secret=? AND is_active=?",
            (webhook_secret, True if IS_POSTGRES else 1)
        ).fetchone()
        if not operator:
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
    message = cq.get('message') or {}
    chat_id = (message.get('chat') or {}).get('id')
    message_id = message.get('message_id')

    parts = (cq.get('data') or '').split(':')
    action = parts[0] if parts else ''
    order_id = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else None

    if action not in ('rcpt_ok', 'rcpt_no') or order_id is None:
        _telegram_answer_callback(bot_token, callback_id, "إجراء غير معروف")
        return jsonify({'ok': True})

    result_text = None
    with _pay_lock:
        conn2 = get_db_connection()
        try:
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
            conn2.commit()
        except Exception:
            conn2.rollback()
            result_text = "⚠️ تعذّر تنفيذ الإجراء — راجع الطلب من لوحة الأدمن."
        finally:
            conn2.close()

    _telegram_answer_callback(bot_token, callback_id, result_text)
    _telegram_edit_message(bot_token, chat_id, message_id, result_text)
    return jsonify({'ok': True})


def _toast_page(icon, icon_color, icon_bg, title, message, btn_label, btn_href, btn_color):
    return f"""<!DOCTYPE html>
<html lang="ar" dir="rtl">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{title}</title>
    <script src="https://cdn.tailwindcss.com"></script>
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css">
    <style>@import url('https://fonts.googleapis.com/css2?family=Cairo:wght@400;600;700;800&display=swap'); body{{font-family:'Cairo',sans-serif;}}</style>
</head>
<body class="bg-slate-50 min-h-screen flex items-center justify-center p-4">
    <div class="bg-white rounded-3xl shadow-xl border border-slate-100 max-w-sm w-full p-8 text-center">
        <div class="w-20 h-20 {icon_bg} rounded-2xl mx-auto mb-5 flex items-center justify-center">
            <i class="fa-solid {icon} {icon_color} text-4xl"></i>
        </div>
        <h2 class="text-xl font-extrabold text-slate-800 mb-2">{title}</h2>
        <p class="text-sm text-slate-500 leading-relaxed mb-7">{message}</p>
        <a href="{btn_href}" class="block w-full bg-gradient-to-r {btn_color} text-white font-bold py-3.5 px-4 rounded-xl shadow-md text-sm">
            {btn_label}
        </a>
    </div>
</body>
</html>"""


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
    operator = g.operator
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
        return _toast_page(
            "fa-ban", "text-rose-400", "bg-rose-50",
            "طلبات كثيرة جداً",
            "تجاوزت الحد المسموح به. انتظر دقيقة ثم حاول مجدداً.",
            "العودة للرئيسية", url_for('user.index', slug=slug), "from-rose-500 to-purple-600"
        )

    price_list_raw = request.form.getlist('price[]')
    profile_list_raw = request.form.getlist('profile[]')
    hours_list_raw = request.form.getlist('hours[]')
    quantity_list_raw = request.form.getlist('quantity[]')

    account_phone = session['customer_phone']
    phone = (request.form.get('phone') or '').strip() or account_phone

    if not re.fullmatch(r'059\d{7}', phone):
        return _toast_page(
            "fa-mobile-screen-button", "text-rose-400", "bg-rose-50",
            "رقم هاتف غير صحيح",
            "يجب أن يبدأ الرقم بـ 059 ويتكوّن من 10 أرقام.",
            "العودة للرئيسية", url_for('user.index', slug=slug), "from-rose-500 to-purple-600"
        )

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
        return _toast_page(
            "fa-hourglass-half", "text-orange-400", "bg-orange-50",
            "لديك طلب دفع قيد الانتظار",
            "أكمل دفع طلبك السابق أو ألغِه أولاً قبل بدء طلب جديد بنفس الرقم.",
            "العودة للرئيسية", url_for('user.index', slug=slug), "from-indigo-500 to-purple-600"
        )

    if not price_list_raw or not (len(price_list_raw) == len(quantity_list_raw) == len(profile_list_raw) == len(hours_list_raw)):
        return _toast_page(
            "fa-triangle-exclamation", "text-amber-400", "bg-amber-50",
            "بيانات غير صحيحة",
            "تأكد من اختيار فئة الباقة والعدد بشكل صحيح لكل صنف.",
            "العودة للرئيسية", url_for('user.index', slug=slug), "from-indigo-500 to-purple-600"
        )

    items = {}
    _price_conn = get_db_connection()
    try:
        allowed_categories = {
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
            qty = int(qty_str)
            cat = (price, profile, hours)
            if cat not in allowed_categories or qty < 1 or qty > 100:
                raise ValueError("بيانات غير صحيحة")
            items[cat] = items.get(cat, 0) + qty
            if items[cat] > 100:
                raise ValueError("تجاوز الحد المسموح")
    except (ValueError, TypeError):
        return _toast_page(
            "fa-triangle-exclamation", "text-amber-400", "bg-amber-50",
            "بيانات غير صحيحة",
            "تأكد من اختيار فئة الباقة والعدد بشكل صحيح لكل صنف.",
            "العودة للرئيسية", url_for('user.index', slug=slug), "from-indigo-500 to-purple-600"
        )

    safe_chars = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
    wallet_code = ''.join(secrets.choice(safe_chars) for _ in range(6))
    order_token = secrets.token_urlsafe(24)

    with _pay_lock:
        conn = get_db_connection()
        try:
            _expire_stale_orders(conn, operator_id)
            reserved_ids = []
            total_base_amount = 0.0
            for (price, profile, hours), qty in items.items():
                vouchers = conn.execute(
                    "SELECT id FROM vouchers WHERE price = ? AND profile = ? AND duration_hours = ? AND status = 'available' AND operator_id = ? LIMIT ?",
                    (price, profile, hours, operator_id, qty)
                ).fetchall()
                if len(vouchers) < qty:
                    conn.rollback()
                    available = len(vouchers)
                    price_label = int(price) if price == int(price) else price
                    cat_label = f"{_hours_label(hours)} / {profile}" if hours else profile
                    msg = (
                        f"المتاح حالياً {available} بطاقة فقط من فئة {cat_label} ({price_label} شيكل)."
                        if available > 0 else f"نفدت بطاقات فئة {cat_label} ({price_label} شيكل) تماماً."
                    )
                    return _toast_page(
                        "fa-box-open", "text-orange-400", "bg-orange-50",
                        "المخزون غير كافٍ", msg,
                        "العودة للرئيسية", url_for('user.index', slug=slug), "from-indigo-500 to-purple-600"
                    )
                for v in vouchers:
                    reserved_ids.append(v['id'])
                total_base_amount += price * qty

            ph = ','.join('?' * len(reserved_ids))
            conn.execute(f"UPDATE vouchers SET status='reserved' WHERE id IN ({ph})", reserved_ids)

            exact_amount = round(total_base_amount, 2)
            expires_at = time.time() + PAYMENT_ORDER_TTL
            conn.execute(
                """
                INSERT INTO pending_orders
                    (order_token, operator_id, customer_phone, account_phone, voucher_ids, wallet_code,
                     base_amount, exact_amount, status, expires_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)
                """,
                (order_token, operator_id, phone, account_phone, json.dumps(reserved_ids), wallet_code,
                 total_base_amount, exact_amount, expires_at)
            )
            conn.commit()
        except Exception:
            conn.rollback()
            return _toast_page(
                "fa-circle-exclamation", "text-rose-400", "bg-rose-50",
                "خطأ في المعالجة", "حدث خطأ غير متوقع. يرجى المحاولة مجدداً.",
                "العودة للرئيسية", url_for('user.index', slug=slug), "from-rose-500 to-purple-600"
            )
        finally:
            conn.close()

    return redirect(url_for('payment.pay_page', slug=slug, order_token=order_token))


@payment_bp.route('/shop/<slug>/pay/<order_token>')
def pay_page(slug, order_token):
    operator = g.operator
    operator_id = operator['id']
    conn = get_db_connection()
    try:
        _expire_stale_orders(conn, operator_id)
        order = conn.execute(
            "SELECT * FROM pending_orders WHERE order_token=? AND operator_id=?",
            (order_token, operator_id)
        ).fetchone()
    finally:
        conn.close()

    if not order:
        return _toast_page(
            "fa-circle-question", "text-slate-400", "bg-slate-100",
            "طلب غير موجود", "لم نجد هذا الطلب أو انتهت صلاحيته.",
            "العودة للرئيسية", url_for('user.index', slug=slug), "from-indigo-500 to-purple-600"
        )

    if order['status'] == 'confirmed':
        return redirect(url_for('payment.receipt', slug=slug, order_token=order_token))

    if order['status'] in ('expired', 'cancelled'):
        return _toast_page(
            "fa-hourglass-end", "text-orange-400", "bg-orange-50",
            "انتهت صلاحية الطلب",
            "لم يصلنا تأكيد الدفع خلال المهلة المحددة. يمكنك المحاولة من جديد.",
            "طلب جديد", url_for('user.index', slug=slug), "from-indigo-500 to-purple-600"
        )

    jawwal_number = html_lib.escape((operator['jawwal_pay_number'] or '').strip())
    support_phone = html_lib.escape((operator['support_phone'] or '').strip())
    exact_amount = f"{float(order['exact_amount']):.2f}"
    remaining = max(0, int(float(order['expires_at']) - time.time()))
    status_url = url_for('payment.order_status', slug=slug, order_token=order_token)
    cancel_url = url_for('payment.cancel_order', slug=slug, order_token=order_token)
    upload_url = url_for('payment.upload_receipt', slug=slug, order_token=order_token)
    csrf_token = current_app.generate_csrf_token()

    pay_message = session.pop('pay_message', None)
    msg_html = ''
    if pay_message:
        kind, text = pay_message
        color = {'success': 'emerald', 'error': 'rose', 'info': 'indigo'}.get(kind, 'slate')
        icon = {'success': 'fa-circle-check', 'error': 'fa-circle-exclamation', 'info': 'fa-circle-info'}.get(kind, 'fa-info')
        msg_html = f"""<div class="mb-4 bg-{color}-50 border border-{color}-100 text-{color}-700 text-xs font-bold px-4 py-3 rounded-xl flex items-center gap-2 text-right">
            <i class="fa-solid {icon}"></i><span>{html_lib.escape(text)}</span>
        </div>"""

    if order['status'] == 'review':
        status_box_icon = 'fa-magnifying-glass'
        status_box_color = 'text-orange-500'
        status_box_text = 'الإشعار قيد المراجعة اليدوية من الشبكة...'
    else:
        status_box_icon = 'fa-circle-notch fa-spin'
        status_box_color = 'text-indigo-500'
        status_box_text = 'بانتظار رفع إشعار التحويل...'

    upload_label = 'ارفع صورة أوضح لإشعار التحويل (اختياري)' if order['status'] == 'review' else 'ارفع صورة أو PDF لإشعار التحويل بعد إتمامه'

    return f"""<!DOCTYPE html>
<html lang="ar" dir="rtl">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>إتمام الدفع</title>
<script src="https://cdn.tailwindcss.com"></script>
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css">
<style>@import url('https://fonts.googleapis.com/css2?family=Cairo:wght@400;600;700;800&display=swap'); body{{font-family:'Cairo',sans-serif;}}</style>
</head>
<body class="bg-slate-50 min-h-screen flex items-center justify-center p-4">
<div class="bg-white rounded-3xl shadow-xl border border-slate-100 max-w-md w-full p-6 sm:p-8 text-center">
    <h2 class="text-xl font-extrabold text-slate-800 mb-1">حوّل عبر جوال باي وارفع الإشعار</h2>
    <p class="text-xs text-slate-500 mb-6">سيتم تسليم بطاقاتك تلقائياً فور التحقق من الإشعار</p>
    {msg_html}
    <div class="bg-indigo-50 border-2 border-dashed border-indigo-300 rounded-2xl p-5 mb-4">
        <p class="text-[11px] font-bold text-indigo-500 mb-1">المبلغ المطلوب بالضبط</p>
        <div class="text-4xl font-extrabold text-indigo-700 tracking-wide" dir="ltr">{exact_amount} <span class="text-lg">₪</span></div>
    </div>
    <div class="bg-slate-50 rounded-xl p-4 mb-4 text-right">
        <p class="text-[11px] font-bold text-slate-400 mb-1">رقم جوال باي المستلم</p>
        <span class="text-lg font-mono font-extrabold text-slate-800 tracking-widest" dir="ltr">{jawwal_number or 'غير محدد بعد'}</span>
    </div>
    <form method="POST" action="{upload_url}" enctype="multipart/form-data" class="mb-4 text-right">
        <input type="hidden" name="_csrf_token" value="{csrf_token}">
        <label class="block text-[11px] font-bold text-slate-500 mb-1.5">{upload_label}</label>
        <input type="file" name="receipt" accept="image/jpeg,image/png,image/webp,application/pdf" required class="block w-full text-xs mb-3">
        <button type="submit" class="w-full bg-indigo-600 hover:bg-indigo-700 text-white font-bold py-3 rounded-xl text-sm">رفع الإشعار</button>
    </form>
    <div id="statusBox" class="flex items-center justify-center gap-2 {status_box_color} font-bold text-sm mb-4">
        <i class="fa-solid {status_box_icon}"></i>
        <span>{status_box_text}</span>
    </div>
    <p class="text-[11px] text-slate-400 mb-4">تنتهي صلاحية الطلب خلال <span id="countdown" class="font-mono font-bold text-slate-600">--:--</span> — الدعم: {support_phone or 'غير محدد'}</p>
    <form method="POST" action="{cancel_url}">
        <input type="hidden" name="_csrf_token" value="{csrf_token}">
        <button type="submit" class="w-full bg-slate-100 hover:bg-slate-200 text-slate-600 font-bold py-3 rounded-xl text-sm">إلغاء الطلب</button>
    </form>
</div>
<script>
let remaining = {remaining};
function tick() {{
    if (remaining <= 0) {{ document.getElementById('countdown').textContent = '00:00'; return; }}
    const m = String(Math.floor(remaining / 60)).padStart(2, '0');
    const s = String(remaining % 60).padStart(2, '0');
    document.getElementById('countdown').textContent = m + ':' + s;
    remaining--;
}}
tick(); setInterval(tick, 1000);
setInterval(async () => {{
    try {{
        const res = await fetch("{status_url}");
        const data = await res.json();
        if (data.status === 'confirmed') window.location.href = data.redirect;
        else if (data.status === 'expired' || data.status === 'cancelled') window.location.reload();
    }} catch (e) {{}}
}}, 4000);
</script>
</body>
</html>"""


@payment_bp.route('/shop/<slug>/pay/<order_token>/status')
def order_status(slug, order_token):
    operator_id = g.operator['id']
    conn = get_db_connection()
    try:
        _expire_stale_orders(conn, operator_id)
        order = conn.execute(
            "SELECT status FROM pending_orders WHERE order_token=? AND operator_id=?",
            (order_token, operator_id)
        ).fetchone()
    finally:
        conn.close()
    if not order:
        return jsonify({'status': 'not_found'})
    payload = {'status': order['status']}
    if order['status'] == 'confirmed':
        payload['redirect'] = url_for('payment.receipt', slug=slug, order_token=order_token)
    return jsonify(payload)


@payment_bp.route('/shop/<slug>/pay/<order_token>/cancel', methods=['POST'])
def cancel_order(slug, order_token):
    current_app.validate_csrf()
    operator_id = g.operator['id']
    with _pay_lock:
        conn = get_db_connection()
        try:
            order = conn.execute(
                "SELECT * FROM pending_orders WHERE order_token=? AND operator_id=? AND status='pending'",
                (order_token, operator_id)
            ).fetchone()
            if order:
                _cancel_order(conn, order)
            conn.commit()
        except Exception:
            conn.rollback()
        finally:
            conn.close()
    return redirect(url_for('user.index', slug=slug))


@payment_bp.route('/shop/<slug>/pay/<order_token>/receipt')
def receipt(slug, order_token):
    operator_id = g.operator['id']
    conn = get_db_connection()
    try:
        order = conn.execute(
            "SELECT * FROM pending_orders WHERE order_token=? AND operator_id=? AND status='confirmed'",
            (order_token, operator_id)
        ).fetchone()
        if not order:
            return _toast_page(
                "fa-circle-question", "text-slate-400", "bg-slate-100",
                "طلب غير موجود", "لم نجد إيصالاً مؤكداً لهذا الطلب.",
                "العودة للرئيسية", url_for('user.index', slug=slug), "from-indigo-500 to-purple-600"
            )
        voucher_ids = json.loads(order['voucher_ids'])
        ph = ','.join('?' * len(voucher_ids))
        cards = conn.execute(
            f"SELECT username, password, price, profile, duration_hours FROM vouchers WHERE id IN ({ph})",
            voucher_ids
        ).fetchall()
    finally:
        conn.close()

    session['recent_bought_codes'] = [
        {
            'username': c['username'],
            'password': c['password'],
            'price': c['price'],
            'profile': c['profile'],
            'duration_hours': c['duration_hours'],
            'customer_phone': order['customer_phone'],
        }
        for c in cards
    ]
    return redirect(url_for('user.index', slug=slug))


_RECEIPT_AMOUNT_RE = re.compile(
    r'(?:ils|nis)\s*(\d+(?:[.,]\d{1,2})?)|(\d+(?:[.,]\d{1,2})?)\s*(?:شيكل|شواقل|₪|nis|ils)',
    re.IGNORECASE
)
_RECEIPT_REF_RE = re.compile(r'\b(\d{12,16})\b')


def _extract_amounts_from_text(text):
    if not text:
        return []
    out = []
    for m in _RECEIPT_AMOUNT_RE.finditer(text):
        raw = m.group(1) or m.group(2)
        try:
            out.append(round(float(raw.replace(',', '.')), 2))
        except ValueError:
            continue
    return out


def _extract_receipt_reference(text):
    if not text:
        return None
    m = _RECEIPT_REF_RE.search(text)
    return m.group(1) if m else None


def _ocr_image(path):
    if not _OCR_AVAILABLE or not _PIL_AVAILABLE:
        return ''
    try:
        img = Image.open(path)
        return pytesseract.image_to_string(img, lang='eng+ara') or ''
    except Exception:
        return ''


def _extract_text_from_pdf(path):
    if not _PYPDF_AVAILABLE:
        return ''
    try:
        reader = PdfReader(path)
        return '\n'.join((page.extract_text() or '') for page in reader.pages[:3])
    except Exception:
        return ''


@payment_bp.route('/shop/<slug>/pay/<order_token>/upload-receipt', methods=['POST'])
def upload_receipt(slug, order_token):
    operator = g.operator
    operator_id = operator['id']
    current_app.validate_csrf()

    conn = get_db_connection()
    try:
        _expire_stale_orders(conn, operator_id)
        order = conn.execute(
            "SELECT * FROM pending_orders WHERE order_token=? AND operator_id=? AND status IN ('pending','review')",
            (order_token, operator_id)
        ).fetchone()
        if not order:
            return redirect(url_for('payment.pay_page', slug=slug, order_token=order_token))

        over_limit = rate_limit.hit_sliding_window(conn, f"upload_receipt:{order_token}", 5, 300)
        if over_limit:
            session['pay_message'] = ('error', 'محاولات رفع كثيرة جداً لنفس الطلب. انتظر قليلاً وحاول مجدداً.')
            return redirect(url_for('payment.pay_page', slug=slug, order_token=order_token))

        f = request.files.get('receipt')
        if not f or not f.filename:
            session['pay_message'] = ('error', 'لم يتم اختيار أي صورة أو ملف.')
            return redirect(url_for('payment.pay_page', slug=slug, order_token=order_token))

        ext = os.path.splitext(f.filename)[1].lower()
        if ext not in RECEIPT_ALLOWED_EXT:
            session['pay_message'] = ('error', 'صيغة غير مدعومة. المسموح: JPG, PNG, WEBP, PDF فقط.')
            return redirect(url_for('payment.pay_page', slug=slug, order_token=order_token))

        f.seek(0, os.SEEK_END)
        size = f.tell()
        f.seek(0)
        if size == 0 or size > RECEIPT_MAX_SIZE:
            session['pay_message'] = ('error', 'حجم الملف غير صالح (الحد الأقصى 4 ميغابايت).')
            return redirect(url_for('payment.pay_page', slug=slug, order_token=order_token))

        raw = f.read()
        receipt_hash = hashlib.sha256(raw).hexdigest()
        is_pdf = (ext == '.pdf')

        if is_pdf:
            if raw[:5] != b'%PDF-':
                session['pay_message'] = ('error', 'الملف ليس PDF صالحاً.')
                return redirect(url_for('payment.pay_page', slug=slug, order_token=order_token))
        else:
            if not _PIL_AVAILABLE:
                session['pay_message'] = ('error', 'تعذّر التحقق من الصورة على السيرفر حالياً.')
                return redirect(url_for('payment.pay_page', slug=slug, order_token=order_token))
            try:
                Image.open(io.BytesIO(raw)).verify()
            except Exception:
                session['pay_message'] = ('error', 'الملف ليس صورة صالحة.')
                return redirect(url_for('payment.pay_page', slug=slug, order_token=order_token))

        dup_hash = conn.execute(
            "SELECT id FROM pending_orders WHERE operator_id=? AND receipt_hash=? AND status='confirmed'",
            (operator_id, receipt_hash)
        ).fetchone()

        op_dir = os.path.join(RECEIPTS_DIR, str(operator_id))
        os.makedirs(op_dir, exist_ok=True)
        stored_name = f"{order['id']}_{secrets.token_hex(8)}{ext}"
        full_path = os.path.join(op_dir, stored_name)
        with open(full_path, 'wb') as out:
            out.write(raw)

        exact_amt = float(order['exact_amount'])
        ocr_reference = None
        if dup_hash:
            text, ocr_amount, match_status = '', None, 'duplicate'
        else:
            text = _extract_text_from_pdf(full_path) if is_pdf else _ocr_image(full_path)
            ocr_reference = _extract_receipt_reference(text)
            candidates = _extract_amounts_from_text(text)
            matched_amount = next((c for c in candidates if abs(c - exact_amt) < 0.01), None)
            if matched_amount is not None:
                ocr_amount, match_status = matched_amount, 'matched'
            elif candidates:
                ocr_amount, match_status = candidates[0], 'mismatch'
            else:
                ocr_amount, match_status = None, 'no_amount'
    finally:
        conn.close()

    confirmed = False
    with _pay_lock:
        conn2 = get_db_connection()
        try:
            fresh = conn2.execute(
                "SELECT * FROM pending_orders WHERE id=? AND status IN ('pending','review')",
                (order['id'],)
            ).fetchone()
            if fresh:
                fields = (
                    "receipt_filename=?, receipt_hash=?, ocr_raw_text=?, ocr_parsed_amount=?, "
                    "ocr_match_status=?, ocr_reference=?"
                )
                params = [stored_name, receipt_hash, text[:2000], ocr_amount, match_status, ocr_reference]
                if match_status != 'matched':
                    fields += ", status='review'"
                conn2.execute(f"UPDATE pending_orders SET {fields} WHERE id=?", params + [order['id']])
                if match_status == 'matched':
                    refreshed = conn2.execute("SELECT * FROM pending_orders WHERE id=?", (order['id'],)).fetchone()
                    _confirm_order(conn2, refreshed)
                    confirmed = True
            conn2.commit()
        except Exception:
            conn2.rollback()
        finally:
            conn2.close()

    if confirmed:
        session['pay_message'] = ('success', 'تم التحقق من الإشعار آلياً وتأكيد طلبك فوراً!')
    else:
        session['pay_message'] = ('info', 'تم استلام الإشعار، وهو الآن قيد المراجعة اليدوية من الشبكة.')
        amt_label = f"{ocr_amount:.2f}₪" if ocr_amount is not None else 'غير واضح'
        _notify_telegram_review_async(
            operator,
            order['id'],
            f"📋 إيصال يحتاج مراجعة يدوية\nطلب #{order['id']} — المبلغ المطلوب: {float(order['exact_amount']):.2f}₪\nالمبلغ المقروء: {amt_label}",
            full_path,
            is_pdf,
        )

    return redirect(url_for('payment.pay_page', slug=slug, order_token=order_token))


def _confirm_order(conn, order):
    voucher_ids = json.loads(order['voucher_ids'])
    ph = ','.join('?' * len(voucher_ids))
    vouchers = conn.execute(f"SELECT * FROM vouchers WHERE id IN ({ph})", voucher_ids).fetchall()

    safe_chars = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
    base_txn_id = ''.join(secrets.choice(safe_chars) for _ in range(16))
    buyer_phone = order.get('account_phone') if hasattr(order, 'get') else order['account_phone']
    buyer_phone = buyer_phone or order['customer_phone']

    total_cards = len(vouchers)
    for i, voucher in enumerate(vouchers, start=1):
        conn.execute("UPDATE vouchers SET status='sold' WHERE id=? AND status='reserved'", (voucher['id'],))
        card_txn_id = f"{base_txn_id}-{i:02d}" if total_cards > 1 else base_txn_id
        conn.execute(
            "INSERT INTO transactions (transaction_id, customer_phone, voucher_id, amount_paid, wallet_code, operator_id) VALUES (?, ?, ?, ?, ?, ?)",
            (card_txn_id, buyer_phone, voucher['id'], voucher['price'], order['wallet_code'], order['operator_id'])
        )

    conn.execute(
        "UPDATE operators SET lifetime_revenue = lifetime_revenue + ? WHERE id = ?",
        (order['exact_amount'], order['operator_id'])
    )
    conn.execute(
        "INSERT INTO revenue_log (operator_id, amount) VALUES (?, ?)",
        (order['operator_id'], order['exact_amount'])
    )
    conn.execute(
        "UPDATE pending_orders SET status='confirmed', confirmed_at=CURRENT_TIMESTAMP WHERE id=?",
        (order['id'],)
    )


def _cancel_order(conn, order):
    voucher_ids = json.loads(order['voucher_ids'])
    if voucher_ids:
        ph = ','.join('?' * len(voucher_ids))
        conn.execute(
            f"UPDATE vouchers SET status='available' WHERE id IN ({ph}) AND status='reserved'",
            voucher_ids
        )
    conn.execute("UPDATE pending_orders SET status='cancelled' WHERE id=?", (order['id'],))


@payment_bp.route('/internal/cleanup-expired-orders')
def internal_cleanup_expired_orders():
    supplied = request.args.get('token', '')
    if not secrets.compare_digest(supplied, CLEANUP_TOKEN):
        abort(404)
    conn = get_db_connection()
    try:
        operator_ids = [row['id'] for row in conn.execute("SELECT id FROM operators").fetchall()]
        total_expired = 0
        with _pay_lock:
            for op_id in operator_ids:
                before = conn.execute(
                    "SELECT COUNT(*) FROM pending_orders WHERE operator_id=? AND status='pending' AND expires_at < ?",
                    (op_id, time.time())
                ).fetchone()[0]
                _expire_stale_orders(conn, op_id)
                total_expired += before
            conn.commit()
    finally:
        conn.close()
    return jsonify({'ok': True, 'expired': total_expired, 'operators_checked': len(operator_ids)})
