"""Structural field and result policies. Free-text detection is a later layer."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from sqlglot import exp

from .identity import PolicyError
from .metrics import MIN_DEMOGRAPHIC_CUSTOMERS


FIELD_POLICY_VERSION = 1
PROHIBITED_SOURCE_FIELDS = frozenset({
    "first_name", "last_name", "email", "street_address", "postal_code",
    "latitude", "longitude", "user_geom",
})
AGGREGATE_ONLY_SOURCE_FIELDS = frozenset({
    "city", "state", "country", "age", "gender", "traffic_source",
})


def validate_source_fields(statement: exp.Expression, *, demographic: bool) -> None:
    """Reject prohibited source columns even inside transformations."""
    for column in statement.find_all(exp.Column):
        name = column.name.casefold()
        if name in PROHIBITED_SOURCE_FIELDS:
            raise PolicyError("prohibited_source_field")
        if name in AGGREGATE_ONLY_SOURCE_FIELDS and not demographic:
            raise PolicyError("aggregate_only_source_field")


@dataclass(frozen=True)
class ResultContract:
    columns: tuple[str, ...]
    demographic: bool = False
    customer_ids: bool = False
    max_rows: int = 100

    def validate_rows(self, rows: Sequence[Mapping[str, Any]]) -> None:
        """Reject unexpected fields before any row reaches a model adapter."""
        if len(rows) > self.max_rows:
            raise PolicyError("result_limit")
        expected = set(self.columns)
        if expected & PROHIBITED_SOURCE_FIELDS:
            raise PolicyError("invalid_result_contract")
        for row in rows:
            if set(row) != expected:
                raise PolicyError("result_schema_violation")
            if self.demographic:
                if "user_id" in row or "customers" not in row:
                    raise PolicyError("demographic_membership_violation")
                customers = row["customers"]
                if type(customers) is not int or customers < MIN_DEMOGRAPHIC_CUSTOMERS:
                    raise PolicyError("demographic_group_too_small")
            if "user_id" in row and not self.customer_ids:
                raise PolicyError("customer_id_violation")
