"""BigQuery grammar regression found by a real, unbilled snapshot dry run."""
import pytest
from sqlglot import parse_one, exp
from opsfleet_assistant.evaluation_part5 import snapshot_sql

STAMP='2026-11-08T22:09:44+00:00'

@pytest.mark.parametrize('sql', [
    'SELECT id FROM `bigquery-public-data.thelook_ecommerce.products`',
    'SELECT p.id FROM `bigquery-public-data.thelook_ecommerce.products` p',
    'SELECT p.id FROM `bigquery-public-data.thelook_ecommerce.products` AS p',
    'SELECT SUM(i.sale_price) FROM `bigquery-public-data.thelook_ecommerce.order_items` i JOIN `bigquery-public-data.thelook_ecommerce.products` p ON p.id=i.product_id WHERE i.status=@status',
    'WITH scoped AS (SELECT i.* FROM `bigquery-public-data.thelook_ecommerce.order_items` i) SELECT COUNT(*) FROM scoped',
])
def test_snapshot_clause_follows_alias_and_preserves_query(sql):
    rendered=snapshot_sql(sql,STAMP)
    original=parse_one(sql,read='bigquery')
    pinned=parse_one(rendered,read='bigquery')
    sources=[t for t in pinned.find_all(exp.Table) if t.catalog=='bigquery-public-data']
    assert sources
    for table in sources:
        assert table.args['version'].expression.this.this==STAMP
        if table.alias:
            assert f'AS {table.alias} FOR SYSTEM_TIME AS OF' in rendered
        table.set('version',None)
    assert pinned==original


def test_existing_snapshot_is_not_replaced():
    sql='SELECT * FROM `bigquery-public-data.thelook_ecommerce.products` AS p FOR SYSTEM_TIME AS OF TIMESTAMP(\'2026-11-08T20:00:00+00:00\')'
    with pytest.raises(ValueError,match='unsupported_snapshot_query'):snapshot_sql(sql,STAMP)
