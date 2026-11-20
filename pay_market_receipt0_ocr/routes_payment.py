code = '''"""
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
from db_layer import (
    IS_POSTGRES,
    get_db_connection as _base_get_db_connection,
    get_table_columns,
    add_column_if_missing,
)
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
_CLEANUP_TOKEN_FILE = 'cleanup_token.key'
def _load_or_create_cleanup_token():
    if os.environ.get("CLEANUP_TOKEN"):
        return os.environ["CLEANUP_TOKEN"]
    if os.path.exists(_CLEANUP_TOKEN_FILE):
        with open(_CLEANUP_TOKEN_FILE, 'r') as f:
            return f.read().strip()
    token = secrets.token_hex(24)
    try:
        with open(_CLEANUP_TOKEN_FILE, 'w') as f:
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
    if h == 720: return 'بطاقة شهرية'
    if h == 1:  return 'ساعة واحدة'
    if h == 2:  return 'ساعتان'
    if 3 <= h <= 10: return f'{h} ساعات'
    return f'{h} ساعة'


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
    return b'\\r\\n'.join(parts), f'multipart/form-data; boundary={boundary}'


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


def _toast_page(icon, icon_color, icon_bg, title, message, btn_label, btn_href, btn_color):
    return f\'\'\'<!DOCTYPE html>
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
</html>\'\'\'


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


# ═══════════════════════════════════════════════════════════════════════════
# 1) بدء عملية شراء: حجز البطاقات + إنشاء طلب معلّق بمبلغ فريد
# ═══════════════════════════════════════════════════════════════════════════
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

    if not re.fullmatch(r'059\\d{7}', phone):
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
        return _toast_page("fa-triangle-exclamation", "text-amber-400", "bg-amber-50",
                           "بيانات غير صحيحة",
                           "تأكد من اختيار فئة الباقة والعدد بشكل صحيح لكل صنف.",
                           "العودة للرئيسية", url_for('user.index', slug=slug), "from-indigo-500 to-purple-600")

    SAFE_CHARS  = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
    wallet_code = ''.join(secrets.choice(SAFE_CHARS) for _ in range(6))
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
                    available   = len(vouchers)
                    price_label = int(price) if price == int(price) else price
                    cat_label   = f"{_hours_label(hours)} / {profile}" if hours else profile
                    msg = (f"المتاح حالياً {available} بطاقة فقط من فئة {cat_label} ({price_label} شيكل)."
                           if available > 0 else f"نفدت بطاقات فئة {cat_label} ({price_label} شيكل) تماماً.")
                    return _toast_page("fa-box-open", "text-orange-400", "bg-orange-50",
                                       "المخزون غير كافٍ", msg,
                                       "العودة للرئيسية", url_for('user.index', slug=slug), "from-indigo-500 to-purple-600")
                for v in vouchers:
                    reserved_ids.append(v['id'])
                total_base_amount += price * qty

            ph = ','.join('?' * len(reserved_ids))
            conn.execute(f"UPDATE vouchers SET status='reserved' WHERE id IN ({ph})", reserved_ids)

            exact_amount = round(total_base_amount, 2)
            expires_at = time.time() + PAYMENT_ORDER_TTL
            conn.execute(\'\'\'
                INSERT INTO pending_orders
                    (order_token, operator_id, customer_phone, account_phone, voucher_ids, wallet_code,
                     base_amount, exact_amount, status, expires_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)
            \'\'\', (order_token, operator_id, phone, account_phone, json.dumps(reserved_ids), wallet_code,
                  total_base_amount, exact_amount, expires_at))
            conn.commit()
        except Exception:
            conn.rollback()
            return _toast_page("fa-circle-exclamation", "text-rose-400", "bg-rose-50",
                               "خطأ في المعالجة", "حدث خطأ غير متوقع. يرجى المحاولة مجدداً.",
                               "العودة للرئيسية", url_for('user.index', slug=slug), "from-rose-500 to-purple-600")
        finally:
            try:
                conn.close()
            except Exception:
                pass

    return redirect(url_for('payment.pay_page', slug=slug, order_token=order_token))


# ═══════════════════════════════════════════════════════════════════════════
# 2) صفحة الانتظار: تعليمات الدفع + Polling على حالة الطلب
# ═══════════════════════════════════════════════════════════════════════════
@payment_bp.route('/shop/<slug>/pay/<order_token>')
def pay_page(slug, order_token):
    operator    = g.operator
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
        return _toast_page("fa-circle-question", "text-slate-400", "bg-slate-100",
                           "طلب غير موجود", "لم نجد هذا الطلب أو انتهت صلاحيته.",
                           "العودة للرئيسية", url_for('user.index', slug=slug), "from-indigo-500 to-purple-600")

    if order['status'] == 'confirmed':
        return redirect(url_for('payment.receipt', slug=slug, order_token=order_token))

    if order['status'] in ('expired', 'cancelled'):
        return _toast_page("fa-hourglass-end", "text-orange-400", "bg-orange-50",
                           "انتهت صلاحية الطلب",
                           "لم يصلنا تأكيد الدفع خلال المهلة المحددة. يمكنك المحاولة من جديد.",
                           "طلب جديد", url_for('user.index', slug=slug), "from-indigo-500 to-purple-600")

    jawwal_number = html_lib.escape((operator['jawwal_pay_number'] or '').strip())
    support_phone = html_lib.escape((operator['support_phone'] or '').strip())
    exact_amount  = f"{float(order['exact_amount']):.2f}"
    remaining     = max(0, int(float(order['expires_at']) - time.time()))
    status_url    = url_for('payment.order_status', slug=slug, order_token=order_token)
    cancel_url    = url_for('payment.cancel_order', slug=slug, order_token=order_token)
    upload_url    = url_for('payment.upload_receipt', slug=slug, order_token=order_token)
    csrf_token    = current_app.generate_csrf_token()

    pay_message = session.pop('pay_message', None)
    msg_html = ''
    if pay_message:
        kind, text = pay_message
        color = {'success': 'emerald', 'error': 'rose', 'info': 'indigo'}.get(kind, 'slate')
        icon  = {'success': 'fa-circle-check', 'error': 'fa-circle-exclamation', 'info': 'fa-circle-info'}.get(kind, 'fa-info')
        msg_html = f\'\'\'<div class="mb-4 bg-{color}-50 border border-{color}-100 text-{color}-700 text-xs font-bold px-4 py-3 rounded-xl flex items-center gap-2 text-right">
            <i class="fa-solid {icon}"></i><span>{html_lib.escape(text)}</span>
        </div>\'\'\'

    if order['status'] == 'review':
        status_box_icon  = 'fa-magnifying-glass'
        status_box_color = 'text-orange-500'
        status_box_text  = 'الإشعار قيد المراجعة اليدوية من الشبكة...'
    else:
        status_box_icon  = 'fa-circle-notch fa-spin'
        status_box_color = 'text-indigo-500'
        status_box_text  = 'بانتظار رفع إشعار التحويل...'

    upload_section = f\'\'\'
    <form id="receiptForm" method="POST" action="{upload_url}" enctype="multipart/form-data" class="mb-4 text-right">
        <input type="hidden" name="_csrf_token" value="{csrf_token}">
        <label class="block text-[11px] font-bold text-slate-500 mb-1.5">
            {'ارفع صورة أوضح لإشعار التحويل (اختياري)' if order['status'] == 'review' else 'ارفع صورة أو PDF لإشعار التحويل بعد إتمامه'}
        </label>
        <input type="file" id="receiptFileInput" name="receipt" dir="ltr"
               accept="image/jpeg,image/png,image/webp,application/pdf" required
               class="sr-only">
        <label for="receiptFileInput" class="flex items-center justify-center gap-2 w-full border-2 border-dashed border-indigo-200 rounded-xl py-4 px-3 cursor-pointer bg-indigo-50/60 active:bg-indigo-100 text-indigo-600 font-bold text-xs transition-colors">
            <i class="fa-solid fa-image"></i>
            <span id="receiptFileLabel">اضغط هنا لاختيار صورة أو PDF</span>
        </label>
        <p class="text-[10px] text-slate-400 mt-1">JPG · PNG · WEBP · PDF — حتى 4 ميغابايت</p>
        <button type="submit" id="uploadBtn" class="w-full mt-3 bg-indigo-600 hover:bg-indigo-700 text-white font-bold py-3 rounded-xl text-sm">
            <i class="fa-solid fa-arrow-up-from-bracket"></i> رفع الإشعار
        </button>
    </form>

    <script>
    (function() {{
        var inp = document.getElementById('receiptFileInput');
        var lbl = document.getElementById('receiptFileLabel');
        if (inp && lbl) {{
            inp.addEventListener('change', function() {{
                lbl.textContent = (inp.files && inp.files[0]) ? inp.files[0].name : 'اضغط هنا لاختيار صورة أو PDF';
            }});
        }}
        var form = document.getElementById('receiptForm');
        var btn  = document.getElementById('uploadBtn');
        if (form && btn) {{
            form.addEventListener('submit', function() {{
                btn.disabled = true;
                btn.innerHTML = '<i class="fa-solid fa-circle-notch fa-spin"></i> جاري الرفع...';
            }});
        }}
    }})();
    </script>\'\'\'

    return f\'\'\'<!DOCTYPE html>
<html lang="ar" dir="rtl">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>إتمام الدفع</title>
<script src="https://cdn.tailwindcss.com"></script>
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css">
<style>@import url('https://fonts.googleapis.com/css2?family=Cairo:wght@400;600;700;800&display=swap'); body{{font-family:'Cairo',sans-serif;}}
@keyframes pulse-ring {{0%{{transform:scale(0.9);opacity:0.6}}100%{{transform:scale(1.5);opacity:0}}}}
.pulse::before{{content:'';position:absolute;inset:0;border-radius:9999px;background:rgb(99 102 241 / 0.4);animation:pulse-ring 1.6s cubic-bezier(0.4,0,0.6,1) infinite;}}
</style>
</head>
<body class="bg-slate-50 min-h-screen flex items-center justify-center p-4">
<div class="bg-white rounded-3xl shadow-xl border border-slate-100 max-w-md w-full p-6 sm:p-8 text-center">
    <div class="relative w-20 h-20 mx-auto mb-5 pulse">
        <div class="relative w-20 h-20 bg-indigo-50 rounded-full flex items-center justify-center">
            <i class="fa-solid fa-mobile-screen-button text-indigo-500 text-3xl"></i>
        </div>
    </div>
    <h2 class="text-xl font-extrabold text-slate-800 mb-1">حوّل عبر جوال باي وارفع الإشعار</h2>
    <p class="text-xs text-slate-500 mb-6">سيتم تسليم بطاقاتك تلقائياً فور التحقق من الإشعار</p>

    {msg_html}

    <div class="bg-indigo-50 border-2 border-dashed border-indigo-300 rounded-2xl p-5 mb-4">
        <p class="text-[11px] font-bold text-indigo-500 mb-1">المبلغ المطلوب بالضبط</p>
        <div class="text-4xl font-extrabold text-indigo-700 tracking-wide" dir="ltr">{exact_amount} <span class="text-lg">₪</span></div>
    </div>

    <div class="bg-slate-50 rounded-xl p-4 mb-4 text-right">
        <p class="text-[11px] font-bold text-slate-400 mb-1">رقم جوال باي المستلم</p>
        <div class="flex items-center justify-between">
            <span class="text-lg font-mono font-extrabold text-slate-800 tracking-widest" dir="ltr">{jawwal_number or 'غير محدد بعد'}</span>
        </div>
    </div>

    {upload_section}

    <div id="statusBox" class="flex items-center justify-center gap-2 {status_box_color} font-bold text-sm mb-6">
        <i class="fa-solid {status_box_icon}"></i>
        <span>{status_box_text}</span>
    </div>

    <p class="text-[11px] text-slate-400 mb-4">تنتهي صلاحية الطلب خلال <span id="countdown" class="font-mono font-bold text-slate-600">--:--</span> — إن واجهت أي مشكلة تواصل معنا على {support_phone or 'رقم الدعم'}</p>

    <form method="POST" action="{cancel_url}">
        <input type="hidden" name="_csrf_token" value="{csrf_token}">
        <button type="submit" class="w-full bg-slate-100 hover:bg-slate-200 text-slate-600 font-bold py-3 rounded-xl text-sm">إلغاء الطلب</button>
    </form>
</div>
<script>
let remaining = {remaining};
function tick() {{
    if (remaining <= 0) {{ document.getElementById('countdown').textContent = '00:00'; return; }}
    const m = String(Math.floor(remaining/60)).padStart(2,'0');
    const s = String(remaining%60).padStart(2,'0');
    document.getElementById('countdown').textContent = m + ':' + s;
    remaining--;
}}
tick(); setInterval(tick, 1000);

async function poll() {{
    try {{
        const res = await fetch("{status_url}");
        const data = await res.json();
        if (data.status === 'confirmed') {{
            document.getElementById('statusBox').innerHTML =
                '<i class="fa-solid fa-circle-check text-emerald-500"></i><span class="text-emerald-600">تم التأكيد! جاري تحويلك...</span>';
            window.location.href = data.redirect;
        }} else if (data.status === 'expired' || data.status === 'cancelled') {{
            window.location.reload();
        }}
    }} catch(e) {{}}
}}
setInterval(poll, 4000);
</script>
</body>
</html>\'\'\'


# ═══════════════════════════════════════════════════════════════════════════
# 3) استعلام حالة الطلب (Polling من صفحة الانتظار)
# ═══════════════════════════════════════════════════════════════════════════
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


# ═══════════════════════════════════════════════════════════════════════════
# 4) إلغاء الطلب من طرف العميل — يفكّ الحجز فوراً
# ═══════════════════════════════════════════════════════════════════════════
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


# ═══════════════════════════════════════════════════════════════════════════
# 5) إيصال الاستلام — بعد التأكيد فقط
# ═══════════════════════════════════════════════════════════════════════════
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
            return _toast_page("fa-circle-question", "text-slate-400", "bg-slate-100",
                               "طلب غير موجود", "لم نجد إيصالاً مؤكداً لهذا الطلب.",
                               "العودة للرئيسية", url_for('user.index', slug=slug), "from-indigo-500 to-purple-600")

        voucher_ids = json.loads(order['voucher_ids'])
        ph = ','.join('?' * len(voucher_ids))
        cards = conn.execute(f"SELECT username, password, price, profile, duration_hours FROM vouchers WHERE id IN ({ph})", voucher_ids).fetchall()
    finally:
        conn.close()

    session['recent_bought_codes'] = [
        {'username': c['username'], 'password': c['password'], 'price': c['price'],
         'profile': c['profile'], 'duration_hours': c['duration_hours'],
         'customer_phone': order['customer_phone']}
        for c in cards
    ]
    return redirect(url_for('user.index', slug=slug))


# ═══════════════════════════════════════════════════════════════════════════
# 6) رفع صورة/PDF لإشعار التحويل + قراءة آلية (OCR) لمطابقة المبلغ
# ═══════════════════════════════════════════════════════════════════════════
_RECEIPT_AMOUNT_RE = re.compile(
    r'(?:ils|nis)\\s*(\\d+(?:[.,]\\d{1,2})?)|(\\d+(?:[.,]\\d{1,2})?)\\s*(?:شيكل|شواقل|₪|nis|ils)',
    re.IGNORECASE
)

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


_MONTH_NAMES = {'jan':1,'feb':2,'mar':3,'apr':4,'may':5,'jun':6,'jul':7,'aug':8,'sep':9,'oct':10,'nov':11,'dec':12}
_RECEIPT_DATE_RE = re.compile(
    r'(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\\.?\\s+(\\d{1,2}),?\\s+(\\d{4})',
    re.IGNORECASE
)
_RECEIPT_TIME_RE = re.compile(
    r'\\b(\\d{1,2}):(\\d{2})(?::(\\d{2}))?\\s*([AaPp]\\.?[Mm]\\.?)?\\b'
)

try:
    from zoneinfo import ZoneInfo
    _GAZA_TZ = ZoneInfo('Asia/Gaza')
except Exception:
    _GAZA_TZ = None

RECEIPT_MAX_AGE_HOURS = 1.5 if _GAZA_TZ is not None else 6

def _extract_receipt_date(text):
    if not text:
        return None
    m = _RECEIPT_DATE_RE.search(text)
    if not m:
        return None
    try:
        import datetime as _dt
        month = _MONTH_NAMES[m.group(1)[:3].lower()]
        day   = int(m.group(2))
        year  = int(m.group(3))
        return _dt.date(year, month, day)
    except (ValueError, KeyError):
        return None


def _extract_receipt_time(text):
    if not text:
        return None
    import datetime as _dt
    for m in _RECEIPT_TIME_RE.finditer(text):
        try:
            hour   = int(m.group(1))
            minute = int(m.group(2))
            second = int(m.group(3) or 0)
            ampm   = (m.group(4) or '').lower().replace('.', '')
            if ampm == 'pm' and hour != 12:
                hour += 12
            elif ampm == 'am' and hour == 12:
                hour = 0
            return _dt.time(hour, minute, second)
        except ValueError:
            continue
    return None


def _receipt_is_stale(receipt_date, receipt_time=None):
    if receipt_date is None:
        return False
    import datetime as _dt
    if _GAZA_TZ is not None:
        now = _dt.datetime.now(_dt.timezone.utc).astimezone(_GAZA_TZ).replace(tzinfo=None)
    else:
        now = _dt.datetime.now()
    if receipt_time is not None:
        receipt_dt = _dt.datetime.combine(receipt_date, receipt_time)
        age_hours = abs((now - receipt_dt).total_seconds()) / 3600
        return age_hours > RECEIPT_MAX_AGE_HOURS
    age_days = (now.date() - receipt_date).days
    return age_days >= 1 or age_days < -1


_RECEIPT_REF_RE = re.compile(r'\\b(\\d{12,16})\\b')

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
    except Exception:
        return ''
    try:
        return pytesseract.image_to_string(img, lang='eng+ara') or ''
    except Exception:
        try:
            return pytesseract.image_to_string(img) or ''
        except Exception:
            return ''


def _extract_text_from_pdf(path):
    if not _PYPDF_AVAILABLE:
        return ''
    try:
        reader = PdfReader(path)
        return '\\n'.join((page.extract_text() or '') for page in reader.pages[:3])
    except Exception:
        return ''


@payment_bp.route('/shop/<slug>/pay/<order_token>/upload-receipt', methods=['POST'])
def upload_receipt(slug, order_token):
    operator    = g.operator
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
                session['pay_message'] = ('error', 'تعذّر التحقق من الصورة على السيرفر حالياً. تواصل مع الدعم.')
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

        ocr_reference, receipt_date, receipt_time = None, None, None
        exact_amt = float(order['exact_amount'])
        if dup_hash:
            text, ocr_amount, match_status = '', None, 'duplicate'
        else:
            text = _extract_text_from_pdf(full_path) if is_pdf else _ocr_image(full_path)
            ocr_reference = _extract_receipt_reference(text)
            receipt_date  = _extract_receipt_date(text)
            receipt_time  = _extract_receipt_time(text)

            dup_ref = None
            if ocr_reference:
                dup_ref = conn.execute(
                    "SELECT id FROM pending_orders WHERE operator_id=? AND ocr_reference=? AND status='confirmed'",
                    (operator_id, ocr_reference)
                ).fetchone()

            candidates = _extract_amounts_from_text(text)
            matched_amount = next((c for c in candidates if abs(c - exact_amt) < 0.01), None)

            if dup_ref:
                ocr_amount, match_status = matched_amount, 'duplicate'
            elif matched_amount is not None and _receipt_is_stale(receipt_date, receipt_time):
                ocr_amount, match_status = matched_amount, 'old_receipt'
            elif matched_amount is not None:
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
                "SELECT * FROM pending_orders WHERE id=? AND status IN ('pending','review')", (order['id'],)
            ).fetchone()
            if fresh:
                fields = ("receipt_filename=?, receipt_hash=?, ocr_raw_text=?, ocr_parsed_amount=?, "
                           "ocr_match_status=?, ocr_reference=?")
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

    exact_amt_str = f"{float(order['exact_amount']):.2f}"
    if confirmed:
        session['pay_message'] = ('success', 'تم التحقق من الإشعار آلياً وتأكيد طلبك فوراً!')
    elif match_status == 'duplicate':
        session['pay_message'] = ('error', 'هذا الإشعار (أو هذا التحويل بعينه) استُخدم من قبل لتأكيد طلب آخر. تواصل مع الدعم لمراجعة حالتك.')
        caption = (f"🚫 محاولة إعادة استخدام إشعار/تحويل مؤكَّد سابقاً\\n"
                   f"طلب #{order['id']} — المبلغ المطلوب: {exact_amt_str}₪")
        _notify_telegram_review_async(operator, order['id'], caption, full_path, is_pdf)
    elif match_status == 'old_receipt':
        session['pay_message'] = ('error', 'هذا الإشعار يبدو لتحويل قديم وليس تحويلاً حديثاً لهذا الطلب. سيراجعه المشغّل يدوياً.')
        caption = (f"⏰ إيصال بتاريخ قديم ({receipt_date})\\n"
                   f"طلب #{order['id']} — المبلغ المطلوب: {exact_amt_str}₪")
        _notify_telegram_review_async(operator, order['id'], caption, full_path, is_pdf)
    else:
        session['pay_message'] = ('info', 'تم استلام الإشعار، وهو الآن قيد المراجعة اليدوية من الشبكة.')
        amt_label = f"{ocr_amount:.2f}₪" if ocr_amount is not None else 'غير واضح'
        caption = (f"📋 إيصال يحتاج مراجعة يدوية\\n"
                   f"طلب #{order['id']} — المبلغ المطلوب: {exact_amt_str}₪\\n"
                   f"المبلغ المقروء آلياً: {amt_label}")
        _notify_telegram_review_async(operator, order['id'], caption, full_path, is_pdf)

    return redirect(url_for('payment.pay_page', slug=slug, order_token=order_token))


def _confirm_order(conn, order):
    voucher_ids = json.loads(order['voucher_ids'])
    ph = ','.join('?' * len(voucher_ids))
    vouchers = conn.execute(f"SELECT * FROM vouchers WHERE id IN ({ph})", voucher_ids).fetchall()

    SAFE_CHARS  = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
    base_txn_id = ''.join(secrets.choice(SAFE_CHARS) for _ in range(16))

    buyer_phone = order.get('account_phone') or order['customer_phone']

    total_cards = len(vouchers)
    for i, voucher in enumerate(vouchers, start=1):
        conn.execute("UPDATE vouchers SET status='sold' WHERE id=? AND status='reserved'", (voucher['id'],))
        card_txn_id = f"{base_txn_id}-{i:02d}" if total_cards > 1 else base_txn_id
        conn.execute(
            "INSERT INTO transactions (transaction_id, customer_phone, voucher_id, amount_paid, wallet_code, operator_id) VALUES (?, ?, ?, ?, ?, ?)",
            (card_txn_id, buyer_phone, voucher['id'], voucher['price'], order['wallet_code'], order['operator_id'])
        )

    conn.execute("UPDATE operators SET lifetime_revenue = lifetime_revenue + ? WHERE id = ?",
                 (order['exact_amount'], order['operator_id']))
    conn.execute("INSERT INTO revenue_log (operator_id, amount) VALUES (?, ?)",
                 (order['operator_id'], order['exact_amount']))
    conn.execute("UPDATE pending_orders SET status='confirmed', confirmed_at=CURRENT_TIMESTAMP WHERE id=?", (order['id'],))

    op_row = conn.execute(
        "SELECT telegram_bot_token, telegram_chat_id FROM operators WHERE id=?", (order['operator_id'],)
    ).fetchone()
    if op_row and op_row['telegram_bot_token'] and op_row['telegram_chat_id']:
        categories = {(v['price'], v['profile'], v['duration_hours']) for v in vouchers}
        for cat_price, cat_profile, cat_hours in categories:
            remaining = conn.execute(
                "SELECT COUNT(*) FROM vouchers WHERE operator_id=? AND price=? AND profile=? AND duration_hours=? AND status='available'",
                (order['operator_id'], cat_price, cat_profile, cat_hours)
            ).fetchone()[0]
            if remaining < LOW_STOCK_THRESHOLD:
                _notify_telegram_async(
                    op_row['telegram_bot_token'], op_row['telegram_chat_id'],
                    f"⚠️ مخزون منخفض — الفئة: {cat_profile} / {_hours_label(cat_hours)} / {cat_price}₪ — باقي {remaining} بطاقة فقط."
                )


def _cancel_order(conn, order):
    voucher_ids = json.loads(order['voucher_ids'])
    if voucher_ids:
        ph = ','.join('?' * len(voucher_ids))
        conn.execute(f"UPDATE vouchers SET status='available' WHERE id IN ({ph}) AND status='reserved'", voucher_ids)
    conn.execute("UPDATE pending_orders SET status='cancelled' WHERE id=?", (order['id'],))


_RATE_LIMIT_STALE_AFTER = 24 * 60 * 60

def _cleanup_stale_rate_limits(conn):
    now = time.time()
    cur = conn.execute(
        "DELETE FROM rate_limits WHERE locked_until < ? AND window_start < ?",
        (now, now - _RATE_LIMIT_STALE_AFTER)
    )
    return cur.rowcount


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
            rate_limits_purged = _cleanup_stale_rate_limits(conn)
            conn.commit()
    finally:
        conn.close()
    return jsonify({'ok': True, 'expired': total_expired, 'operators_checked': len(operator_ids),
                     'rate_limits_purged': rate_limits_purged})
'''

code = code.replace("\\'\\'\\'", "'''")

with open('routes_payment.py', 'w', encoding='utf-8') as f:
    f.write(code)

compile(code, 'routes_payment.py', 'exec')
print("routes_payment.py updated and compiled successfully.")
