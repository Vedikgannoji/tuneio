"""SQLite persistence. Every operation has its own connection and transaction."""
import sqlite3
from contextlib import contextmanager


REPORT_SQL = "\n                SELECT p.id AS product_id, p.name, COUNT(o.id) AS order_count,\n                       COALESCE(SUM(o.quantity),0) AS units,\n                       COALESCE(SUM(o.quantity*o.unit_price_cents),0) AS revenue_cents\n                FROM products p LEFT JOIN orders o ON o.product_id=p.id AND o.status!='cancelled'\n                GROUP BY p.id ORDER BY revenue_cents DESC, p.id\n            "


class Database:
    def __init__(self, path):
        self.path = str(path)
        with self.connect() as conn:
            conn.executescript('''
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY, name TEXT NOT NULL, email TEXT NOT NULL UNIQUE);
                CREATE TABLE IF NOT EXISTS products(id INTEGER PRIMARY KEY, name TEXT NOT NULL, price_cents INTEGER NOT NULL CHECK(price_cents>0));
                CREATE TABLE IF NOT EXISTS orders(id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id),
                    product_id INTEGER NOT NULL REFERENCES products(id), quantity INTEGER NOT NULL CHECK(quantity>0),
                    unit_price_cents INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'pending');
                CREATE INDEX IF NOT EXISTS orders_sales_idx ON orders(product_id, status, quantity, unit_price_cents);
            ''')

    @contextmanager
    def connect(self):
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA foreign_keys=ON')
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def list(self, table, limit, offset):
        with self.connect() as conn:
            return [dict(r) for r in conn.execute(f'SELECT * FROM {table} ORDER BY id LIMIT ? OFFSET ?', (limit, offset))]

    def get(self, table, item_id):
        with self.connect() as conn:
            row = conn.execute(f'SELECT * FROM {table} WHERE id=?', (item_id,)).fetchone()
            return dict(row) if row else None

    def create(self, table, values):
        with self.connect() as conn:
            if table == 'orders':
                product = conn.execute('SELECT price_cents FROM products WHERE id=?', (values['product_id'],)).fetchone()
                if product is None:
                    raise sqlite3.IntegrityError('Unknown product')
                values = dict(values, unit_price_cents=product[0])
            columns = ','.join(values)
            placeholders = ','.join('?' for _ in values)
            cursor = conn.execute(f'INSERT INTO {table} ({columns}) VALUES ({placeholders})', tuple(values.values()))
            return dict(conn.execute(f'SELECT * FROM {table} WHERE id=?', (cursor.lastrowid,)).fetchone())

    def update(self, table, item_id, values):
        with self.connect() as conn:
            assignments = ','.join(f'{column}=?' for column in values)
            conn.execute(f'UPDATE {table} SET {assignments} WHERE id=?', (*values.values(), item_id))
            row = conn.execute(f'SELECT * FROM {table} WHERE id=?', (item_id,)).fetchone()
            return dict(row) if row else None

    def report(self):
        with self.connect() as conn:
            return [dict(r) for r in conn.execute(REPORT_SQL)]
