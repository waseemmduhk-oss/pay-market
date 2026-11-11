from flask import request, session, redirect, url_for, make_response, current_app, render_template, jsonify, g
import re, csv, io, os, math, pypdf
from werkzeug.security import generate_password_hash
from report_utils import _xlsx_safe
from .common import (
    admin_bp, get_db_connection, write_audit, _require_admin_login, verify_admin_password,
    is_using_default_password, ALLOWED_EXTENSIONS, MAX_FILE_SIZE, PAGE_SIZE,
)

# ── مدد الكروت المعتمدة عند الرفع ────────────────────────────────────────────
# المشغّل بيختار المدة من قائمة عند رفع الملف، وبيتعبّى السعر والسرعة تلقائياً
# بالقيم المعتادة لكل مدة (أو بآخر سعر وسرعة باعهم المشغّل نفسه لهاي المدة)،
# وبيقدر يعدّلهم. قبل كان عدد الساعات يُحسب من جدول ثابت (بروفايل + سعر) فأي
# تغيير بالسعر (مثلاً 1 ← 1.50) كان يوقف الرفع. القيم المعتادة هون مأخوذة من
# ذلك الجدول القديم + البطاقة الشهرية (2M بـ50 شيكل).
MONTHLY_HOURS = 720            # البطاقة الشهرية = 30 يوم
DURATION_OPTIONS = [
    {'hours': 2,             'label': 'ساعتان',       'profile': '5M', 'price': 2},
    {'hours': 8,             'label': '8 ساعات',      'profile': '2M', 'price': 1},
    {'hours': 10,            'label': '10 ساعات',     'profile': '3M', 'price': 2},
    {'hours': 24,            'label': '24 ساعة',      'profile': '2M', 'price': 3},
    {'hours': MONTHLY_HOURS, 'label': 'بطاقة شهرية',  'profile': '2M', 'price': 50},
]
ALLOWED_DURATIONS = {o['hours'] for o in DURATION_OPTIONS}

# حد أمان لسعر الكرت — يُستخدم عند "تعديل السعر" بالمخزون (update_category_price)
MAX_CARD_PRICE = 1000          # شيكل — حد أمان ضد خطأ كتابي (مثلاً 15000 بدل 1.5)

def _hours_label(h):
    h = int(h or 0)
    if h <= 0:  return ''
    if h == MONTHLY_HOURS: return 'بطاقة شهرية'
    if h == 1:  return 'ساعة واحدة'
    if h == 2:  return 'ساعتان'
    if 3 <= h <= 10: return f'{h} ساعات'
    return f'{h} ساعة'

# ── استخراج بأنماط متعددة ────────────────────────────────────────────────────
def extract_vouchers_from_text(full_text, price, profile, duration_hours, conn, operator_id):
    lines = [l.strip() for l in full_text.split('\n')]
    pairs = []

    # نمط 1: سطرين
    u_pat = re.compile(r'\b\d{12}\b')
    p_pat = re.compile(r'\b\d{4,8}\b')
    cur   = None
    for line in lines:
        if not line:
            continue
        um = u_pat.search(line)
        if um:
            cur  = um.group()
            rest = line[um.end():]
            pm   = p_pat.search(rest)
            if pm:
                pairs.append((cur, pm.group())); cur = None
            continue
        if cur:
            pm = p_pat.search(line)
            if pm:
                pairs.append((cur, pm.group())); cur = None

    # نمط 2: inline username:password
    if not pairs:
        pat2 = re.compile(r'(\b\d{12}\b)[\s:/,|]+?(\b\d{4,8}\b)')
        for line in lines:
            m = pat2.search(line)
            if m:
                pairs.append((m.group(1), m.group(2)))

    # نمط 3: CSV
    if not pairs:
        try:
            for row in csv.reader(io.StringIO(full_text)):
                if len(row) >= 2:
                    u, p = row[0].strip(), row[1].strip()
                    if re.fullmatch(r'\d{12}', u) and re.fullmatch(r'\d{4,8}', p):
                        pairs.append((u, p))
        except Exception:
            pass

    inserted = duplicates = 0
    for u, p in pairs:
        try:
            c = conn.execute(
                'INSERT OR IGNORE INTO vouchers (username,password,price,profile,status,operator_id,duration_hours) VALUES (?,?,?,?,?,?,?)',
                (u, p, price, profile, 'available', operator_id, duration_hours)
            )
            if c.rowcount: inserted += 1
            else:          duplicates += 1
        except Exception:
            duplicates += 1
    return inserted, duplicates


