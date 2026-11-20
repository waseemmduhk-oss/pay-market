from flask import request, session, redirect, url_for, current_app, render_template, make_response
import json, html as html_lib
from datetime import datetime
from db_layer import IS_POSTGRES
from .common import dev_bp, get_db_connection, write_dev_audit, _require_dev_login

_BACKUP_TABLES = (
    'operators',
    'operator_admins',
    'vouchers',
    'transactions',
    'customers',
    'audit_log',
)


def _ensure_backups_table(conn):
    if IS_POSTGRES:
        conn.execute('''
            CREATE TABLE IF NOT EXISTS db_backups (
                id         SERIAL PRIMARY KEY,
                name       TEXT NOT NULL UNIQUE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                size_bytes INTEGER NOT NULL DEFAULT 0,
                payload    TEXT NOT NULL
            )
        ''')
    else:
        conn.execute('''
            CREATE TABLE IF NOT EXISTS db_backups (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                name       TEXT NOT NULL UNIQUE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                size_bytes INTEGER NOT NULL DEFAULT 0,
                payload    TEXT NOT NULL
            )
        ''')
    conn.commit()


def _sync_pg_sequence(conn, table_name):
    """يحدّث عدّاد الترقيم التلقائي في PostgreSQL بعد استعادة صفوف تحمل id محدد."""
    if not IS_POSTGRES:
        return
    try:
        conn.execute(
            """
            SELECT setval(
                pg_get_serial_sequence(?, 'id'),
                COALESCE((SELECT MAX(id) FROM """ + table_name + """), 1),
                true
            )
            """,
            (table_name,),
        )
    except Exception:
        pass


def _serialize_rows(rows):
    result = []
    for r in rows:
        item = {}
        for k in r.keys():
            val = r[k]
            if isinstance(val, datetime):
                item[k] = val.isoformat()
            else:
                item[k] = val
        result.append(item)
    return result


def _build_snapshot_payload(conn):
    data = {
        'version': 1,
        'created_at': datetime.utcnow().isoformat(),
        'tables': {},
    }
    for tbl in _BACKUP_TABLES:
        try:
            rows = conn.execute(f'SELECT * FROM {tbl}').fetchall()
            data['tables'][tbl] = _serialize_rows(rows)
        except Exception:
            data['tables'][tbl] = []
    return json.dumps(data, ensure_ascii=False, indent=2)


@dev_bp.route('/backup', methods=['POST'])
def create_backup():
    if not _require_dev_login():
        return "غير مسموح", 403
    current_app.validate_csrf()
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    name = f"supabase_backup_{ts}.json"
    conn = get_db_connection()
    try:
        _ensure_backups_table(conn)
        payload = _build_snapshot_payload(conn)
        size_bytes = len(payload.encode('utf-8'))
        conn.execute(
            'INSERT INTO db_backups (name, size_bytes, payload) VALUES (?,?,?)',
            (name, size_bytes, payload),
        )
        conn.commit()
        write_dev_audit("BACKUP_MANUAL", name)
        session['op_message'] = ('success', f'تم إنشاء النسخة الاحتياطية الدائمة: {name}')
    except Exception as ex:
        conn.rollback()
        session['op_message'] = ('error', f'فشل إنشاء النسخة: {ex}')
    finally:
        conn.close()
    return redirect(url_for('dev.dev_backups'))


@dev_bp.route('/backups')
def dev_backups():
    if not _require_dev_login():
        return redirect(url_for('dev.dev_login'))
    return render_template(
        'dev_backups.html',
        csrf_token=current_app.generate_csrf_token(),
        op_message=session.pop('op_message', None),
    )


