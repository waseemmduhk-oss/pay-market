from flask import request, session, redirect, url_for, current_app, render_template
import re
from werkzeug.security import generate_password_hash
from .common import dev_bp, get_db_connection, write_dev_audit, _require_dev_login

# ── إضافة مشغّل جديد ─────────────────────────────────────────────────────────
SLUG_RE = re.compile(r'^[a-z0-9]([a-z0-9-]{1,48}[a-z0-9])?$')

@dev_bp.route('/operators/new', methods=['POST'])
def create_operator():
    if not _require_dev_login():
        return redirect(url_for('dev.dev_login'))
    current_app.validate_csrf()
    slug          = request.form.get('slug', '').strip().lower()
    name          = request.form.get('name', '').strip()
    admin_user    = request.form.get('admin_username', '').strip()
    admin_pass    = request.form.get('admin_password', '')
    support_phone = request.form.get('support_phone', '').strip()
    jawwal_number = request.form.get('jawwal_pay_number', '').strip()

    if not SLUG_RE.fullmatch(slug):
        session['op_message'] = ('error', 'الرابط (slug) يجب أن يكون حروف/أرقام إنجليزية وشرطات فقط، بدون مسافات.')
        return redirect(url_for('dev.dev_dashboard'))
    if not name or not admin_user or len(admin_pass) < 8:
        session['op_message'] = ('error', 'تأكد من تعبئة الاسم، اسم مستخدم الأدمن، وكلمة مرور 8 أحرف على الأقل.')
        return redirect(url_for('dev.dev_dashboard'))

    conn = get_db_connection()
    try:
        existing = conn.execute('SELECT id FROM operators WHERE slug=?', (slug,)).fetchone()
        if existing:
            session['op_message'] = ('error', f'الرابط "{slug}" مستخدَم مسبقاً لمشغّل آخر.')
            conn.close()
            return redirect(url_for('dev.dev_dashboard'))

        cur = conn.execute(
            'INSERT INTO operators (slug, name, jawwal_pay_number, support_phone, is_active) VALUES (?,?,?,?,1)',
            (slug, name, jawwal_number, support_phone)
        )
        op_id = cur.lastrowid
        conn.execute(
            'INSERT INTO operator_admins (operator_id, username, password_hash) VALUES (?,?,?)',
            (op_id, admin_user, generate_password_hash(admin_pass))
        )
        conn.commit()
        write_dev_audit("OPERATOR_CREATED", f"slug={slug} name={name}")
        session['op_message'] = ('success', f'تم إنشاء المشغّل "{name}" بنجاح. رابط المتجر: /shop/{slug}/')
    except Exception as ex:
        conn.rollback()
        session['op_message'] = ('error', f'فشل الإنشاء: {ex}')
    finally:
        conn.close()
    return redirect(url_for('dev.dev_dashboard'))

# ── تعديل بيانات مشغّل (شبكة) موجود ──────────────────────────────────────────
@dev_bp.route('/operators/<int:op_id>/edit', methods=['POST'])
def edit_operator(op_id):
    if not _require_dev_login():
        return redirect(url_for('dev.dev_login'))
    current_app.validate_csrf()
    slug              = request.form.get('slug', '').strip().lower()
    name              = request.form.get('name', '').strip()
    hotspot_title     = request.form.get('hotspot_title', '').strip()
    hotspot_login_url = request.form.get('hotspot_login_url', '').strip()
    support_phone     = request.form.get('support_phone', '').strip()
    jawwal_number     = request.form.get('jawwal_pay_number', '').strip()

    if not hotspot_login_url:
        hotspot_login_url = 'http://00.00.00.00/login'
    if not re.match(r'^https?://', hotspot_login_url):
        session['op_message'] = ('error', 'عنوان صفحة الهوتسبوت يجب أن يبدأ بـ http:// أو https://')
        return redirect(url_for('dev.dev_dashboard'))

    if not SLUG_RE.fullmatch(slug):
        session['op_message'] = ('error', 'الرابط (slug) يجب أن يكون حروف/أرقام إنجليزية وشرطات فقط، بدون مسافات.')
        return redirect(url_for('dev.dev_dashboard'))
    if not name:
        session['op_message'] = ('error', 'اسم الشبكة مطلوب.')
        return redirect(url_for('dev.dev_dashboard'))

    conn = get_db_connection()
    try:
        op = conn.execute('SELECT id, slug FROM operators WHERE id=?', (op_id,)).fetchone()
        if not op:
            session['op_message'] = ('error', 'المشغّل غير موجود.')
            return redirect(url_for('dev.dev_dashboard'))

        clash = conn.execute('SELECT id FROM operators WHERE slug=? AND id!=?', (slug, op_id)).fetchone()
        if clash:
            session['op_message'] = ('error', f'الرابط "{slug}" مستخدَم مسبقاً لمشغّل آخر.')
            return redirect(url_for('dev.dev_dashboard'))

        conn.execute(
            'UPDATE operators SET name=?, slug=?, hotspot_title=?, hotspot_login_url=?, support_phone=?, jawwal_pay_number=? WHERE id=?',
            (name, slug, hotspot_title, hotspot_login_url, support_phone, jawwal_number, op_id)
        )
        conn.commit()
        write_dev_audit("OPERATOR_EDITED", f"id={op_id} slug: {op['slug']} -> {slug} name={name}")
        session['op_message'] = ('success', f'تم تحديث بيانات شبكة "{name}" بنجاح.')
    except Exception as ex:
        conn.rollback()
        session['op_message'] = ('error', f'فشل التحديث: {ex}')
    finally:
        conn.close()
    return redirect(url_for('dev.dev_dashboard'))

