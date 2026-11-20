import os
import re
import sqlite3

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
SQLITE_PATH = os.path.join(BASE_DIR, "hotspot_cards.db")
DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()

if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = "postgresql://" + DATABASE_URL[len("postgres://"):]

if DATABASE_URL and "sslmode=" not in DATABASE_URL:
    sep = "&" if "?" in DATABASE_URL else "?"
    DATABASE_URL = f"{DATABASE_URL}{sep}sslmode=require"

IS_POSTGRES = bool(DATABASE_URL)

if IS_POSTGRES:
    import psycopg
    from psycopg.rows import dict_row


class DictOrIndexRow(dict):
    """صف يدعم الوصول بالاسم row['col'] وبالفهرس row[0] لتوافق كود SQLite."""

    def __init__(self, mapping):
        super().__init__(mapping)
        self._values = list(mapping.values())

    def __getitem__(self, key):
        if isinstance(key, int):
            return self._values[key]
        return super().__getitem__(key)


def _adapt_sql_to_pg(sql: str) -> str:
    """تحويل صياغة استعلامات SQLite الشائعة إلى صياغة متوافقة مع PostgreSQL."""
    q = sql.strip()

    q = re.sub(
        r"\bINTEGER\s+PRIMARY\s+KEY\s+AUTOINCREMENT\b",
        "SERIAL PRIMARY KEY",
        q,
        flags=re.IGNORECASE,
    )

    out = []
    in_single_quote = False
    i = 0
    while i < len(q):
        ch = q[i]
        if ch == "'":
            if in_single_quote and i + 1 < len(q) and q[i + 1] == "'":
                out.append("''")
                i += 2
                continue
            in_single_quote = not in_single_quote
            out.append(ch)
        elif ch == "?" and not in_single_quote:
            out.append("%s")
        else:
            out.append(ch)
        i += 1
    q = "".join(out)

    q = re.sub(
        r"date\('now',\s*%s\)",
        "(CURRENT_DATE + (%s)::interval)",
        q,
        flags=re.IGNORECASE,
    )
    q = re.sub(r"\bdate\(([^)]+)\)", r"(\1)::date", q, flags=re.IGNORECASE)

    q = re.sub(
        r'(=|!=|<>|\bLIKE\s+)"([a-z0-9_ -]+)"',
        r"\1'\2'",
        q,
        flags=re.IGNORECASE,
    )

    return q


class PgCursorWrapper:
    def __init__(self, cur, is_insert=False):
        self._cur = cur
        self.lastrowid = None
        if is_insert and cur.description:
            row = cur.fetchone()
            if row:
                first_val = next(iter(row.values())) if isinstance(row, dict) else row[0]
                self.lastrowid = first_val

    @property
    def rowcount(self):
        return self._cur.rowcount

    def execute(self, sql: str, params=None):
        adapted = _adapt_sql_to_pg(sql)
        stripped_upper = adapted.lstrip().upper()
        is_insert = (
            stripped_upper.startswith("INSERT INTO")
            and "RETURNING" not in stripped_upper
            and "ON CONFLICT" not in stripped_upper
        )
        self.lastrowid = None
        if is_insert:
            adapted_with_ret = f"{adapted.rstrip().rstrip(';')} RETURNING id"
            try:
                self._cur.execute(adapted_with_ret, params or ())
                if self._cur.description:
                    row = self._cur.fetchone()
                    if row:
                        self.lastrowid = next(iter(row.values())) if isinstance(row, dict) else row[0]
                return self
            except Exception:
                self._cur.execute(adapted, params or ())
                return self

        self._cur.execute(adapted, params or ())
        return self

    def fetchone(self):
        row = self._cur.fetchone()
        if row is None:
            return None
        return DictOrIndexRow(row) if not isinstance(row, DictOrIndexRow) else row

    def fetchall(self):
        rows = self._cur.fetchall()
        return [DictOrIndexRow(r) if not isinstance(r, DictOrIndexRow) else r for r in rows]

    def __iter__(self):
        for r in self._cur:
            yield DictOrIndexRow(r) if not isinstance(r, DictOrIndexRow) else r


class PgConnWrapper:
    def __init__(self, pg_conn):
        self._conn = pg_conn

    def execute(self, sql: str, params=None):
        adapted = _adapt_sql_to_pg(sql)
        stripped_upper = adapted.lstrip().upper()
        is_insert = (
            stripped_upper.startswith("INSERT INTO")
            and "RETURNING" not in stripped_upper
            and "ON CONFLICT" not in stripped_upper
        )
        if is_insert:
            adapted_with_ret = f"{adapted.rstrip().rstrip(';')} RETURNING id"
            try:
                cur = self._conn.cursor(row_factory=dict_row)
                cur.execute(adapted_with_ret, params or ())
                return PgCursorWrapper(cur, is_insert=True)
            except Exception:
                cur = self._conn.cursor(row_factory=dict_row)
                cur.execute(adapted, params or ())
                return PgCursorWrapper(cur, is_insert=False)

        cur = self._conn.cursor(row_factory=dict_row)
        cur.execute(adapted, params or ())
        return PgCursorWrapper(cur, is_insert=False)

    def cursor(self):
        return PgCursorWrapper(self._conn.cursor(row_factory=dict_row))

    def commit(self):
        self._conn.commit()

    def rollback(self):
        self._conn.rollback()

    def close(self):
        self._conn.close()


def get_db_connection():
    """إرجاع اتصال قاعدة بيانات موحّد (PostgreSQL/Supabase إذا وُجد DATABASE_URL، وإلا SQLite)."""
    if IS_POSTGRES:
        pg_conn = psycopg.connect(DATABASE_URL)
        return PgConnWrapper(pg_conn)

    os.makedirs(os.path.dirname(SQLITE_PATH), exist_ok=True)
    conn = sqlite3.connect(SQLITE_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def get_table_columns(conn, table_name: str) -> set:
    """إرجاع أسماء أعمدة جدول معين بشكل متوافق مع SQLite و PostgreSQL."""
    if IS_POSTGRES:
        rows = conn.execute(
            """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_schema = 'public' AND table_name = ?
            """,
            (table_name,),
        ).fetchall()
        return {r["column_name"] for r in rows}

    rows = conn.execute(f"PRAGMA table_info({table_name})").fetchall()
    return {r[1] for r in rows}


def add_column_if_missing(conn, table_name: str, column_name: str, column_def: str):
    """إضافة عمود إلى جدول إذا لم يكن موجوداً مسبقاً."""
    existing = get_table_columns(conn, table_name)
    if column_name not in existing:
        conn.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_def}")
