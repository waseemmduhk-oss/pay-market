from flask import request, session, redirect, url_for, current_app, render_template, make_response
import time, sys, platform, shutil, flask, os, sqlite3
from datetime import datetime
from werkzeug.security import check_password_hash
import report_utils
from routes_payment import CLEANUP_TOKEN
from .common import (
    dev_bp, get_db_connection, write_dev_audit, _require_dev_login,
    DATABASE, BACKUP_DIR, LOW_STOCK_THRESHOLD,
)

# ── لوحة المطوّر الرئيسية ──────────────────────────────────────────────────
@dev_bp.route('/dashboard')
def dev_dashboard():
    if not _require_dev_login():
        return redirect(url_for('dev.dev_login'))
    conn = get_db_connection()
    operators = conn.execute('SELECT * FROM operators ORDER BY id ASC').fetchall()
    stats = {}
    for op in operators:
        total   = conn.execute('SELECT COUNT(*) FROM vouchers WHERE operator_id=?', (op['id'],)).fetchone()[0]
        avail   = conn.execute('SELECT COUNT(*) FROM vouchers WHERE operator_id=? AND status="available"', (op['id'],)).fetchone()[0]
        sold    = conn.execute('SELECT COUNT(*) FROM vouchers WHERE operator_id=? AND status="sold"', (op['id'],)).fetchone()[0]
        # نعرض الإيراد التراكمي المحفوظ على operators.lifetime_revenue،
        revenue = op['lifetime_revenue']
        admin_row = conn.execute('SELECT username FROM operator_admins WHERE operator_id=?', (op['id'],)).fetchone()
        low_stock_count = conn.execute('''
            SELECT COUNT(*) FROM (
                SELECT price, COUNT(*) AS c FROM vouchers
                WHERE operator_id=? AND status="available" GROUP BY price, profile HAVING c < ?
            )
        ''', (op['id'], LOW_STOCK_THRESHOLD)).fetchone()[0]
        stats[op['id']] = {
            'total': total, 'available': avail, 'sold': sold,
            'revenue': revenue, 'admin_username': admin_row['username'] if admin_row else None,
            'low_stock_count': low_stock_count
        }
    global_total    = conn.execute('SELECT COUNT(*) FROM vouchers').fetchone()[0]
    global_revenue  = conn.execute('SELECT COALESCE(SUM(lifetime_revenue),0) FROM operators').fetchone()[0]

    # ── تحسين الرسم البياني: يمكن تصفيته حسب مشغّل معيّن ومدى الأيام ──
    chart_operator_raw = request.args.get('chart_operator', '').strip()
    chart_operator_id  = int(chart_operator_raw) if chart_operator_raw.isdigit() else None
    try:
        chart_days = int(request.args.get('chart_days', 30))
    except ValueError:
        chart_days = 30
    chart_days = chart_days if chart_days in (7, 30, 90) else 30

    revenue_series = report_utils.daily_revenue_series(conn, operator_id=chart_operator_id, days=chart_days)
    revenue_period_total = round(sum(p['revenue'] for p in revenue_series), 2)
    revenue_period_avg   = round(revenue_period_total / chart_days, 2) if chart_days else 0
    chart_operator_name  = None
    if chart_operator_id:
        op_row = conn.execute('SELECT name FROM operators WHERE id=?', (chart_operator_id,)).fetchone()
        chart_operator_name = op_row['name'] if op_row else None

    # تنبيه أمني: هل لسه المطوّر على كلمة المرور الافتراضية المولّدة تلقائياً؟
    dev_row = conn.execute('SELECT password_hash FROM dev_admins WHERE id=1').fetchone()
    using_default_password = bool(dev_row and check_password_hash(dev_row['password_hash'], 'ChangeMe#2026'))
    conn.close()

    login_time        = session.get('login_time', time.time())
    session_remaining = max(0, int(1800 - (time.time() - login_time)))
    cleanup_url = request.host_url.rstrip('/') + url_for('payment.internal_cleanup_expired_orders') + f'?token={CLEANUP_TOKEN}'

    return render_template('dev_dashboard.html',
        operators=operators, stats=stats,
        global_total=global_total, global_revenue=global_revenue,
        revenue_series=revenue_series,
        revenue_series_max=max([p['revenue'] for p in revenue_series] + [1]),
        revenue_period_total=revenue_period_total, revenue_period_avg=revenue_period_avg,
        chart_operator_id=chart_operator_id, chart_operator_name=chart_operator_name, chart_days=chart_days,
        current_year=datetime.now().year, current_month=datetime.now().month,
        csrf_token=current_app.generate_csrf_token(),
        op_message=session.pop('op_message', None),
        using_default_password=using_default_password,
        cleanup_url=cleanup_url,
        session_minutes=session_remaining // 60, session_seconds=session_remaining % 60)

# ── تقرير شهري (Excel) — لمشغّل واحد أو لكل المشغّلين مجمّعين ──────────────
@dev_bp.route('/monthly-report')
def dev_monthly_report():
    if not _require_dev_login():
        return redirect(url_for('dev.dev_login'))
    try:
        year  = int(request.args.get('year',  datetime.now().year))
        month = int(request.args.get('month', datetime.now().month))
        if month < 1 or month > 12: raise ValueError
    except (ValueError, TypeError):
        return "شهر أو سنة غير صحيحة", 400

    operator_id_raw = request.args.get('operator_id', '').strip()
    operator_id = int(operator_id_raw) if operator_id_raw.isdigit() else None

    conn = get_db_connection()
    try:
        data = report_utils.build_monthly_report_workbook(conn, year, month, operator_id=operator_id)
    except ImportError:
        return "openpyxl غير مثبّت. شغّل: pip install openpyxl", 500
    finally:
        conn.close()

    label = f"operator{operator_id}" if operator_id else "all"
    write_dev_audit("MONTHLY_REPORT_EXPORT", f"{report_utils.month_label(year, month)} scope={label}")
    out = make_response(data)
    out.headers["Content-Disposition"] = f"attachment; filename=tagreer_{label}_{year}_{month:02d}.xlsx"
    out.headers["Content-type"]        = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    out.headers["Cache-Control"]       = "no-store"
    return out

# ── صحة النظام ───────────────────────────────────────────────────────────────
@dev_bp.route('/system-health')
def dev_system_health():
    if not _require_dev_login():
        return redirect(url_for('dev.dev_login'))
    conn = get_db_connection()

    db_size_mb = round(os.path.getsize(DATABASE) / (1024 * 1024), 2) if os.path.exists(DATABASE) else 0
    disk = shutil.disk_usage('.')
    disk_free_gb  = round(disk.free  / (1024 ** 3), 2)
    disk_total_gb = round(disk.total / (1024 ** 3), 2)
    disk_used_pct = round((disk.used / disk.total) * 100, 1) if disk.total else 0

    os.makedirs(BACKUP_DIR, exist_ok=True)
    backup_files = [f for f in os.listdir(BACKUP_DIR) if f.endswith('.db')]
    backups_size_mb = round(sum(os.path.getsize(os.path.join(BACKUP_DIR, f)) for f in backup_files) / (1024 * 1024), 2)
    last_backup_time = None
    if backup_files:
        newest = max(backup_files, key=lambda f: os.path.getmtime(os.path.join(BACKUP_DIR, f)))
        last_backup_time = datetime.fromtimestamp(os.path.getmtime(os.path.join(BACKUP_DIR, newest)))
    days_since_backup = (datetime.now() - last_backup_time).days if last_backup_time else None

    operators_active   = conn.execute('SELECT COUNT(*) FROM operators WHERE is_active=1').fetchone()[0]
    operators_inactive  = conn.execute('SELECT COUNT(*) FROM operators WHERE is_active=0').fetchone()[0]
    total_vouchers      = conn.execute('SELECT COUNT(*) FROM vouchers').fetchone()[0]
    total_transactions  = conn.execute('SELECT COUNT(*) FROM transactions').fetchone()[0]
    total_audit_rows    = conn.execute('SELECT COUNT(*) FROM audit_log').fetchone()[0]

    dev_row = conn.execute('SELECT password_hash FROM dev_admins WHERE id=1').fetchone()
    using_default_password = bool(dev_row and check_password_hash(dev_row['password_hash'], 'ChangeMe#2026'))

    # عدد محاولات الدخول الفاشلة بآخر 24 ساعة (كل اللوحات مجمّعة)
    failed_logins_24h = conn.execute('''
        SELECT COUNT(*) FROM audit_log
        WHERE action IN ('LOGIN_FAIL','LOGIN_LOCKOUT','DEV_LOGIN_FAIL','DEV_LOGIN_LOCKOUT')
        AND event_time >= datetime('now', '-1 day')
    ''').fetchone()[0]
    conn.close()

    return render_template('dev_system_health.html',
        db_size_mb=db_size_mb, disk_free_gb=disk_free_gb, disk_total_gb=disk_total_gb, disk_used_pct=disk_used_pct,
        backups_count=len(backup_files), backups_size_mb=backups_size_mb,
        last_backup_time=last_backup_time, days_since_backup=days_since_backup,
        operators_active=operators_active, operators_inactive=operators_inactive,
        total_vouchers=total_vouchers, total_transactions=total_transactions, total_audit_rows=total_audit_rows,
        using_default_password=using_default_password,
        failed_logins_24h=failed_logins_24h,
        python_version=sys.version.split()[0], flask_version=flask.__version__,
        sqlite_version=sqlite3.sqlite_version, platform_info=platform.platform())

