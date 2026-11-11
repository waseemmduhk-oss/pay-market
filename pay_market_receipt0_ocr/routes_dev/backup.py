from flask import request, session, redirect, url_for, current_app, render_template, make_response
import sqlite3, os, shutil, time, html as html_lib
from datetime import datetime
from .common import dev_bp, get_db_connection, write_dev_audit, _require_dev_login, DATABASE, BACKUP_DIR

@dev_bp.route('/backup', methods=['POST'])
def create_backup():
    if not _require_dev_login(): return "غير مسموح", 403
    current_app.validate_csrf()
    os.makedirs(BACKUP_DIR, exist_ok=True)
    ts   = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = os.path.join(BACKUP_DIR, f"hotspot_backup_{ts}.db")
    try:
        src = sqlite3.connect(DATABASE)
        dst = sqlite3.connect(path)
        src.backup(dst); src.close(); dst.close()
        write_dev_audit("BACKUP_MANUAL", path)
        session['op_message'] = ('success', f'تم إنشاء النسخة الاحتياطية: hotspot_backup_{ts}.db')
    except Exception as ex:
        session['op_message'] = ('error', f'فشل: {ex}')
    return redirect(url_for('dev.dev_backups'))

@dev_bp.route('/backups')
def dev_backups():
    if not _require_dev_login():
        return redirect(url_for('dev.dev_login'))
    return render_template('dev_backups.html', csrf_token=current_app.generate_csrf_token(),
                            op_message=session.pop('op_message', None))


@dev_bp.route('/backup/list')
def list_backups():
    if not _require_dev_login(): return "غير مسموح", 403
    os.makedirs(BACKUP_DIR, exist_ok=True)
    files = sorted([f for f in os.listdir(BACKUP_DIR) if f.endswith('.db')], reverse=True)
    rows  = ""
    for fname in files[:30]:
        fpath = os.path.join(BACKUP_DIR, fname)
        size  = os.path.getsize(fpath) // 1024
        mtime = datetime.fromtimestamp(os.path.getmtime(fpath)).strftime("%Y-%m-%d %H:%M")
        # الاسم قد يأتي من ملف مرفوع يدوياً، فننظّفه قبل إدراجه بالـ HTML أو JS
        fname_safe    = html_lib.escape(fname, quote=True)
        fname_js_safe = fname.replace('\\', '\\\\').replace("'", "\\'")
        rows += f'''<tr class="border-b dark:border-[#3A506B]/20 text-xs">
            <td class="p-2 font-mono text-slate-600 dark:text-slate-400">{fname_safe}</td>
            <td class="p-2 text-center text-slate-500">{mtime}</td>
            <td class="p-2 text-center text-slate-500">{size} KB</td>
            <td class="p-2 text-center flex gap-2 justify-center">
                <a href="/dev/backup/download/{fname_safe}" class="text-indigo-600 dark:text-indigo-400 font-bold hover:underline">⬇ تنزيل</a>
                <span class="text-slate-300">|</span>
                <button onclick="restoreBackup('{fname_js_safe}')" class="text-amber-600 dark:text-amber-400 font-bold hover:underline">↩ استرجاع</button>
                <span class="text-slate-300">|</span>
                <button onclick="deleteBackup('{fname_js_safe}')" class="text-red-500 dark:text-red-400 font-bold hover:underline">🗑 حذف</button>
            </td>
        </tr>'''
    if not rows:
        rows = '<tr><td colspan="4" class="text-center text-xs text-slate-400 py-4">لا توجد نسخ احتياطية.</td></tr>'
    return f'''<table class="w-full text-right">
        <thead><tr class="text-[11px] text-slate-400 border-b dark:border-[#3A506B]/30">
            <th class="p-2">اسم الملف</th><th class="p-2 text-center">التاريخ</th>
            <th class="p-2 text-center">الحجم</th><th class="p-2 text-center">إجراءات</th>
        </tr></thead><tbody>{rows}</tbody></table>'''

@dev_bp.route('/backup/download/<filename>')
def download_backup(filename):
    if not _require_dev_login(): return "غير مسموح", 403
    safe = os.path.basename(filename)
    path = os.path.join(BACKUP_DIR, safe)
    if not os.path.exists(path) or not safe.endswith('.db'):
        return "الملف غير موجود", 404
    write_dev_audit("BACKUP_DOWNLOAD", safe)
    with open(path, 'rb') as f: data = f.read()
    out = make_response(data)
    out.headers["Content-Disposition"] = f"attachment; filename={safe}"
    out.headers["Content-type"]        = "application/octet-stream"
    out.headers["Cache-Control"]       = "no-store"
    return out

