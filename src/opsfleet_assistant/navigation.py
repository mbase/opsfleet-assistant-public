"""Small typed navigation contracts and opaque, owner-scoped continuation."""
from dataclasses import dataclass, asdict
import re


@dataclass(frozen=True)
class NavigationResult:
    operation: str
    status: str = 'ok'
    items: tuple[dict, ...] = ()
    content: dict | None = None
    active_conversation: str | None = None
    next_cursor: str | None = None
    message: str = ''
    model_requests: int = 0
    query_executions: int = 0
    business_queries: int = 0

    def payload(self):
        return asdict(self)


class NavigationFailure(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def page_limit(limit):
    if type(limit) is not int or not 1 <= limit <= 50:
        raise NavigationFailure('invalid_page')
    return limit


def position(store, owner, operation, limit, cursor, default):
    page_limit(limit)
    if cursor is None:
        return default
    if not re.fullmatch(r'[A-Za-z0-9_-]{32}', cursor):
        raise NavigationFailure('invalid_cursor')
    value = store.read_navigation_cursor(owner, operation, limit, cursor)
    if value is None:
        raise NavigationFailure('invalid_cursor')
    return value
