from flask import request, session, redirect, url_for, current_app, render_template
from .common import dev_bp, get_db_connection, write_dev_audit, _require_dev_login

# ── سجل تسجيلات الدخول الفاشلة المجمّع ──────────────────────────────────────
@dev_bp.route('/security')
def dev_security_log():
    if not _require_dev_login():
        return redirect(url_for('dev.dev_login'))
    conn = get_db_connection()
    failed_actions = ('LOGIN_FAIL', 'LOGIN_LOCKOUT', 'DEV_LOGIN_FAIL', 'DEV_LOGIN_LOCKOUT')
    placeholders = ','.join('?' * len(failed_actions))

    recent = conn.execute(f'''
        SELECT audit_log.*, operators.name AS operator_name
        FROM audit_log LEFT JOIN operators ON audit_log.operator_id = operators.id
        WHERE audit_log.action IN ({placeholders})
        ORDER BY audit_log.id DESC LIMIT 200
    ''', failed_actions).fetchall()

    # تجميع حسب IP لآخر 7 أيام — لرصد أي محاولات اختراق متكررة
    by_ip = conn.execute(f'''
        SELECT ip_address, COUNT(*) AS attempts, MAX(event_time) AS last_attempt
        FROM audit_log
        WHERE action IN ({placeholders}) AND event_time >= datetime('now', '-7 days')
        GROUP BY ip_address ORDER BY attempts DESC LIMIT 20
    ''', failed_actions).fetchall()
    conn.close()

    return render_template('dev_security_log.html', recent=recent, by_ip=by_ip,
        csrf_token=current_app.generate_csrf_token(), op_message=session.pop('op_message', None))

# ──مسح سجل محاولات الدخول الخاطئة──────────────────────────────────────────
@dev_bp.route('/security/clear', methods=['POST'])
def clear_security_log():
    if not _require_dev_login():
        return redirect(url_for('dev.dev_login'))
    current_app.validate_csrf()
    failed_actions = ('LOGIN_FAIL', 'LOGIN_LOCKOUT', 'DEV_LOGIN_FAIL', 'DEV_LOGIN_LOCKOUT')
    placeholders = ','.join('?' * len(failed_actions))
    conn = get_db_connection()
    count = conn.execute(f"SELECT COUNT(*) FROM audit_log WHERE action IN ({placeholders})", failed_actions).fetchone()[0]
    conn.execute(f"DELETE FROM audit_log WHERE action IN ({placeholders})", failed_actions)
    conn.commit()
    conn.close()
    write_dev_audit("SECURITY_LOG_CLEARED", f"تم مسح {count} محاولة دخول فاشلة")
    session['op_message'] = ('success', f'تم مسح سجل الأمان ({count} محاولة).')
    return redirect(url_for('dev.dev_security_log'))

# ── سجل النشاط الشامل (كل المشغّلين) ──────────────────────────────────────────
@dev_bp.route('/audit-log')
def dev_audit_log():
    if not _require_dev_login():
        return redirect(url_for('dev.dev_login'))
    conn = get_db_connection()
    logs = conn.execute('''
        SELECT audit_log.*, operators.name AS operator_name, operators.slug AS operator_slug
        FROM audit_log LEFT JOIN operators ON audit_log.operator_id = operators.id
        ORDER BY audit_log.id DESC LIMIT 300
    ''').fetchall()
    conn.close()
    return render_template('dev_audit_log.html', logs=logs,
        csrf_token=current_app.generate_csrf_token(),
        op_message=session.pop('op_message', None))

# ──مسح سجل نشاط لوحة المطور فقط──────────────────────────────────────────
@dev_bp.route('/audit-log/clear', methods=['POST'])
def clear_dev_audit_log():
    if not _require_dev_login():
        return redirect(url_for('dev.dev_login'))
    current_app.validate_csrf()
    conn = get_db_connection()
    count = conn.execute("SELECT COUNT(*) FROM audit_log WHERE operator_id IS NULL").fetchone()[0]
    conn.execute("DELETE FROM audit_log WHERE operator_id IS NULL")
    conn.commit()
    conn.close()
    write_dev_audit("AUDIT_LOG_CLEARED_DEV", f"تم مسح {count} سجل خاص بالمطوّر فقط")
    session['op_message'] = ('success', f'تم مسح سجل نشاط لوحة المطوّر ({count} سجل). سجلات المشغّلين لم تتأثر.')
    return redirect(url_for('dev.dev_audit_log'))