@dev_bp.route('/backup/restore', methods=['POST'])
def restore_backup():
    if not _require_dev_login(): return "غير مسموح", 403
    current_app.validate_csrf()
    filename = request.form.get('filename', '')
    safe     = os.path.basename(filename)
    path     = os.path.join(BACKUP_DIR, safe)
    if not safe.endswith('.db') or not os.path.exists(path):
        session['op_message'] = ('error', 'النسخة الاحتياطية غير موجودة.')
        return redirect(url_for('dev.dev_backups'))
    try:
        # التأكد إن الملف يحتوي على الجداول الأساسية المتوقعة قبل الاستبدال
        check_conn = sqlite3.connect(path)
        tables = {r[0] for r in check_conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        check_conn.close()
        required = {'vouchers', 'transactions', 'operators', 'operator_admins'}
        if not required.issubset(tables):
            session['op_message'] = ('error', 'الملف لا يحتوي على هيكل قاعدة بيانات متوافق. تم إلغاء الاسترجاع.')
            return redirect(url_for('dev.dev_backups'))

        ts        = datetime.now().strftime("%Y%m%d_%H%M%S")
        pre_path  = os.path.join(BACKUP_DIR, f"before_restore_{ts}.db")
        src = sqlite3.connect(DATABASE)
        dst = sqlite3.connect(pre_path)
        src.backup(dst); src.close(); dst.close()

        src2 = sqlite3.connect(path)
        dst2 = sqlite3.connect(DATABASE)
        src2.backup(dst2); src2.close(); dst2.close()
        write_dev_audit("RESTORE_BACKUP", f"استُرجع من: {safe}")
        session['op_message'] = ('success', f'تم استرجاع النسخة "{safe}" بنجاح. النسخة السابقة حُفظت.')
    except Exception as ex:
        session['op_message'] = ('error', f'فشل الاسترجاع: {ex}')
    return redirect(url_for('dev.dev_backups'))

@dev_bp.route('/backup/upload', methods=['POST'])
def upload_backup():
    if not _require_dev_login(): return "غير مسموح", 403
    current_app.validate_csrf()
    file = request.files.get('backup_file')
    if not file or not file.filename:
        session['op_message'] = ('error', 'لم يتم اختيار ملف.')
        return redirect(url_for('dev.dev_backups'))
    safe_name = os.path.basename(file.filename or '')
    if not safe_name.endswith('.db'):
        session['op_message'] = ('error', 'يُقبل فقط ملفات .db')
        return redirect(url_for('dev.dev_backups'))
    file.seek(0, 2); size = file.tell(); file.seek(0)
    if size > 20 * 1024 * 1024:
        session['op_message'] = ('error', 'حجم الملف يتجاوز 20MB.')
        return redirect(url_for('dev.dev_backups'))
    os.makedirs(BACKUP_DIR, exist_ok=True)
    ts        = datetime.now().strftime("%Y%m%d_%H%M%S")
    dest_name = safe_name if not os.path.exists(os.path.join(BACKUP_DIR, safe_name)) else f"uploaded_{ts}_{safe_name}"
    dest_path = os.path.join(BACKUP_DIR, dest_name)
    try:
        file.save(dest_path)
        test_conn = sqlite3.connect(dest_path)
        test_conn.execute("SELECT 1"); test_conn.close()
        write_dev_audit("BACKUP_UPLOADED", f"تم رفع: {dest_name}")
        session['op_message'] = ('success', f'تم رفع النسخة الاحتياطية: {dest_name}')
    except Exception as ex:
        if os.path.exists(dest_path): os.remove(dest_path)
        session['op_message'] = ('error', f'الملف تالف أو غير صالح: {ex}')
    return redirect(url_for('dev.dev_backups'))

@dev_bp.route('/backup/delete', methods=['POST'])
def delete_backup():
    if not _require_dev_login(): return "غير مسموح", 403
    current_app.validate_csrf()
    filename = request.form.get('filename', '')
    safe     = os.path.basename(filename)
    path     = os.path.join(BACKUP_DIR, safe)
    if not safe.endswith('.db') or not os.path.exists(path):
        session['op_message'] = ('error', 'النسخة غير موجودة.')
        return redirect(url_for('dev.dev_backups'))
    try:
        os.remove(path)
        write_dev_audit("BACKUP_DELETED", f"تم حذف: {safe}")
        session['op_message'] = ('success', f'تم حذف النسخة: {safe}')
    except Exception as ex:
        session['op_message'] = ('error', f'فشل الحذف: {ex}')
    return redirect(url_for('dev.dev_backups'))