# ── API: فلترة البطاقات من السيرفر (بدلاً من تحميل كل شيء) ──────────────────
@admin_bp.route('/<slug>/admin/vouchers-json')
def vouchers_json(slug):
    if not _require_admin_login(slug):
        return jsonify({'error': 'unauthorized'}), 403
    operator_id   = g.operator['id']
    status_filter = request.args.get('status', '')
    search        = request.args.get('q', '').strip()
    page          = max(1, int(request.args.get('page', 1)))
    offset        = (page - 1) * PAGE_SIZE

    where  = ['operator_id = ?']
    params = [operator_id]
    if status_filter in ('available', 'sold'):
        where.append('status = ?');  params.append(status_filter)
    if search:
        where.append('(username LIKE ? OR password LIKE ? OR profile LIKE ? OR CAST(id AS TEXT) LIKE ?)')
        like = f'%{search}%'
        params.extend([like, like, like, like])

    where_sql = 'WHERE ' + ' AND '.join(where)
    conn  = get_db_connection()
    total = conn.execute(f'SELECT COUNT(*) FROM vouchers {where_sql}', params).fetchone()[0]
    # ملاحظة: عمود id في vouchers تلقائي الترقيم على مستوى القاعدة كلها (مشترك
    # بين كل المشغّلين)، فكان يظهر للمشغّل أرقاماً غير متسلسلة (فيها "قفزات")
    # لأن بطاقات مشغّلين آخرين تحتل أرقاماً بينها. "seq" هنا رقم تسلسلي خاص
    # بهذا المشغّل فقط (1، 2، 3...) حسب ترتيب إضافة بطاقاته هو، بغض النظر عن
    # بطاقات بقية المشغّلين أو أي فلترة/بحث مطبّق على القائمة.
    #
    # ملاحظة أداء: سابقاً كان seq يُحسب بـ subquery مرتبط (COUNT لكل صف
    # مُرجَع) — تكلفته تقارب O(عدد الصفوف بالصفحة × عدد بطاقات المشغّل)،
    # وتصير بطيئة تدريجياً مع نمو المخزون. استبدلناه بـ ROW_NUMBER() تُحسب
    # مرة واحدة فقط عبر ترتيب/مسح واحد لكل بطاقات المشغّل (بدون الفلترة
    # الحالية، حتى يبقى الترقيم ثابتاً بغض النظر عن البحث/الفلتر)، وبعدين
    # نطبّق الفلترة والصفحات فوق النتيجة.
    rows = conn.execute(
        f'''SELECT id, username, password, price, profile, duration_hours, status, seq
            FROM (
                SELECT id, username, password, price, profile, duration_hours, status, operator_id,
                       ROW_NUMBER() OVER (PARTITION BY operator_id ORDER BY id) AS seq
                FROM vouchers
                WHERE operator_id = ?
            )
            {where_sql} ORDER BY id DESC LIMIT ? OFFSET ?''',
        [operator_id] + params + [PAGE_SIZE, offset]
    ).fetchall()
    conn.close()

    return jsonify({
        'total': total,
        'page':  page,
        'pages': max(1, -(-total // PAGE_SIZE)),  # ceiling division
        'rows':  [dict(r) for r in rows]
    })


# ── لوحة التحكم ──────────────────────────────────────────────────────────────
@admin_bp.route('/<slug>/admin/dashboard', methods=['GET', 'POST'])
def admin_dashboard(slug):
    if not _require_admin_login(slug):
        return redirect(url_for('admin.admin_login', slug=slug))

    operator    = g.operator
    operator_id = operator['id']
    conn = get_db_connection()

    # تحديث البيانات العامة للشبكة (رقم الدعم، Jawwal Pay، عنوان/رابط الهوتسبوت)
    if request.method == 'POST' and request.form.get('update_profile') == '1':
        current_app.validate_csrf()
        support_phone     = request.form.get('support_phone', '').strip()
        jawwal_number     = request.form.get('jawwal_pay_number', '').strip()
        hotspot_title     = request.form.get('hotspot_title', '').strip()
        hotspot_login_url = request.form.get('hotspot_login_url', '').strip() or 'http://00.00.00.00/login'
        if not hotspot_login_url.startswith(('http://', 'https://')):
            session['settings_message'] = ('error', 'عنوان صفحة الهوتسبوت يجب أن يبدأ بـ http:// أو https://')
            conn.close()
            return redirect(url_for('admin.admin_dashboard', slug=slug))

        # ── شعار (لوجو) الشبكة — رفع اختياري، أو إزالة الشعار الحالي ──────
        # ملاحظة أمان: SVG مستبعد عمداً من الصيغ المسموحة — ملفات SVG ممكن
        # تحتوي <script> أو أحداث onload/onclick وتتنفّذ لو انفتح الملف مباشرة
        # برابطه (خارج <img>)، وهذا يعطي XSS مخزّن بنفس أصل الموقع. لو احتجت
        # دعم SVG مستقبلاً لازم تعقيمه فعلياً قبل الحفظ (إزالة script/on*/
        # javascript: URIs) وليس فقط الاعتماد على الامتداد.
        LOGO_ALLOWED_EXT = {'.png', '.jpg', '.jpeg', '.webp'}
        LOGO_MAX_SIZE    = 2 * 1024 * 1024  # 2MB كافية جداً للوجو
        logo_filename = operator['logo_filename'] or ''
        logo_dir = os.path.join(current_app.root_path, 'static', 'logos')

        if request.form.get('remove_logo') == '1':
            if logo_filename:
                old_path = os.path.join(logo_dir, logo_filename)
                if os.path.exists(old_path):
                    os.remove(old_path)
            logo_filename = ''
        else:
            logo_file = request.files.get('logo_file')
            if logo_file and logo_file.filename:
                ext = os.path.splitext(logo_file.filename)[1].lower()
                if ext not in LOGO_ALLOWED_EXT:
                    session['settings_message'] = ('error', 'صيغة الصورة غير مدعومة. مسموح: PNG, JPG, WEBP فقط.')
                    conn.close()
                    return redirect(url_for('admin.admin_dashboard', slug=slug))
                logo_file.seek(0, os.SEEK_END)
                size = logo_file.tell()
                logo_file.seek(0)
                if size > LOGO_MAX_SIZE:
                    session['settings_message'] = ('error', 'حجم الصورة أكبر من 2 ميجابايت المسموح بها.')
                    conn.close()
                    return redirect(url_for('admin.admin_dashboard', slug=slug))
                os.makedirs(logo_dir, exist_ok=True)
                # اسم ثابت حسب رقم المشغّل حتى يستبدل شعاره القديم تلقائياً بدل ما يتراكم ملفات يتيمة
                new_filename = f"logo_{operator_id}{ext}"
                logo_file.save(os.path.join(logo_dir, new_filename))
                # لو الامتداد الجديد مختلف عن القديم (مثلاً استبدل png بـ jpg)، نحذف الملف القديم
                if logo_filename and logo_filename != new_filename:
                    old_path = os.path.join(logo_dir, logo_filename)
                    if os.path.exists(old_path):
                        os.remove(old_path)
                logo_filename = new_filename

        logo_size = request.form.get('logo_size', 'medium')
        if logo_size not in ('small', 'medium', 'large', 'xlarge'):
            logo_size = 'medium'

        conn.execute(
            'UPDATE operators SET support_phone=?, jawwal_pay_number=?, hotspot_title=?, hotspot_login_url=?, logo_filename=?, logo_size=? WHERE id=?',
            (support_phone, jawwal_number, hotspot_title, hotspot_login_url, logo_filename, logo_size, operator_id)
        )
        conn.commit()
        write_audit("PROFILE_UPDATE", "تحديث بيانات الشبكة العامة", operator_id=operator_id)
        session['settings_message'] = ('success', 'تم تحديث بيانات الشبكة بنجاح.')
        conn.close()
        return redirect(url_for('admin.admin_dashboard', slug=slug))

    # تغيير كلمة المرور
    if request.method == 'POST' and request.form.get('change_password') == '1':
        current_app.validate_csrf()
        cur = request.form.get('current_password', '')
        new = request.form.get('new_password', '')
        cfm = request.form.get('confirm_password', '')

        if not verify_admin_password(operator_id, cur):
            session['settings_message'] = ('error', 'كلمة المرور الحالية غير صحيحة!')
        elif new != cfm:
            session['settings_message'] = ('error', 'كلمة المرور الجديدة غير متطابقة!')
        elif len(new) < 8:
            session['settings_message'] = ('error', 'كلمة المرور يجب أن تكون 8 أحرف على الأقل!')
        else:
            conn.execute('UPDATE operator_admins SET password_hash=? WHERE operator_id=?', (generate_password_hash(new), operator_id))
            conn.commit()
            write_audit("PASSWORD_CHANGE", "تم تغيير كلمة مرور المسؤول", operator_id=operator_id)
            session['settings_message'] = ('success', 'تم تحديث كلمة المرور بنجاح.')
        conn.close()
        return redirect(url_for('admin.admin_dashboard', slug=slug))

    # رفع الملفات
    if request.method == 'POST' and 'pdf_file' in request.files:
        current_app.validate_csrf()
        file     = request.files['pdf_file']
        filename = (file.filename or '').lower()
        ext      = ('.' + filename.rsplit('.', 1)[-1]) if '.' in filename else ''
        if ext not in ALLOWED_EXTENSIONS:
            session['upload_error'] = 'نوع الملف غير مسموح. المقبول: PDF، TXT، XLSX.'
            conn.close(); return redirect(url_for('admin.admin_dashboard', slug=slug))
        file.seek(0, 2); size = file.tell(); file.seek(0)
        if size > MAX_FILE_SIZE:
            session['upload_error'] = f'حجم الملف يتجاوز 5MB.'
            conn.close(); return redirect(url_for('admin.admin_dashboard', slug=slug))
        try:
            price = round(float(request.form['file_price']), 2)
        except (ValueError, KeyError):
            price = 0
        if not (math.isfinite(price) and 0 < price <= MAX_CARD_PRICE):
            session['upload_error'] = f'سعر غير صحيح — لازم يكون بين 0.01 و{MAX_CARD_PRICE} شيكل.'
            conn.close(); return redirect(url_for('admin.admin_dashboard', slug=slug))
        profile = (request.form.get('file_profile') or '').strip()
        if not profile or len(profile) > 30:
            session['upload_error'] = 'اكتب سرعة الكرت (بروفايل الميكروتيك) — حتى 30 حرف.'
            conn.close(); return redirect(url_for('admin.admin_dashboard', slug=slug))
        try:
            duration_hours = int(request.form.get('file_hours', ''))
        except ValueError:
            duration_hours = None
        if duration_hours not in ALLOWED_DURATIONS:
            session['upload_error'] = 'اختر مدة الكرت من القائمة.'
            conn.close(); return redirect(url_for('admin.admin_dashboard', slug=slug))
        full_text = ""
        if ext == '.pdf':
            try:
                reader = pypdf.PdfReader(file)
                for pg in reader.pages: full_text += (pg.extract_text() or "") + "\n"
            except Exception:
                session['upload_error'] = 'فشل قراءة PDF. تأكد أنه غير محمي.'
                conn.close(); return redirect(url_for('admin.admin_dashboard', slug=slug))
        elif ext == '.txt':
            full_text = file.read().decode('utf-8', errors='ignore')
        elif ext in ('.xlsx', '.xls'):
            try:
                import openpyxl
                wb = openpyxl.load_workbook(file, data_only=True, read_only=True)
                for ws in wb.worksheets:
                    for row in ws.iter_rows(values_only=True):
                        cells = []
                        for c in row:
                            if c is None: cells.append("")
                            elif isinstance(c, float) and c == int(c): cells.append(str(int(c)))
                            else: cells.append(str(c).strip())
                        full_text += ",".join(cells) + "\n"
            except Exception:
                session['upload_error'] = 'فشل قراءة Excel.'
                conn.close(); return redirect(url_for('admin.admin_dashboard', slug=slug))
        if full_text:
            ins, dup = extract_vouchers_from_text(full_text, price, profile, duration_hours, conn, operator_id)
            conn.commit()
            write_audit("UPLOAD_CARDS", f"ملف='{file.filename}' مضاف={ins} مكرر={dup} سعر={price}₪ بروفايل={profile} ساعات={duration_hours}", operator_id=operator_id)
            if ins == 0 and dup == 0:
                session['upload_error'] = 'لم يتم العثور على بطاقات. تحقق من تنسيق الملف.'
            elif ins == 0:
                session['upload_error'] = f'جميع البطاقات ({dup}) موجودة مسبقاً.'
            else:
                session['upload_success']    = ins
                if dup > 0: session['upload_duplicates'] = dup
        else:
            session['upload_error'] = 'لم يتم العثور على نص في الملف.'
        conn.close()
        return redirect(url_for('admin.admin_dashboard', slug=slug))

    # بيانات الإحصاء فقط (لا نُحمّل كل البطاقات — تُحمَّل عبر API)
    total_count     = conn.execute('SELECT COUNT(*) FROM vouchers WHERE operator_id=?', (operator_id,)).fetchone()[0]
    available_count = conn.execute('SELECT COUNT(*) FROM vouchers WHERE status="available" AND operator_id=?', (operator_id,)).fetchone()[0]
    sold_count      = conn.execute('SELECT COUNT(*) FROM vouchers WHERE status="sold" AND operator_id=?', (operator_id,)).fetchone()[0]
    revenue         = conn.execute('SELECT COALESCE(SUM(amount_paid),0) FROM transactions WHERE operator_id=?', (operator_id,)).fetchone()[0]

    # بحث عميل
    customer_search_phone = request.args.get('search_phone', '').strip()
    customer_search_date  = request.args.get('search_date', '').strip()
    customer_groups = []
    if customer_search_phone:
        sql    = '''
            SELECT t.transaction_id, t.wallet_code, t.created_at, t.amount_paid,
                   v.username, v.password, v.price, v.profile
            FROM transactions t JOIN vouchers v ON t.voucher_id = v.id
            WHERE t.customer_phone = ? AND t.operator_id = ?
        '''
        params = [customer_search_phone, operator_id]
        if customer_search_date:
            sql += ' AND date(t.created_at) = ?'
            params.append(customer_search_date)
        sql += ' ORDER BY t.id DESC'
        txn_rows = conn.execute(sql, params).fetchall()
        groups, order = {}, []
        for row in txn_rows:
            key = row['wallet_code'] or row['transaction_id']
            if key not in groups:
                groups[key] = {'wallet_code': row['wallet_code'], 'created_at': row['created_at'], 'cards': [], 'total': 0.0}
                order.append(key)
            groups[key]['cards'].append(row)
            groups[key]['total'] += row['amount_paid']
        customer_groups = [groups[k] for k in order]

    # سجل النشاط الخاص بهذا المشغّل فقط (آخر 50)
    audit_logs = conn.execute('SELECT * FROM audit_log WHERE operator_id=? ORDER BY id DESC LIMIT 50', (operator_id,)).fetchall()

    # ── تنبيه انخفاض المخزون: نحسب المتاح لكل باقة (سعر/بروفايل) على حدة ──
    LOW_STOCK_THRESHOLD = 10
    stock_by_price = conn.execute('''
        SELECT price, profile, duration_hours, COUNT(CASE WHEN status="available" THEN 1 END) AS avail
        FROM vouchers WHERE operator_id=?
        GROUP BY price, profile, duration_hours ORDER BY price ASC
    ''', (operator_id,)).fetchall()
    low_stock_items  = [dict(r) for r in stock_by_price if r['avail'] < LOW_STOCK_THRESHOLD]
    stock_by_category = []
    for r in stock_by_price:
        d = dict(r)
        d['hours_label'] = _hours_label(d['duration_hours'])
        stock_by_category.append(d)
    # القيم المعبّأة تلقائياً بنموذج الرفع لكل مدة: السعر والسرعة الحاليين لهاي المدة عند
    # المشغّل نفسه (يفضَّل آخر كرت متاح للبيع، وإلا آخر كرت)، وإلا القيمة المعتادة
    own = {}
    for r in conn.execute('''
            SELECT duration_hours, price, profile FROM vouchers
            WHERE operator_id=? AND duration_hours IN (%s)
            ORDER BY (status='available') DESC, id DESC''' % ','.join('?' * len(ALLOWED_DURATIONS)),
            (operator_id, *sorted(ALLOWED_DURATIONS))).fetchall():
        own.setdefault(r['duration_hours'], r)
    duration_options = []
    for o in DURATION_OPTIONS:
        d = dict(o)
        if o['hours'] in own:
            d['price'], d['profile'] = own[o['hours']]['price'], own[o['hours']]['profile']
        d['price'] = int(d['price']) if float(d['price']) == int(d['price']) else round(float(d['price']), 2)
        duration_options.append(d)
    conn.close()

    import time
    login_time        = session.get('login_time', time.time())
    session_remaining = max(0, int(1800 - (time.time() - login_time)))

    resp = make_response(render_template('admin_dashboard.html',
        slug                  = slug,
        operator_name         = operator['name'],
        operator_support_phone     = operator['support_phone'] or '',
        operator_jawwal_number     = operator['jawwal_pay_number'] or '',
        operator_hotspot_title     = operator['hotspot_title'] or '',
        operator_hotspot_login_url = operator['hotspot_login_url'] or 'http://00.00.00.00/login',
        operator_logo_filename     = operator['logo_filename'] or '',
        operator_logo_size         = operator['logo_size'] or 'medium',
        csrf_token            = current_app.generate_csrf_token(),
        total_count           = total_count,
        available_count       = available_count,
        sold_count            = sold_count,
        revenue               = revenue,
        low_stock_items       = low_stock_items,
        low_stock_threshold   = LOW_STOCK_THRESHOLD,
        stock_by_category     = stock_by_category,
        duration_options      = duration_options,
        customer_search_phone = customer_search_phone,
        customer_search_date  = customer_search_date,
        customer_groups       = customer_groups,
        session_remaining     = session_remaining,
        session_minutes       = session_remaining // 60,
        session_seconds       = session_remaining % 60,
        upload_count          = session.pop('upload_success',    None),
        upload_duplicates     = session.pop('upload_duplicates', None),
        upload_error          = session.pop('upload_error',      None),
        settings_message      = session.pop('settings_message',  None),
        audit_logs            = audit_logs,
        using_default_password = is_using_default_password(operator_id),
    ))
    # لمنع أي متصفح/بروكسي من عرض نسخة قديمة من اللوحة (مخزون قديم مثلاً)
    # بعد أي عملية حذف أو تعديل تعيد التوجيه لنفس هذه الصفحة
    resp.headers["Cache-Control"] = "no-store"
    return resp


# ── حذف ──────────────────────────────────────────────────────────────────────
@admin_bp.route('/<slug>/admin/delete-vouchers', methods=['POST'])
def delete_vouchers(slug):
    if not _require_admin_login(slug): return "غير مسموح", 403
    current_app.validate_csrf()
    operator_id  = g.operator['id']
    delete_type  = request.form.get('delete_type')
    selected_ids = request.form.getlist('selected_vouchers')
    conn = get_db_connection()
    try:
        if delete_type == 'all':
            count = conn.execute('SELECT COUNT(*) FROM vouchers WHERE operator_id=?', (operator_id,)).fetchone()[0]
            conn.execute('DELETE FROM transactions WHERE operator_id=?', (operator_id,))
            conn.execute('DELETE FROM vouchers WHERE operator_id=?', (operator_id,))
            write_audit("DELETE_ALL", f"تصفير شامل: {count} بطاقة", operator_id=operator_id)
            conn.commit()
            session['settings_message'] = ('success', f'تم حذف كل البطاقات بنجاح ({count} بطاقة).')
        elif delete_type == 'sold':
            count = conn.execute('SELECT COUNT(*) FROM vouchers WHERE status="sold" AND operator_id=?', (operator_id,)).fetchone()[0]
            conn.execute('DELETE FROM transactions WHERE operator_id=? AND voucher_id IN (SELECT id FROM vouchers WHERE status="sold" AND operator_id=?)', (operator_id, operator_id))
            conn.execute('DELETE FROM vouchers WHERE status="sold" AND operator_id=?', (operator_id,))
            write_audit("DELETE_SOLD", f"{count} بطاقة مباعة", operator_id=operator_id)
            conn.commit()
            session['settings_message'] = ('success', f'تم حذف البطاقات المباعة بنجاح ({count} بطاقة).')
        elif delete_type == 'selected' and selected_ids:
            safe = [str(int(i)) for i in selected_ids if str(i).isdigit()]
            if safe:
                ph = ','.join('?'*len(safe))
                # نتأكد إن كل البطاقات المحددة تتبع فعلاً لهذا المشغّل قبل الحذف
                owned = conn.execute(f'SELECT id FROM vouchers WHERE id IN ({ph}) AND operator_id=?', safe + [operator_id]).fetchall()
                owned_ids = [str(r['id']) for r in owned]
                if owned_ids:
                    ph2 = ','.join('?'*len(owned_ids))
                    conn.execute(f'DELETE FROM transactions WHERE voucher_id IN ({ph2})', owned_ids)
                    conn.execute(f'DELETE FROM vouchers WHERE id IN ({ph2})', owned_ids)
                    write_audit("DELETE_SELECTED", f"{len(owned_ids)} بطاقة IDs={','.join(owned_ids)}", operator_id=operator_id)
                    conn.commit()
                    session['settings_message'] = ('success', f'تم حذف البطاقات المحددة بنجاح ({len(owned_ids)} بطاقة).')
                else:
                    session['settings_message'] = ('error', 'لم يتم العثور على أي بطاقة من المحددة ضمن بطاقاتك.')
            else:
                session['settings_message'] = ('error', 'لم يتم تحديد أي بطاقة للحذف.')
        elif delete_type == 'category':
            cat_price   = request.form.get('cat_price')
            cat_profile = request.form.get('cat_profile', '')
            cat_hours   = request.form.get('cat_hours', '0')
            try:
                cat_price = float(cat_price)
                cat_hours = int(cat_hours)
            except (TypeError, ValueError):
                session['settings_message'] = ('error', 'بيانات الفئة غير صالحة.')
                conn.close()
                return redirect(url_for('admin.admin_dashboard', slug=slug))
            # نحذف فقط البطاقات غير المباعة (available) من هذه الفئة، حتى لا تنكسر
            # سجلات المبيعات/الفواتير المرتبطة بأي بطاقة من نفس الفئة سبق بيعها.
            # نعتمد على rowcount نفسه من عملية الحذف كمصدر وحيد للعدد الحقيقي
            # المحذوف، بدل عدّ منفصل قبلها، تفادياً لأي تعارض توقيت بين الاثنين.
            cur = conn.execute(
                'DELETE FROM vouchers WHERE operator_id=? AND price=? AND profile=? AND duration_hours=? AND status="available"',
                (operator_id, cat_price, cat_profile, cat_hours)
            )
            count = cur.rowcount
            write_audit("DELETE_CATEGORY", f"فئة profile={cat_profile} ساعات={cat_hours} سعر={cat_price}₪: حُذف {count} بطاقة غير مباعة", operator_id=operator_id)
            conn.commit()
            session['settings_message'] = ('success', f'تم حذف {count} بطاقة غير مباعة من هذه الفئة بنجاح.')
        else:
            session['settings_message'] = ('error', 'نوع عملية الحذف غير صالح.')
    except Exception as ex:
        conn.rollback()
        session['settings_message'] = ('error', f'فشلت عملية الحذف: {ex}')
    finally: conn.close()
    return redirect(url_for('admin.admin_dashboard', slug=slug))


@admin_bp.route('/<slug>/admin/update-category-price', methods=['POST'])
def update_category_price(slug):
    """تغيير سعر فئة كروت (سعر + بروفايل + مدة) دفعة واحدة.

    يغيّر سعر البطاقات "المتاحة" (غير المباعة) فقط:
      - البطاقات المباعة وسجلاتها وإيراداتها السابقة ما بتتأثر أبداً.
      - البطاقات المحجوزة لطلب قيد الدفع بتضل بسعرها القديم (الزبون شاف ودفع
        بالسعر القديم، ومبلغ الطلب نفسه ثابت)، وبعد انتهاء الطلب لو رجعت متاحة
        ممكن يظهر عدد قليل منها بالفئة القديمة — يعدّل المشغّل سعرها بنفس الطريقة.
      - لو في فئة موجودة أصلاً بنفس (السعر الجديد + البروفايل + المدة) بتندمج
        معها تلقائياً لأن الفئة هي الثلاثية نفسها.
    البروفايل والمدة (اللي بتحدد اسم الكرت) ما بيتغيروا."""
    if not _require_admin_login(slug): return "غير مسموح", 403
    current_app.validate_csrf()
    operator_id = g.operator['id']
    back = redirect(url_for('admin.admin_dashboard', slug=slug))

    try:
        old_price   = float(request.form.get('cat_price'))
        cat_profile = request.form.get('cat_profile', '')
        cat_hours   = int(request.form.get('cat_hours', '0') or 0)
        new_price   = round(float((request.form.get('new_price') or '').strip().replace(',', '.')), 2)
    except (TypeError, ValueError):
        session['settings_message'] = ('error', 'بيانات غير صالحة — تأكد من كتابة السعر الجديد بأرقام.')
        return back
    if not math.isfinite(new_price) or new_price < 0.01 or new_price > MAX_CARD_PRICE:
        session['settings_message'] = ('error', f'السعر لازم يكون بين 0.01 و{MAX_CARD_PRICE} شيكل.')
        return back
    if abs(new_price - old_price) < 0.005:
        session['settings_message'] = ('error', 'السعر الجديد نفس السعر الحالي — ما في شي تغيّر.')
        return back

    conn = get_db_connection()
    try:
        cur = conn.execute(
            "UPDATE vouchers SET price=? WHERE operator_id=? AND status='available' "
            "AND ABS(price-?) < 0.005 AND profile=? AND COALESCE(duration_hours,0)=?",
            (new_price, operator_id, old_price, cat_profile, cat_hours)
        )
        count = cur.rowcount
        reserved = conn.execute(
            "SELECT COUNT(*) FROM vouchers WHERE operator_id=? AND status='reserved' "
            "AND ABS(price-?) < 0.005 AND profile=? AND COALESCE(duration_hours,0)=?",
            (operator_id, old_price, cat_profile, cat_hours)
        ).fetchone()[0]
        # نثبّت التعديل أولاً ثم نكتب سجل النشاط: write_audit بتفتح اتصالها الخاص،
        # ولو كتبت والتعديل لسا غير مثبّت بتلاقي القاعدة مقفولة وبتفشل بصمت
        conn.commit()
        if count:
            write_audit("UPDATE_CATEGORY_PRICE",
                        f"فئة profile={cat_profile} ساعات={cat_hours}: سعر {count} بطاقة من {old_price}₪ إلى {new_price}₪",
                        operator_id=operator_id)
        if count:
            msg = f'تم تغيير سعر {count} بطاقة من {old_price:g}₪ إلى {new_price:g}₪.'
            if reserved:
                msg += f' ({reserved} بطاقة قيد الشراء حالياً ضلّت بالسعر القديم.)'
            session['settings_message'] = ('success', msg)
        else:
            session['settings_message'] = ('error', 'ما في بطاقات متاحة بهالفئة لتغيير سعرها (ممكن تكون انباعت كلها).')
    except Exception as ex:
        conn.rollback()
        session['settings_message'] = ('error', f'فشل تغيير السعر: {ex}')
    finally:
        conn.close()
    return back


@admin_bp.route('/<slug>/admin/delete-single', methods=['POST'])
def delete_card_single(slug):
    if not _require_admin_login(slug): return "Unauthorized", 403
    current_app.validate_csrf()
    operator_id = g.operator['id']
    card_id = request.form.get('card_id')
    if card_id and str(card_id).isdigit():
        conn = get_db_connection()
        # نتأكد إن البطاقة تتبع لهذا المشغّل قبل أي حذف (عزل بيانات صارم)
        row = conn.execute('SELECT username,profile FROM vouchers WHERE id=? AND operator_id=?', (card_id, operator_id)).fetchone()
        if row:
            conn.execute('DELETE FROM transactions WHERE voucher_id=?', (card_id,))
            conn.execute('DELETE FROM vouchers WHERE id=?', (card_id,))
            conn.commit()
            write_audit("DELETE_SINGLE", f"ID={card_id}" + (f" user={row['username']}" if row else ""), operator_id=operator_id)
            session['settings_message'] = ('success', f"تم حذف البطاقة ({row['username']}) بنجاح.")
        else:
            session['settings_message'] = ('error', 'لم يتم العثور على هذه البطاقة ضمن بطاقاتك.')
        conn.close()
    else:
        session['settings_message'] = ('error', 'معرّف البطاقة غير صالح.')
    return redirect(url_for('admin.admin_dashboard', slug=slug))


# ── تصدير CSV ────────────────────────────────────────────────────────────────
@admin_bp.route('/<slug>/admin/export-archive')
def export_archive(slug):
    if not _require_admin_login(slug): return "غير مسموح", 403
    operator_id   = g.operator['id']
    status_filter = request.args.get('status')
    conn = get_db_connection()
    if status_filter in ('sold', 'available'):
        vouchers = conn.execute('SELECT * FROM vouchers WHERE status=? AND operator_id=?', (status_filter, operator_id)).fetchall()
    else:
        vouchers = conn.execute('SELECT * FROM vouchers WHERE operator_id=?', (operator_id,)).fetchall()
    conn.close()
    si = io.StringIO()
    cw = csv.writer(si)
    cw.writerow(['ID', 'Username', 'Password', 'Price', 'Profile', 'Status'])
    for v in vouchers:
        # حماية من CSV Formula Injection (نفس فكرة _xlsx_safe) — ملفات CSV
        # كمان ممكن تُفسَّر كصيغ لو فُتحت بـ Excel/Sheets وبدأت الخلية بـ =+-@
        cw.writerow([v['id'], _xlsx_safe(v['username']), _xlsx_safe(v['password']), v['price'], _xlsx_safe(v['profile']), v['status']])
    label = {'sold': 'sold', 'available': 'available'}.get(status_filter, 'all')
    write_audit("EXPORT_ARCHIVE", f"status={label} عدد={len(vouchers)}", operator_id=operator_id)
    out = make_response(si.getvalue())
    out.headers["Content-Disposition"] = f"attachment; filename=archive_{label}.csv"
    out.headers["Content-type"]        = "text/csv; charset=utf-8-sig"
    out.headers["Cache-Control"]       = "no-store"
    return out


# ── تصدير Excel للمبيعات ────────────────────────────────────────────────────
@admin_bp.route('/<slug>/admin/export-sales-excel')
def export_sales_excel(slug):
    if not _require_admin_login(slug): return "غير مسموح", 403
    operator_id = g.operator['id']
    try:
        import openpyxl
        from openpyxl.styles import Font, PatternFill, Alignment
    except ImportError:
        return "openpyxl غير مثبّت. شغّل: pip install openpyxl", 500
    conn = get_db_connection()
    rows = conn.execute('''
        SELECT t.transaction_id, t.customer_phone, t.wallet_code, t.amount_paid, t.created_at,
               v.username, v.password, v.price, v.profile
        FROM transactions t JOIN vouchers v ON t.voucher_id=v.id
        WHERE t.operator_id=? ORDER BY t.id DESC
    ''', (operator_id,)).fetchall()
    conn.close()
    wb = openpyxl.Workbook(); ws = wb.active
    ws.title = "تقرير المبيعات"
    ws.sheet_view.rightToLeft = True
    headers = ['#', 'رقم العملية', 'هاتف العميل', 'رمز المحفظة', 'المبلغ', 'التاريخ', 'Username', 'Password', 'السعر', 'البروفايل']
    hfill = PatternFill("solid", fgColor="1E3A5F")
    hfont = Font(bold=True, color="FFFFFF", size=11)
    for ci, h in enumerate(headers, 1):
        c = ws.cell(row=1, column=ci, value=h)
        c.fill = hfill; c.font = hfont; c.alignment = Alignment(horizontal='center')
    afill = PatternFill("solid", fgColor="F0F4F8")
    for ri, r in enumerate(rows, 2):
        data = [ri-1, _xlsx_safe(r['transaction_id']), _xlsx_safe(r['customer_phone']), _xlsx_safe(r['wallet_code']),
                r['amount_paid'], r['created_at'], _xlsx_safe(r['username']), _xlsx_safe(r['password']), r['price'], _xlsx_safe(r['profile'])]
        for ci, val in enumerate(data, 1):
            c = ws.cell(row=ri, column=ci, value=val)
            if ri % 2 == 0: c.fill = afill
            c.alignment = Alignment(horizontal='center')
    for col in ws.columns:
        ml = max((len(str(c.value or '')) for c in col), default=10)
        ws.column_dimensions[col[0].column_letter].width = min(ml + 4, 40)
    buf = io.BytesIO(); wb.save(buf); buf.seek(0)
    write_audit("EXPORT_SALES_EXCEL", f"{len(rows)} سجل", operator_id=operator_id)
    out = make_response(buf.getvalue())
    out.headers["Content-Disposition"] = "attachment; filename=sales_report.xlsx"
    out.headers["Content-type"]        = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    out.headers["Cache-Control"]       = "no-store"
    return out


# ── تصدير بطاقات فئة معيّنة (غير مباعة) لملف إكسل جاهز للطباعة/الاستخدام لاحقاً ──
@admin_bp.route('/<slug>/admin/export-category')
def export_category(slug):
    if not _require_admin_login(slug): return "غير مسموح", 403
    operator_id = g.operator['id']
    try:
        import openpyxl
        from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
    except ImportError:
        return "openpyxl غير مثبّت. شغّل: pip install openpyxl", 500

    try:
        cat_price = float(request.args.get('cat_price'))
        cat_hours = int(request.args.get('cat_hours', '0'))
    except (TypeError, ValueError):
        return "بيانات الفئة غير صالحة.", 400
    cat_profile = request.args.get('cat_profile', '')

    conn = get_db_connection()
    rows = conn.execute('''
        SELECT username, password FROM vouchers
        WHERE operator_id=? AND price=? AND profile=? AND duration_hours=? AND status="available"
        ORDER BY id ASC
    ''', (operator_id, cat_price, cat_profile, cat_hours)).fetchall()
    conn.close()

    wb = openpyxl.Workbook(); ws = wb.active
    hours_label = _hours_label(cat_hours) or f'بروفايل {cat_profile}'
    ws.title = "بطاقات"[:31]
    ws.sheet_view.rightToLeft = True

    thin = Side(style='thin', color='CCCCCC')
    border = Border(left=thin, right=thin, top=thin, bottom=thin)

    ws.merge_cells('A1:C1')
    title_cell = ws.cell(row=1, column=1, value=f"فئة: {hours_label} ({cat_profile}) — {cat_price}₪ — {len(rows)} بطاقة غير مباعة")
    title_cell.font = Font(bold=True, size=13, color="1E3A5F")
    title_cell.alignment = Alignment(horizontal='center')

    headers = ['#', 'Username', 'Password']
    hfill = PatternFill("solid", fgColor="1E3A5F")
    hfont = Font(bold=True, color="FFFFFF", size=11)
    for ci, h in enumerate(headers, 1):
        c = ws.cell(row=3, column=ci, value=h)
        c.fill = hfill; c.font = hfont; c.alignment = Alignment(horizontal='center'); c.border = border

    afill = PatternFill("solid", fgColor="F0F4F8")
    for ri, r in enumerate(rows, 4):
        for ci, val in enumerate([ri - 3, _xlsx_safe(r['username']), _xlsx_safe(r['password'])], 1):
            c = ws.cell(row=ri, column=ci, value=val)
            if ri % 2 == 1: c.fill = afill
            c.alignment = Alignment(horizontal='center')
            c.border = border

    ws.column_dimensions['A'].width = 6
    ws.column_dimensions['B'].width = 22
    ws.column_dimensions['C'].width = 22

    buf = io.BytesIO(); wb.save(buf); buf.seek(0)
    write_audit("EXPORT_CATEGORY", f"فئة profile={cat_profile} ساعات={cat_hours} سعر={cat_price}₪: {len(rows)} بطاقة غير مباعة", operator_id=operator_id)
    fname = f"cards_{cat_profile}_{cat_hours}h_{int(cat_price)}nis.xlsx".replace(' ', '_')
    out = make_response(buf.getvalue())
    out.headers["Content-Disposition"] = f"attachment; filename={fname}"
    out.headers["Content-type"]        = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    out.headers["Cache-Control"]       = "no-store"
    return out

# ── مسح سجل النشاط الخاص بهذا المشغّل فقط ────────────────────────────────────
@admin_bp.route('/<slug>/admin/audit-log/clear', methods=['POST'])
def clear_operator_audit_log(slug):
    if not _require_admin_login(slug): return "غير مسموح", 403
    current_app.validate_csrf()
    operator_id = g.operator['id']
    conn = get_db_connection()
    try:
        count = conn.execute('SELECT COUNT(*) FROM audit_log WHERE operator_id=?', (operator_id,)).fetchone()[0]
        conn.execute('DELETE FROM audit_log WHERE operator_id=?', (operator_id,))
        conn.commit()
        write_audit("AUDIT_LOG_CLEARED", f"تم مسح {count} سجل خاص بهذا المشغّل", operator_id=operator_id)
        session['settings_message'] = ('success', f'تم مسح سجل النشاط الخاص بشبكتك بالكامل ({count} سجل).')
    except Exception as ex:
        conn.rollback()
        session['settings_message'] = ('error', f'فشل مسح السجل: {ex}')
    finally:
        conn.close()
    return redirect(url_for('admin.admin_dashboard', slug=slug))