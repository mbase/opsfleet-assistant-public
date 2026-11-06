"""Trusted demo identity and catalog resolution for brand-only access."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Iterable, Mapping

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from .config import ROOT
from .contracts import BrandScope, Role


class PolicyError(Exception):
    """Safe category only; never includes catalog names or request content."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class Catalog:
    brands: frozenset[str]
    product_brands: Mapping[int, str]

    @classmethod
    def from_products(cls, products: Iterable[Mapping[str, object]]) -> "Catalog":
        product_brands: dict[int, str] = {}
        for row in products:
            product_id = row.get("id")
            brand = row.get("brand")
            if type(product_id) is not int or product_id <= 0 or not isinstance(brand, str) or not brand.strip():
                continue
            if product_id in product_brands and product_brands[product_id] != brand:
                raise PolicyError("invalid_catalog")
            product_brands[product_id] = brand
        brands = frozenset(product_brands.values())
        if not brands:
            raise PolicyError("invalid_catalog")
        return cls(brands=brands, product_brands=MappingProxyType(product_brands))

    def canonical_brand(self, name: str) -> str:
        # BigQuery contains distinct brands whose spelling differs only by case.
        # Preserve an exact catalog value; an ambiguous case-insensitive alias
        # must never grant access to either brand implicitly.
        if name in self.brands:
            return name
        matches = [brand for brand in self.brands if brand.casefold() == name.casefold()]
        if len(matches) != 1:
            raise PolicyError("brand_not_available")
        return matches[0]


class _DemoUser(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)
    user_id: str = Field(min_length=1, max_length=100)
    role: Role
    brands: tuple[str, ...]

    @model_validator(mode="after")
    def valid_role(self) -> "_DemoUser":
        if self.role == Role.MANAGER and not self.brands:
            raise ValueError("manager_requires_brands")
        if self.role == Role.CEO and self.brands:
            raise ValueError("ceo_scope_is_catalog")
        return self


class _DemoConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)
    version: int = Field(ge=1)
    users: tuple[_DemoUser, ...]

    @model_validator(mode="after")
    def unique_users(self) -> "_DemoConfig":
        if len({user.user_id for user in self.users}) != len(self.users):
            raise ValueError("duplicate_demo_user")
        return self


def resolve_demo_scope(
    user_id: str,
    catalog: Catalog,
    path: Path = ROOT / "config" / "demo_users.json",
) -> BrandScope:
    """Only this trusted local file grants the demo role and manager brands."""
    try:
        config = _DemoConfig.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValidationError, ValueError, json.JSONDecodeError) as exc:
        raise PolicyError("invalid_identity_config") from exc
    user = next((entry for entry in config.users if entry.user_id == user_id), None)
    if user is None:
        raise PolicyError("unknown_identity")
    if user.role == Role.MANAGER:
        try:
            brands = tuple(sorted({catalog.canonical_brand(brand) for brand in user.brands}))
        except PolicyError as exc:
            raise PolicyError("invalid_identity_config") from exc
        if not brands:
            raise PolicyError("invalid_identity_config")
    else:
        brands = tuple(sorted(catalog.brands))
    return BrandScope(
        user_id=user.user_id,
        role=user.role,
        brands=brands,
        authorization_version=config.version,
    )


def effective_brands(
    scope: BrandScope,
    requested_brands: tuple[str, ...] | None,
    catalog: Catalog,
) -> tuple[str, ...]:
    """Reject expansion rather than silently dropping out-of-scope brands."""
    if not catalog.brands or not scope.user_id or scope.authorization_version < 1:
        raise PolicyError("invalid_scope")
    if scope.role == Role.MANAGER:
        if not scope.brands:
            raise PolicyError("empty_scope")
        try:
            allowed = {catalog.canonical_brand(brand) for brand in scope.brands}
        except PolicyError as exc:
            raise PolicyError("invalid_scope") from exc
    elif scope.role == Role.CEO:
        allowed = set(catalog.brands)
    else:
        raise PolicyError("invalid_scope")
    if requested_brands is None:
        return tuple(sorted(allowed))
    try:
        requested = {catalog.canonical_brand(brand) for brand in requested_brands}
    except PolicyError as exc:
        raise PolicyError("unauthorized_brand") from exc
    if not requested or not requested.issubset(allowed):
        raise PolicyError("unauthorized_brand")
    return tuple(sorted(requested))


def authorize_products(
    product_ids: tuple[int, ...] | None,
    brands: tuple[str, ...],
    catalog: Catalog,
) -> None:
    if product_ids is None:
        return
    if any(catalog.product_brands.get(product_id) not in brands for product_id in product_ids):
        raise PolicyError("product_not_available")
