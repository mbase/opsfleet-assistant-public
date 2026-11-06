"""Deterministic report-management commands; model output has no role here."""

from __future__ import annotations

from dataclasses import dataclass
import re


@dataclass(frozen=True)
class DeletionCommand:
    action: str
    selector: str = ""
    value: str = ""


_TOKEN = r"([A-Za-z0-9_-]{43})"
_MENTION = re.compile(r"^delete\s+all\s+(?:the\s+)?reports?\s+mentioning\s+(.+?)[.!?]?\s*$", re.I | re.S)
_CONVERSATION = re.compile(
    r"^delete\s+all\s+(?:the\s+)?reports?(?:\s+we\s+made)?\s+in\s+this\s+conversation[.!?]?\s*$",
    re.I,
)


def parse_deletion(question: str) -> DeletionCommand | None:
    stripped = question.strip()
    if stripped.casefold().startswith("please "):
        stripped = stripped[7:].lstrip()
    if re.fullmatch(r"confirm\s+" + _TOKEN, stripped, re.I):
        return DeletionCommand("confirm", value=stripped.split(maxsplit=1)[1])
    if re.fullmatch(r"cancel\s+" + _TOKEN, stripped, re.I):
        return DeletionCommand("cancel", value=stripped.split(maxsplit=1)[1])
    if _CONVERSATION.fullmatch(stripped):
        return DeletionCommand("preview", "conversation")
    match = _MENTION.fullmatch(stripped)
    if match:
        value = match.group(1).strip().strip("\"'“”‘’ ")
        if value and len(value) <= 200 and not re.search(r"\b(?:and|or)\b", value, re.I):
            return DeletionCommand("preview", "literal", value)
    if re.search(r"\b(?:delete|remove)\b.{0,120}\breports?\b", stripped, re.I | re.S):
        return DeletionCommand("ambiguous")
    if re.match(r"^confirm\b", stripped, re.I):
        return DeletionCommand("ambiguous")
    return None