@dev_bp.route('/backup/list')
def list_backups():
    if not _require_dev_login():
        return "غير مسموح", 403
    conn = get_db_connection()
    try:
        _ensure_backups_table(conn)
        files = conn.execute(
            'SELECT name, created_at, size_bytes FROM db_backups ORDER BY id DESC LIMIT 30'
        ).fetchall()
    finally:
        conn.close()

    rows = ""
    for r in files:
        fname = r['name']
        size_kb = max(1, int(r['size_bytes'] or 0) // 1024)
        created_raw = r['created_at']
        if isinstance(created_raw, datetime):
            mtime = created_raw.strftime("%Y-%m-%d %H:%M")
        else:
            mtime = str(created_raw or '')[:16]
        fname_safe = html_lib.escape(fname, quote=True)
        fname_js_safe = fname.replace('\\', '\\\\').replace("'", "\\'")
        rows += f'''<tr class="border-b dark:border-[#3A506B]/20 text-xs">
            <td class="p-2 font-mono text-slate-600 dark:text-slate-400">{fname_safe}</td>
            <td class="p-2 text-center text-slate-500">{html_lib.escape(mtime)}</td>
            <td class="p-2 text-center text-slate-500">{size_kb} KB</td>
            <td class="p-2 text-center flex gap-2 justify-center">
                <a href="/dev/backup/download/{fname_safe}" class="text-indigo-600 dark:text-indigo-400 font-bold hover:underline">⬇ تنزيل</a>
                <span class="text-slate-300">|</span>
                <button type="button" onclick="restoreBackup('{fname_js_safe}')" class="text-amber-600 dark:text-amber-400 font-bold hover:underline">↩ استرجاع</button>
                <span class="text-slate-300">|</span>
                <button type="button" onclick="deleteBackup('{fname_js_safe}')" class="text-red-500 dark:text-red-400 font-bold hover:underline">🗑 حذف</button>
            </td>
        </tr>'''

    if not rows:
        rows = '<tr><td colspan="4" class="text-center text-xs text-slate-400 py-4">لا توجد نسخ احتياطية محفوظة.</td></tr>'

    return f'''<table class="w-full text-right">
        <thead><tr class="text-[11px] text-slate-400 border-b dark:border-[#3A506B]/30">
            <th class="p-2">اسم النسخة</th><th class="p-2 text-center">التاريخ</th>
            <th class="p-2 text-center">الحجم</th><th class="p-2 text-center">إجراءات</th>
        </tr></thead><tbody>{rows}</tbody></table>'''


@dev_bp.route('/backup/download/<filename>')
def download_backup(filename):
    if not _require_dev_login():
        return "غير مسموح", 403
    conn = get_db_connection()
    try:
        _ensure_backups_table(conn)
        row = conn.execute(
            'SELECT name, payload FROM db_backups WHERE name = ?',
            (filename,),
        ).fetchone()
    finally:
        conn.close()

    if not row:
        return "الملف غير موجود", 404

    write_dev_audit("BACKUP_DOWNLOAD", row['name'])
    out = make_response(row['payload'].encode('utf-8'))
    out.headers["Content-Disposition"] = f"attachment; filename={row['name']}"
    out.headers["Content-type"] = "application/json; charset=utf-8"
    out.headers["Cache-Control"] = "no-store"
    return out


@dev_bp.route('/backup/restore', methods=['POST'])
def restore_backup():
    if not _require_dev_login():
        return "غير مسموح", 403
    current_app.validate_csrf()
    filename = request.form.get('filename', '').strip()
    if not filename:
        session['op_message'] = ('error', 'اسم النسخة غير صالح.')
        return redirect(url_for('dev.dev_backups'))

    conn = get_db_connection()
    try:
        _ensure_backups_table(conn)
        row = conn.execute(
            'SELECT name, payload FROM db_backups WHERE name = ?',
            (filename,),
        ).fetchone()
        if not row:
            session['op_message'] = ('error', 'النسخة الاحتياطية غير موجودة.')
            return redirect(url_for('dev.dev_backups'))

        snapshot = json.loads(row['payload'])
        tables_data = snapshot.get('tables') or {}
        if not {'operators', 'vouchers', 'transactions'}.issubset(set(tables_data.keys())):
            session['op_message'] = ('error', 'بنية النسخة الاحتياطية غير متوافقة.')
            return redirect(url_for('dev.dev_backups'))

        # حفظ نسخة أمان قبل الاسترجاع
        pre_name = f"before_restore_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        pre_payload = _build_snapshot_payload(conn)
        conn.execute(
            'INSERT INTO db_backups (name, size_bytes, payload) VALUES (?,?,?)',
            (pre_name, len(pre_payload.encode('utf-8')), pre_payload),
        )

        # استعادة الجداول بالترتيب الآمن للعلاقات الأجنبية
        for tbl in reversed(_BACKUP_TABLES):
            conn.execute(f'DELETE FROM {tbl}')

        for tbl in _BACKUP_TABLES:
            rows_to_insert = tables_data.get(tbl) or []
            for item in rows_to_insert:
                if not item:
                    continue
                cols = list(item.keys())
                placeholders = ','.join(['?'] * len(cols))
                col_sql = ','.join(cols)
                vals = [item[c] for c in cols]
                conn.execute(f'INSERT INTO {tbl} ({col_sql}) VALUES ({placeholders})', vals)
            _sync_pg_sequence(conn, tbl)

        conn.commit()
        write_dev_audit("RESTORE_BACKUP", f"استُرجع من: {filename}")
        session['op_message'] = ('success', f'تم استرجاع النسخة "{filename}" بنجاح مع حفظ نسخة احتياطية سابقة.')
    except Exception as ex:
        conn.rollback()
        session['op_message'] = ('error', f'فشل الاسترجاع: {ex}')
    finally:
        conn.close()
    return redirect(url_for('dev.dev_backups'))


@dev_bp.route('/backup/upload', methods=['POST'])
def upload_backup():
    if not _require_dev_login():
        return "غير مسموح", 403
    current_app.validate_csrf()
    file = request.files.get('backup_file')
    if not file or not file.filename:
        session['op_message'] = ('error', 'لم يتم اختيار ملف.')
        return redirect(url_for('dev.dev_backups'))

    try:
        raw = file.read().decode('utf-8')
        parsed = json.loads(raw)
        if not isinstance(parsed, dict) or 'tables' not in parsed:
            session['op_message'] = ('error', 'ملف النسخة الاحتياطية يجب أن يكون JSON صالحاً يحتوي على tables.')
            return redirect(url_for('dev.dev_backups'))

        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        name = f"uploaded_{ts}.json"
        conn = get_db_connection()
        try:
            _ensure_backups_table(conn)
            conn.execute(
                'INSERT INTO db_backups (name, size_bytes, payload) VALUES (?,?,?)',
                (name, len(raw.encode('utf-8')), raw),
            )
            conn.commit()
        finally:
            conn.close()

        write_dev_audit("BACKUP_UPLOADED", f"تم رفع: {name}")
        session['op_message'] = ('success', f'تم رفع النسخة الاحتياطية إلى قاعدة البيانات: {name}')
    except Exception as ex:
        session['op_message'] = ('error', f'الملف تالف أو غير صالح: {ex}')
    return redirect(url_for('dev.dev_backups'))


@dev_bp.route('/backup/delete', methods=['POST'])
def delete_backup():
    if not _require_dev_login():
        return "غير مسموح", 403
    current_app.validate_csrf()
    filename = request.form.get('filename', '').strip()
    if not filename:
        session['op_message'] = ('error', 'النسخة غير موجودة.')
        return redirect(url_for('dev.dev_backups'))

    conn = get_db_connection()
    try:
        _ensure_backups_table(conn)
        cur = conn.execute('DELETE FROM db_backups WHERE name = ?', (filename,))
        conn.commit()
        if cur.rowcount:
            write_dev_audit("BACKUP_DELETED", f"تم حذف: {filename}")
            session['op_message'] = ('success', f'تم حذف النسخة: {filename}')
        else:
            session['op_message'] = ('error', 'النسخة غير موجودة.')
    except Exception as ex:
        conn.rollback()
        session['op_message'] = ('error', f'فشل الحذف: {ex}')
    finally:
        conn.close()
    return redirect(url_for('dev.dev_backups'))
