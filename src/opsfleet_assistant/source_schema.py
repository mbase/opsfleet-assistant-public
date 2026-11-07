"""Trusted source contracts. No model/request supplied names or expressions.

Only the public v1 binding ships. Alternate bindings require a reviewed code change;
synthetic migrations belong in tests, never in the production registry.
"""
from dataclasses import dataclass
import re
from typing import Mapping

import sqlglot
from sqlglot import exp

from .identity import PolicyError
from .privacy import PROHIBITED_SOURCE_FIELDS, AGGREGATE_ONLY_SOURCE_FIELDS

DATASET = 'bigquery-public-data.thelook_ecommerce'
COMPILER_VERSION = 1


@dataclass(frozen=True)
class SourceField:
    table: str
    logical: str
    physical: str
    types: tuple[str, ...]


@dataclass(frozen=True)
class SourceBinding:
    version: str
    compiler_version: int
    fields: tuple[SourceField, ...]


PUBLIC_BINDING = SourceBinding('public_v1', COMPILER_VERSION, tuple(
    SourceField(table, name, name, types)
    for table, columns in (
        ('order_items', (('order_id', ('INT64',)), ('product_id', ('INT64',)),
                         ('created_at', ('TIMESTAMP',)), ('sale_price', ('FLOAT64', 'NUMERIC')),
                         ('status', ('STRING',)))),
        ('orders', (('order_id', ('INT64',)), ('user_id', ('INT64',)))),
        ('products', (('id', ('INT64',)), ('brand', ('STRING',)))),
        ('users', (('id', ('INT64',)), ('age', ('INT64',)), ('city', ('STRING',)),
                   ('state', ('STRING',)), ('country', ('STRING',)), ('gender', ('STRING',)),
                   ('traffic_source', ('STRING',)))),
    ) for name, types in columns
))
APPROVED_BINDINGS = (PUBLIC_BINDING,)
TABLES = ('order_items', 'orders', 'products', 'users')


def trusted_binding(binding: SourceBinding) -> None:
    if (len({b.version for b in APPROVED_BINDINGS}) != len(APPROVED_BINDINGS)
            or binding not in APPROVED_BINDINGS or binding.compiler_version != COMPILER_VERSION
            or {(f.table, f.logical) for f in binding.fields}
            != {(f.table, f.logical) for f in PUBLIC_BINDING.fields}
            or len(binding.fields) != len(PUBLIC_BINDING.fields)
            or len({(f.table, f.physical) for f in binding.fields}) != len(binding.fields)):
        raise PolicyError('query_schema_error')
    for field in binding.fields:
        baseline = next(f for f in PUBLIC_BINDING.fields if (f.table, f.logical) == (field.table, field.logical))
        if (not re.fullmatch(r'[a-z][a-z0-9_]{0,63}', field.physical)
                or field.physical in PROHIBITED_SOURCE_FIELDS
                or field.physical in AGGREGATE_ONLY_SOURCE_FIELDS and field.physical != field.logical
                or field.types != baseline.types):
            raise PolicyError('query_schema_error')


def choose_binding(schemas: Mapping[str, Mapping[str, tuple[str, str]]]) -> SourceBinding:
    """Extra source fields are ignored; required bindings must be scalar and typed."""
    for binding in APPROVED_BINDINGS:
        trusted_binding(binding)
        if all(schemas.get(f.table, {}).get(f.physical, ('', ''))[0] in f.types
               and schemas[f.table][f.physical][1] in {'NULLABLE', 'REQUIRED'} for f in binding.fields):
            return binding
    raise PolicyError('query_schema_error')


def bind_sql(sql: str, binding: SourceBinding) -> str:
    """Render approved physical fields through logical projections, without casts.

    This is a compiler-owned binding operation, not an error-text SQL rewrite.
    """
    trusted_binding(binding)
    if all(f.logical == f.physical for f in binding.fields):
        return sql
    tree = sqlglot.parse_one(sql, read='bigquery')
    for table in list(tree.find_all(exp.Table)):
        if table.catalog != 'bigquery-public-data' or table.db != 'thelook_ecommerce':
            continue  # Logical CTE references are unchanged.
        fields = tuple(f for f in binding.fields if f.table == table.name)
        if not fields or all(f.logical == f.physical for f in fields):
            continue
        source = table.copy()
        source.set('alias', None)
        projection = exp.select(*(exp.alias_(exp.column(f.physical, quoted=True), f.logical, quoted=True)
                                  for f in fields)).from_(source)
        table.replace(projection.subquery(alias=table.alias_or_name))
    return tree.sql(dialect='bigquery')
