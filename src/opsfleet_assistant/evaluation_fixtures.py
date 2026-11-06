"""Deterministic warehouse transport, executing compiled SQL over synthetic rows.

Only the warehouse dialect/transport is substituted. BigQueryTurn, sealed compiler,
current authorization, coverage, release validation, grounding and SQLite stay active.
This is not a BigQuery dialect-conformance test. No network client is constructed.
"""
from __future__ import annotations
from datetime import date, datetime, timezone
from decimal import Decimal, localcontext
from pathlib import Path
import re
import sqlite3
from uuid import uuid4

from google.cloud import bigquery
import sqlglot

DATASET = 'bigquery-public-data.thelook_ecommerce'
PRODUCTS = ((41001, 'Calvin Klein'), (41002, 'Carhartt'), (41003, 'Hanes'))
AS_OF = datetime(2026, 11, 5, 0, tzinfo=timezone.utc)


class DecimalSum:
    def __init__(self): self.value = Decimal(0); self.seen = False
    def step(self, value):
        if value is not None:
            self.seen = True
            with localcontext() as ctx:
                ctx.prec = 100
                self.value += Decimal(str(value))
    def finalize(self): return str(self.value) if self.seen else None


class FixtureJob:
    def __init__(self, rows=(), schema=(), *, dry=False):
        self.rows, self.schema = tuple(rows), tuple(schema)
        self.job_id = str(uuid4())
        self.total_bytes_processed = 0
        self.total_bytes_billed = 0
        self.ended = AS_OF
    def result(self, **kwargs): return self.rows
    def cancel(self, **kwargs): return True


class FixtureClient:
    """Real compiled SQL is run in an isolated in-memory SQLite warehouse.

    Decimal SUM is exact; average/rate division uses SQLite real arithmetic and is
    checked with explicit tolerance. Exact Decimal division/points have separate
    controlled regression cases. Fixture totals are independent authored operands.
    """
    def __init__(self, profile='standard'):
        self.db = sqlite3.connect(':memory:')
        self.db.create_aggregate('SUM', 1, DecimalSum)
        self.db.create_function('DATE_TRUNC', 2, lambda unit, value: str(value)[:7]+'-01')
        self.db.create_function('DATE', -1, lambda value, *args: str(value)[:10])
        self.db.executescript('''
        CREATE TABLE products(id INTEGER, brand TEXT);
        CREATE TABLE orders(order_id INTEGER, user_id INTEGER);
        CREATE TABLE users(id INTEGER, state TEXT, city TEXT, country TEXT, age INTEGER, gender TEXT, traffic_source TEXT);
        CREATE TABLE order_items(order_id INTEGER, product_id INTEGER, status TEXT, sale_price TEXT, created_at TEXT);
        ''')
        self.db.executemany('INSERT INTO products VALUES(?,?)', PRODUCTS)
        # All profiles share catalog. Add two allowed lines and an unauthorized
        # line in the SAME order, plus cancelled/null lines. Default period total
        # 30, Complete-only total 30; narrow first brand total 10. Never 120.
        if profile != 'empty':
            periods = ['2024-02-29', '2024-10-10', '2025-01-10', '2025-04-10',
                       '2025-07-10', '2025-10-10', '2025-12-10', '2026-10-10', '2026-11-10']
            if profile in {'increase', 'decline', 'equal', 'zero', 'precision'}:
                values = {
                    'increase': ('100', '200'), 'decline': ('200', '100'),
                    'equal': ('125.25', '125.25'), 'zero': ('0', '200'),
                    'precision': ('100.000000000000000001', '123.456789012345678901'),
                }[profile]
                for i, (day, amount) in enumerate(zip(['2025-04-10','2025-07-10'], values), 1):
                    self.add(i, 41001, 'Complete', amount, day)
            else:
                for i, day in enumerate(periods, 1):
                    self.add(i*10, 41001, 'Complete', '10', day)
                    self.add(i*10, 41003, 'Complete', '90', day)
                    self.add(i*10+1, 41002, 'Complete', '20', day)
                    self.add(i*10+2, 41001, 'Cancelled', '7', day)
                    self.add(i*10+3, 41001, 'Complete', None, day)
        self.calls = []

    def add(self, order, product, status, value, day):
        if not self.db.execute('SELECT 1 FROM orders WHERE order_id=?', (order,)).fetchone():
            self.db.execute('INSERT INTO orders VALUES(?,?)', (order, order))
            self.db.execute('INSERT INTO users VALUES(?,?,?,?,?,?,?)',
                            (order, 'Texas', 'Fixture City', 'US', 35, 'F', 'Search'))
        self.db.execute('INSERT INTO order_items VALUES(?,?,?,?,?)',
                        (order, product, status, value, day+'T12:00:00+00:00'))

    def query(self, sql, *, job_config, **kwargs):
        self.calls.append({'dry': job_config.dry_run, 'purpose': (job_config.labels or {}).get('purpose')})
        params = {}
        for p in job_config.query_parameters or ():
            if isinstance(p, bigquery.ArrayQueryParameter):
                names = []
                for i, v in enumerate(p.values):
                    name = f'{p.name}_{i}'; names.append('@'+name); params[name] = v
                sql = sql.replace(f'UNNEST(@{p.name})', '('+','.join(names)+')')
            else:
                v = p.value
                params[p.name] = v.isoformat() if isinstance(v, (date, datetime)) else v
        for table in ('products', 'orders', 'users', 'order_items'):
            sql = sql.replace(f'`{DATASET}.{table}`', table)
        # SQLite SUM(IF(...)) is null on empty input; BigQuery COUNTIF is zero.
        # Transform only conditional counts, leaving SUM/AVG/MAX null semantics intact.
        from sqlglot import exp
        tree = sqlglot.parse_one(sql, read='bigquery')
        tree = tree.transform(lambda node: exp.Count(this=exp.Case(ifs=[
            exp.If(this=node.this.copy(), true=exp.Literal.number(1))]))
            if isinstance(node, exp.CountIf) else node)
        translated = tree.sql(dialect='sqlite')
        cursor = self.db.execute(translated, params)
        names = tuple(x[0] for x in cursor.description)
        integers = {'id','product_id','order_id','user_id','customers','order_count','customer_count',
                    'item_count','eligible_customers','inactive_customers','source_rows','period_rows','matched_customers'}
        strings = {'brand','demographic_value'}
        schema = [bigquery.SchemaField(n, 'INT64' if n in integers else 'STRING' if n in strings else
                    'DATE' if n == 'month' else 'TIMESTAMP' if n == 'latest_item_at' else 'NUMERIC') for n in names]
        rows = []
        for raw in cursor.fetchall():
            row = {}
            for n, value in zip(names, raw):
                if value is not None:
                    if n in integers: value = int(value)
                    elif n == 'month': value = date.fromisoformat(value)
                    elif n == 'latest_item_at': value = datetime.fromisoformat(value)
                    elif n not in strings: value = Decimal(str(value))
                row[n] = value
            rows.append(row)
        return FixtureJob(rows, schema, dry=job_config.dry_run)
