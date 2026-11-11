from flask import request, session, redirect, url_for, current_app, render_template, jsonify
import time
from werkzeug.security import generate_password_hash, check_password_hash
import rate_limit
from .common import (
    dev_bp, get_db_connection, write_dev_audit, _require_dev_login,
    MAX_LOGIN_ATTEMPTS, LOCKOUT_DURATION,
)

# ── تسجيل الدخول ─────────────────────────────────────────────────────────────
@dev_bp.route('/login', methods=['GET', 'POST'])
def dev_login():
    ip     = request.remote_addr or 'unknown'
    rl_key = f"dev_login:{ip}"
    rl_conn = get_db_connection()
    state   = rate_limit.check_and_hit(rl_conn, rl_key, MAX_LOGIN_ATTEMPTS, LOCKOUT_DURATION)
    rl_conn.close()
    locked        = state['locked']
    remaining     = state['remaining']
    attempts_left = state['attempts_left']
    shake_class   = ""

    if request.method == 'POST' and not locked:
        current_app.validate_csrf()
        username = request.form.get('username', '')
        password = request.form.get('password', '')
        conn = get_db_connection()
        row  = conn.execute('SELECT username, password_hash FROM dev_admins WHERE id=1').fetchone()
        conn.close()
        if row and username == row['username'] and check_password_hash(row['password_hash'], password):
            rl_conn = get_db_connection()
            rate_limit.reset(rl_conn, rl_key)
            rl_conn.close()
            session.clear()
            session['dev_logged_in'] = True
            session['login_time']    = time.time()
            session.permanent        = True
            write_dev_audit("DEV_LOGIN_SUCCESS", "تسجيل دخول مطوّر ناجح")
            return redirect(url_for('dev.dev_dashboard'))
        else:
            rl_conn = get_db_connection()
            state   = rate_limit.register_failure(rl_conn, rl_key, MAX_LOGIN_ATTEMPTS, LOCKOUT_DURATION)
            rl_conn.close()
            if state['locked']:
                write_dev_audit("DEV_LOGIN_LOCKOUT", f"تجميد بعد {MAX_LOGIN_ATTEMPTS} محاولات")
            else:
                write_dev_audit("DEV_LOGIN_FAIL", f"محاولة {state['count']}/{MAX_LOGIN_ATTEMPTS}")
            locked        = state['locked']
            remaining     = state['remaining']
            attempts_left = state['attempts_left']
            shake_class   = "shake"

    return render_template('dev_login.html',
        locked=locked, remaining=remaining,
        attempts_left=attempts_left, attempts_count=state["count"],
        shake_class=shake_class,
        login_message=session.pop('login_message', None),
        form_disabled='pointer-events-none opacity-40 select-none' if locked else '',
        btn_label='الوصول مقيّد مؤقتاً' if locked else 'دخول لوحة المطوّر')

# ── تسجيل الخروج ─────────────────────────────────────────────────────────────
@dev_bp.route('/logout')
def dev_logout():
    if _require_dev_login():
        write_dev_audit("DEV_LOGOUT", "تسجيل خروج مطوّر")
    session.clear()
    # نضع الرسالة بعد المسح مباشرة حتى تُحفظ في كوكي الجلسة الجديد (الفارغ)
    # وتظهر لمرة واحدة فقط في صفحة تسجيل الدخول التالية
    session['login_message'] = ('success', 'تم تسجيل الخروج بنجاح.')
    return redirect(url_for('dev.dev_login'))


# ── تغيير اسم مستخدم دخول المطوّر ─────────────────────────────────────────────
@dev_bp.route('/settings/change-username', methods=['POST'])
def dev_change_username():
    if not _require_dev_login():
        return redirect(url_for('dev.dev_login'))
    current_app.validate_csrf()
    cur_pass     = request.form.get('current_password', '')
    new_username = request.form.get('new_username', '').strip()

    conn = get_db_connection()
    row  = conn.execute('SELECT username, password_hash FROM dev_admins WHERE id=1').fetchone()

    if not row or not check_password_hash(row['password_hash'], cur_pass):
        session['op_message'] = ('error', 'كلمة المرور الحالية غير صحيحة.')
    elif not new_username:
        session['op_message'] = ('error', 'الرجاء إدخال اسم مستخدم جديد.')
    elif len(new_username) < 3:
        session['op_message'] = ('error', 'اسم المستخدم يجب أن يكون 3 أحرف على الأقل.')
    elif new_username == row['username']:
        session['op_message'] = ('error', 'هذا هو اسم المستخدم الحالي بالفعل.')
    else:
        conn.execute('UPDATE dev_admins SET username=? WHERE id=1', (new_username,))
        conn.commit()
        write_dev_audit("DEV_USERNAME_CHANGE", f"{row['username']} -> {new_username}")
        session['op_message'] = ('success', f'تم تحديث اسم مستخدم الدخول إلى "{new_username}".')
    conn.close()
    return redirect(url_for('dev.dev_dashboard'))


# ── تغيير كلمة مرور دخول المطوّر ──────────────────────────────────────────
@dev_bp.route('/settings/change-password', methods=['POST'])
def dev_change_password():
    if not _require_dev_login():
        return redirect(url_for('dev.dev_login'))
    current_app.validate_csrf()
    cur = request.form.get('current_password', '')
    new = request.form.get('new_password', '')
    cfm = request.form.get('confirm_password', '')

    conn = get_db_connection()
    row  = conn.execute('SELECT password_hash FROM dev_admins WHERE id=1').fetchone()

    if not row or not check_password_hash(row['password_hash'], cur):
        session['op_message'] = ('error', 'كلمة المرور الحالية غير صحيحة.')
    elif new != cfm:
        session['op_message'] = ('error', 'كلمة المرور الجديدة غير متطابقة.')
    elif len(new) < 8:
        session['op_message'] = ('error', 'كلمة المرور يجب أن تكون 8 أحرف على الأقل.')
    else:
        conn.execute('UPDATE dev_admins SET password_hash=? WHERE id=1', (generate_password_hash(new),))
        conn.commit()
        write_dev_audit("DEV_PASSWORD_CHANGE", "تغيير كلمة مرور الدخول")
        session['op_message'] = ('success', 'تم تحديث كلمة المرور بنجاح.')
    conn.close()
    return redirect(url_for('dev.dev_dashboard'))


# ── لوحة المطوّر الرئيسية ──────────────────────────────────────────────────
# ── API: وقت الجلسة المتبقي (للـ heartbeat بلوحة المطوّر) — قراءة فقط ────────
@dev_bp.route('/session-ping', methods=['POST'])
def dev_session_ping():
    if not _require_dev_login():
        return jsonify({'remaining': 0, 'expired': True})
    login_time = session.get('login_time', time.time())
    remaining  = max(0, int(1800 - (time.time() - login_time)))
    if remaining <= 0:
        session.clear()
        return jsonify({'remaining': 0, 'expired': True})
    return jsonify({'remaining': remaining, 'expired': False})