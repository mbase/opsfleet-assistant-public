"""Offline SQLite transport for real compiler/adapter. Not BigQuery certification.

Uses sqlglot's SQLite rendering and float arithmetic; released counts/results
still pass production result validation. No network or credential access.
"""
from datetime import datetime, timezone
from types import SimpleNamespace
import sqlite3
import sqlglot
from google.cloud import bigquery
from google.api_core import exceptions as ge
from test_step3 import Job

class SourceClient:
    def __init__(self):
        self.db=sqlite3.connect(':memory:');self.db.row_factory=sqlite3.Row
        self.db.executescript('''
        CREATE TABLE products(id INTEGER PRIMARY KEY, brand TEXT);
        CREATE TABLE orders(order_id INTEGER PRIMARY KEY, user_id INTEGER);
        CREATE TABLE users(id INTEGER PRIMARY KEY, age INTEGER, city TEXT, state TEXT, country TEXT, gender TEXT, traffic_source TEXT);
        CREATE TABLE order_items(order_id INTEGER, product_id INTEGER, created_at TEXT, sale_price REAL, status TEXT);
        INSERT INTO products VALUES(1,'Calvin Klein'),(2,'Carhartt'),(3,'Hanes');
        ''')
        self.calls=[];self.metadata=[];self.fault=None;self.fault_stage='analysis';self.reject_once=False
        self.next_order=1
    def user(self,id,state='Texas',country='A'):
        self.db.execute('INSERT OR REPLACE INTO users(id,state,country) VALUES(?,?,?)',(id,state,country))
    def order(self,user,items,at='2025-02-01T00:00:00+00:00',status='Complete'):
        oid=self.next_order;self.next_order+=1
        self.db.execute('INSERT INTO orders VALUES(?,?)',(oid,user))
        self.db.executemany('INSERT INTO order_items VALUES(?,?,?,?,?)',[(oid,p,at,price,status) for p,price in items])
    def get_table(self,name,*,retry,timeout):
        self.metadata.append(name)
        kinds={'INTEGER':'INT64','TEXT':'STRING','REAL':'FLOAT64'}
        return SimpleNamespace(schema=[bigquery.SchemaField(r['name'],'TIMESTAMP' if r['name']=='created_at' else kinds[r['type']])
            for r in self.db.execute('PRAGMA table_info('+name.rsplit('.',1)[1]+')')])
    def query(self,sql,*,job_config,retry,job_retry,timeout,**kwargs):
        stage='analysis' if 'scoped_items AS' in sql else 'coverage' if 'source_rows' in sql else 'catalog'
        self.calls.append((stage,job_config.dry_run,sql))
        if self.fault and stage==self.fault_stage:
            if self.fault=='schema' and job_config.dry_run:
                self.fault=None
                raise ge.BadRequest('Unrecognized name: synthetic schema fault')
            if self.fault=='ambiguous' and not job_config.dry_run:
                raise ge.ServiceUnavailable('Synthetic acknowledged ambiguity')
            if self.fault=='coverage' and not job_config.dry_run:
                raise ge.Forbidden('Synthetic coverage failure')
        rendered=sql.replace('`bigquery-public-data.thelook_ecommerce.','`');values={}
        for p in job_config.query_parameters:
            if isinstance(p,bigquery.ArrayQueryParameter):
                literals=','.join("'"+str(v).replace("'","''")+"'" for v in p.values)
                rendered=rendered.replace('UNNEST(@'+p.name+')','('+literals+')')
            else:
                values[p.name]=p.value.isoformat() if isinstance(p.value,datetime) else p.value
        translated=sqlglot.transpile(rendered,read='bigquery',write='sqlite')[0]
        cursor=self.db.execute(translated,values);rows=[dict(r) for r in cursor]
        if job_config.dry_run:return SimpleNamespace(total_bytes_processed=10)
        for row in rows:
            if 'period_rows' in row and row['period_rows'] is None:row['period_rows']=0
            if row.get('latest_item_at'):row['latest_item_at']=datetime.fromisoformat(row['latest_item_at'])
        kinds={'id':'INT64','brand':'STRING','product_id':'INT64','item_count':'INT64','order_count':'INT64',
            'customers':'INT64','demographic_value':'STRING','cutoff_role':'STRING','segment_key':'STRING',
            'eligible_customers':'INT64','inactive_customers':'INT64','source_rows':'INT64','period_rows':'INT64','latest_item_at':'TIMESTAMP'}
        return Job(rows,[bigquery.SchemaField(d[0],kinds.get(d[0],'FLOAT64')) for d in cursor.description],
            job_id='synthetic_'+str(len(self.calls)),billed=10)

def product_source():
    c=SourceClient()
    for i in range(100):c.order(i+1,[(1,10)])
    for i in range(50):c.order(101+i,[(2,12)])
    # Mixed basket, excluded statuses, null price and exact end boundary.
    c.order(1,[(3,9999),(1,None)])
    c.order(1,[(1,9999)],status='Cancelled')
    c.order(1,[(1,9999)],at='2025-04-01T00:00:00+00:00')
    c.order(1,[(1,9999)],at='2024-12-31T23:59:59+00:00')
    return c

def regional_source():
    c=SourceClient()
    for i in range(100):
        c.user(i+1,'Texas')
        for _ in range(2):c.order(i+1,[(1,20),(2,30),(3,9999)])
    for i in range(50):
        c.user(i+101,'California');c.order(i+101,[(1,40),(2,40),(3,9999)])
    c.order(1,[(1,9999)],status='Returned')
    c.order(1,[(1,9999)],at='2025-04-01T00:00:00+00:00')
    return c

def churn_source():
    c=SourceClient()
    # Cumulative prior-purchaser populations: baseline 600/400, current 600/600.
    # Rates .1/.2 -> .2/.3; shares .6/.4 -> .5/.5. Same +.01/+.10 identity.
    for group,n,base_inactive,current_inactive,offset in [('A',600,60,120,0),('B',600,80,180,1000)]:
        for i in range(n):
            uid=offset+i+1;c.user(uid,country=group)
            if group=='B' and i>=400:
                c.order(uid,[(1,1)],at='2025-02-01T00:00:00+00:00')
            else:
                c.order(uid,[(1,1)],at=('2024-09-01T00:00:00+00:00' if i<base_inactive else '2024-12-01T00:00:00+00:00'))
            if i>=current_inactive:
                c.order(uid,[(1,1)],at='2025-03-01T00:00:00+00:00')
    return c