# ── تعديل اسم مستخدم أدمن مشغّل (بمعزل عن كلمة المرور) ───────────────────────
@dev_bp.route('/operators/<int:op_id>/edit-admin-username', methods=['POST'])
def edit_operator_admin_username(op_id):
    if not _require_dev_login():
        return redirect(url_for('dev.dev_login'))
    current_app.validate_csrf()
    new_username = request.form.get('admin_username', '').strip()
    if not new_username:
        session['op_message'] = ('error', 'اسم المستخدم مطلوب.')
        return redirect(url_for('dev.dev_dashboard'))

    conn = get_db_connection()
    try:
        op = conn.execute('SELECT name FROM operators WHERE id=?', (op_id,)).fetchone()
        if not op:
            session['op_message'] = ('error', 'المشغّل غير موجود.')
            return redirect(url_for('dev.dev_dashboard'))
        conn.execute('UPDATE operator_admins SET username=? WHERE operator_id=?', (new_username, op_id))
        conn.commit()
        write_dev_audit("OPERATOR_ADMIN_USERNAME_EDITED", f"id={op_id} name={op['name']} -> {new_username}")
        session['op_message'] = ('success', f'تم تحديث اسم مستخدم أدمن "{op["name"]}" إلى "{new_username}".')
    except Exception as ex:
        conn.rollback()
        session['op_message'] = ('error', f'فشل التحديث: {ex}')
    finally:
        conn.close()
    return redirect(url_for('dev.dev_dashboard'))

@dev_bp.route('/operators/<int:op_id>/toggle', methods=['POST'])
def toggle_operator(op_id):
    if not _require_dev_login():
        return redirect(url_for('dev.dev_login'))
    current_app.validate_csrf()
    conn = get_db_connection()
    row  = conn.execute('SELECT is_active, name FROM operators WHERE id=?', (op_id,)).fetchone()
    if row:
        new_state = 0 if row['is_active'] else 1
        conn.execute('UPDATE operators SET is_active=? WHERE id=?', (new_state, op_id))
        conn.commit()
        write_dev_audit("OPERATOR_TOGGLED", f"id={op_id} name={row['name']} -> {'مفعّل' if new_state else 'معطّل'}")
        session['op_message'] = ('success', f'تم {"تفعيل" if new_state else "تعطيل"} المشغّل "{row["name"]}".')
    conn.close()
    return redirect(url_for('dev.dev_dashboard'))

# ── إعادة تعيين كلمة مرور أدمن مشغّل ──────────────────────────────────────────
@dev_bp.route('/operators/<int:op_id>/reset-password', methods=['POST'])
def reset_operator_password(op_id):
    if not _require_dev_login():
        return redirect(url_for('dev.dev_login'))
    current_app.validate_csrf()
    new_pass = request.form.get('new_password', '')
    conn = get_db_connection()
    op = conn.execute('SELECT name FROM operators WHERE id=?', (op_id,)).fetchone()
    if not op:
        session['op_message'] = ('error', 'المشغّل غير موجود.')
    elif len(new_pass) < 8:
        session['op_message'] = ('error', 'كلمة المرور يجب أن تكون 8 أحرف على الأقل.')
    else:
        conn.execute('UPDATE operator_admins SET password_hash=? WHERE operator_id=?', (generate_password_hash(new_pass), op_id))
        conn.commit()
        write_dev_audit("OPERATOR_PASSWORD_RESET", f"id={op_id} name={op['name']}")
        session['op_message'] = ('success', f'تم تحديث كلمة مرور أدمن "{op["name"]}".')
    conn.close()
    return redirect(url_for('dev.dev_dashboard'))

