from flask import request, session, redirect, url_for, current_app, jsonify, g, render_template
import time
import rate_limit
from .common import (
    admin_bp, get_db_connection, get_admin_credentials, verify_admin_password,
    write_audit, ADMIN_SECRET_URL, MAX_LOGIN_ATTEMPTS, LOCKOUT_DURATION,
)


# ── تسجيل الدخول ─────────────────────────────────────────────────────────────
@admin_bp.route(f'/<slug>/admin/{ADMIN_SECRET_URL}', methods=['GET', 'POST'])
def admin_login(slug):
    operator    = g.operator
    operator_id = operator['id']
    # مفتاح فريد لكل (IP + مشغّل) لمنع محاولات فاشلة عند مشغّل من التأثير على آخر
    rl_key = f"admin_login:{request.remote_addr or 'unknown'}:{operator_id}"
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
        ADMIN_USERNAME, _ = get_admin_credentials(operator_id)

        if ADMIN_USERNAME and username == ADMIN_USERNAME and verify_admin_password(operator_id, password):
            rl_conn = get_db_connection()
            rate_limit.reset(rl_conn, rl_key)
            rl_conn.close()
            session.clear()
            session['admin_logged_in'] = True
            session['operator_id']     = operator_id
            session['operator_slug']   = slug
            session['login_time']      = time.time()
            session.permanent          = True
            write_audit("LOGIN_SUCCESS", "تسجيل دخول ناجح", actor=username, operator_id=operator_id)
            return redirect(url_for('admin.admin_dashboard', slug=slug))
        else:
            rl_conn = get_db_connection()
            state   = rate_limit.register_failure(rl_conn, rl_key, MAX_LOGIN_ATTEMPTS, LOCKOUT_DURATION)
            rl_conn.close()
            if state['locked']:
                write_audit("LOGIN_LOCKOUT", f"تجميد بعد {MAX_LOGIN_ATTEMPTS} محاولات", actor=username or "?", operator_id=operator_id)
            else:
                write_audit("LOGIN_FAIL", f"محاولة {state['count']}/{MAX_LOGIN_ATTEMPTS}", actor=username or "?", operator_id=operator_id)
            locked        = state['locked']
            remaining     = state['remaining']
            attempts_left = state['attempts_left']
            shake_class   = "shake"

    return render_template('admin_login.html',
        locked=locked, remaining=remaining,
        attempts_left=attempts_left, attempts_count=state["count"],
        shake_class=shake_class,
        operator_name=operator['name'],
        login_message=session.pop('login_message', None),
        form_disabled='pointer-events-none opacity-40 select-none' if locked else '',
        btn_label='الوصول مقيّد مؤقتاً' if locked else 'دخول آمن')


# ── API: وقت الجلسة المتبقي (للـ heartbeat) ──────────────────────────────────
@admin_bp.route('/<slug>/admin/session-ping', methods=['POST'])
def session_ping(slug):
    """
    يُعيد الوقت المتبقي الحقيقي حسب وقت تسجيل الدخول (login_time) — قراءة فقط،
    لا يُمدّد الجلسة أبداً. لو انتهت، نمسح الجلسة فعلياً هون بدل ما نعتمد فقط
    على before_request في الطلب القادم.
    """
    if not session.get('admin_logged_in') or session.get('operator_slug') != slug:
        return jsonify({'remaining': 0, 'expired': True})
    login_time = session.get('login_time', time.time())
    remaining  = max(0, int(1800 - (time.time() - login_time)))
    if remaining <= 0:
        session.clear()
        return jsonify({'remaining': 0, 'expired': True})
    return jsonify({'remaining': remaining, 'expired': False})


# ── تسجيل الخروج ─────────────────────────────────────────────────────────────
@admin_bp.route('/<slug>/admin/logout')
def logout(slug):
    if session.get('admin_logged_in') and session.get('operator_slug') == slug:
        write_audit("LOGOUT", "تسجيل خروج", operator_id=g.operator['id'])
    session.clear()
    session['login_message'] = ('success', 'تم تسجيل الخروج بنجاح.')
    return redirect(url_for('admin.admin_login', slug=slug))
