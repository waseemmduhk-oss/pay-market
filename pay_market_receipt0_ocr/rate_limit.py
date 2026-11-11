import time


def check_and_hit(conn, key, max_attempts, lockout_duration):
    """
    قراءة الحالة الحالية لمفتاح معيّن (بدون زيادة العدّاد) — تُستخدم لعرض
    حالة القفل عند تحميل صفحة الدخول (GET) قبل أي محاولة جديدة.
    """
    now = time.time()
    row = conn.execute('SELECT count, locked_until FROM rate_limits WHERE key=?', (key,)).fetchone()
    count        = row['count'] if row else 0
    locked_until = row['locked_until'] if row else 0
    locked       = now < locked_until
    return {
        'locked': locked,
        'remaining': max(0, int(locked_until - now)),
        'count': count,
        'attempts_left': max(0, max_attempts - count),
    }


def register_failure(conn, key, max_attempts, lockout_duration):
    """يسجّل محاولة فاشلة، ويجمّد المفتاح مؤقتاً لو تجاوز الحد المسموح."""
    now = time.time()
    row = conn.execute('SELECT count, locked_until FROM rate_limits WHERE key=?', (key,)).fetchone()
    count        = (row['count'] if row else 0) + 1
    locked_until = row['locked_until'] if row else 0
    if count >= max_attempts:
        locked_until = now + lockout_duration
    conn.execute('''
        INSERT INTO rate_limits (key, count, window_start, locked_until) VALUES (?, ?, ?, ?)
        ON CONFLICT(key) DO UPDATE SET count=excluded.count, locked_until=excluded.locked_until
    ''', (key, count, now, locked_until))
    conn.commit()
    return {
        'locked': now < locked_until,
        'remaining': max(0, int(locked_until - now)),
        'count': count,
        'attempts_left': max(0, max_attempts - count),
    }


def reset(conn, key):
    """يمسح عدّاد مفتاح معيّن (يُستدعى عادة بعد نجاح تسجيل الدخول/التحقق)."""
    conn.execute('DELETE FROM rate_limits WHERE key=?', (key,))
    conn.commit()


def check_locked(conn, key):
    """فحص سريع للقفل فقط (بدون تفاصيل العدّاد) — يرجّع (locked, remaining_seconds)."""
    now = time.time()
    row = conn.execute('SELECT locked_until FROM rate_limits WHERE key=?', (key,)).fetchone()
    locked_until = row['locked_until'] if row else 0
    return (now < locked_until), max(0, int(locked_until - now))


def hit_sliding_window(conn, key, limit, window_seconds):
    """
    عدّاد نافذة منزلقة بسيطة (لتحديد عدد الطلبات خلال مدة زمنية، كطلبات
    الشراء). يزيد العدّاد ويرجّع True لو تجاوز الحد المسموح خلال النافذة.
    """
    now = time.time()
    row = conn.execute('SELECT count, window_start FROM rate_limits WHERE key=?', (key,)).fetchone()
    if row and (now - row['window_start']) <= window_seconds:
        count        = row['count'] + 1
        window_start = row['window_start']
    else:
        count        = 1
        window_start = now
    conn.execute('''
        INSERT INTO rate_limits (key, count, window_start, locked_until) VALUES (?, ?, ?, 0)
        ON CONFLICT(key) DO UPDATE SET count=excluded.count, window_start=excluded.window_start, locked_until=0
    ''', (key, count, window_start))
    conn.commit()
    return count > limit