# ── حذف مشغّل نهائياً (وكل بياناته) ────────────────────────────────────────────
@dev_bp.route('/operators/<int:op_id>/delete', methods=['POST'])
def delete_operator(op_id):
    if not _require_dev_login():
        return redirect(url_for('dev.dev_login'))
    current_app.validate_csrf()
    confirm_slug = request.form.get('confirm_slug', '').strip().lower()
    conn = get_db_connection()
    op = conn.execute('SELECT slug, name FROM operators WHERE id=?', (op_id,)).fetchone()
    if not op or confirm_slug != op['slug']:
        session['op_message'] = ('error', 'يجب كتابة رابط (slug) المشغّل بالضبط لتأكيد الحذف.')
        conn.close()
        return redirect(url_for('dev.dev_dashboard'))
    try:
        conn.execute('DELETE FROM transactions WHERE operator_id=?', (op_id,))
        conn.execute('DELETE FROM vouchers WHERE operator_id=?', (op_id,))
        conn.execute('DELETE FROM revenue_log WHERE operator_id=?', (op_id,))
        conn.execute('DELETE FROM operator_admins WHERE operator_id=?', (op_id,))
        conn.execute('UPDATE audit_log SET operator_id=NULL WHERE operator_id=?', (op_id,))
        conn.execute('DELETE FROM operators WHERE id=?', (op_id,))
        conn.commit()
        write_dev_audit("OPERATOR_DELETED", f"id={op_id} slug={op['slug']} name={op['name']}")
        session['op_message'] = ('success', f'تم حذف المشغّل "{op["name"]}" وكل بياناته نهائياً.')
    except Exception as ex:
        conn.rollback()
        session['op_message'] = ('error', f'فشل الحذف: {ex}')
    finally:
        conn.close()
    return redirect(url_for('dev.dev_dashboard'))


# ── مسح الإيرادات (لشبكة معيّنة أو لكل الشبكات) ───────────────────────────────
@dev_bp.route('/operators/reset-revenue', methods=['POST'])
def reset_revenue():
    if not _require_dev_login():
        return redirect(url_for('dev.dev_login'))
    current_app.validate_csrf()
    scope = request.form.get('scope')  # 'single' أو 'all'
    conn = get_db_connection()
    try:
        if scope == 'all':
            confirm_text = request.form.get('confirm_all', '').strip()
            if confirm_text != 'مسح الكل':
                session['op_message'] = ('error', 'يجب كتابة "مسح الكل" بالضبط لتأكيد مسح إيرادات كل الشبكات.')
                conn.close()
                return redirect(url_for('dev.dev_dashboard'))
            count = conn.execute('SELECT COUNT(*) FROM operators WHERE lifetime_revenue != 0').fetchone()[0]
            conn.execute('UPDATE operators SET lifetime_revenue = 0')
            conn.execute('DELETE FROM revenue_log')
            conn.commit()
            write_dev_audit("REVENUE_RESET_ALL", f"تصفير إيرادات {count} شبكة (كل الشبكات)")
            session['op_message'] = ('success', f'تم مسح إيرادات كل الشبكات بنجاح ({count} شبكة).')
        elif scope == 'single':
            op_id = request.form.get('op_id')
            op = conn.execute('SELECT id, name, lifetime_revenue FROM operators WHERE id=?', (op_id,)).fetchone()
            if not op:
                session['op_message'] = ('error', 'المشغّل غير موجود.')
            else:
                conn.execute('UPDATE operators SET lifetime_revenue = 0 WHERE id=?', (op['id'],))
                conn.execute('DELETE FROM revenue_log WHERE operator_id=?', (op['id'],))
                conn.commit()
                write_dev_audit("REVENUE_RESET_SINGLE", f"id={op['id']} name={op['name']} (كان {op['lifetime_revenue']}₪)")
                session['op_message'] = ('success', f'تم مسح إيرادات شبكة "{op["name"]}" بنجاح.')
        else:
            session['op_message'] = ('error', 'نطاق غير صالح.')
    except Exception as ex:
        conn.rollback()
        session['op_message'] = ('error', f'فشلت العملية: {ex}')
    finally:
        conn.close()
    return redirect(url_for('dev.dev_dashboard'))