"""Deterministic input/output screening before any model request."""

from __future__ import annotations

import re

from .identity import PolicyError


_EMAIL = re.compile(r"\b[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}\b")
_PHONE = re.compile(r"(?<!\d)(?:\+\d{1,3}[-. ]?)?\(?\d{3}\)?[-. ]?\d{3}[-. ]?\d{4}(?!\d)")
_COORDINATES = re.compile(r"\b(?:lat(?:itude)?|lon(?:gitude)?|coordinates?)\b", re.I)
_ADDRESS = re.compile(r"\b(?:street[_ ]?address|home address|mailing address|postal[_ ]?code|zip[_ ]?code)\b", re.I)
_STREET = re.compile(r"\b\d{1,6}\s+(?:[A-Za-z]+\s+){0,3}(?:Street|St|Road|Rd|Avenue|Ave|Boulevard|Blvd)\b", re.I)
_PRIVATE_FIELD = re.compile(r"\b(?:first[_ ]?name|last[_ ]?name|(?:customer|user|buyer|person)\s+names?|email(?:s|[_ ]?address)?|phone(?: number)?|date of birth|social security|ssn)\b", re.I)
_IDENTITY_LOOKUP = re.compile(r"\b(?:who is|identify)\s+(?:customer|user|buyer)\s*\d+\b", re.I)
_RAW_SQL = re.compile(
    r"\bselect\b.{0,100}\bfrom\b|\binsert\b.{0,100}\binto\b|"
    r"\bupdate\b.{0,100}\bset\b|\bdelete\b.{0,100}\bfrom\b|"
    r"\b(?:drop|alter|create)\s+(?:table|view|schema|database)\b|```|bigquery-public-data\.",
    re.I | re.S,
)
_INJECTION = re.compile(
    r"(?:ignore|override|bypass|disregard).{0,35}(?:instructions|policy|rules|system|developer)"
    r"|(?:system|developer)\s*(?:prompt|message|instructions?)"
    r"|(?:reveal|print|show).{0,25}(?:api key|secret|credential|environment variable)"
    r"|(?:execute|run).{0,15}(?:arbitrary|raw)\s+sql"
    r"|(?:act|pretend).{0,25}(?:system|developer|administrator)",
    re.I | re.S,
)
_DEMOGRAPHIC_ID = re.compile(
    r"(?:\b(?:list|show|give|identify|find)\b.{0,55}\b(?:customer|user|person|people)?\s*ids?\b"
    r"|\b(?:who|which customers?)\b.{0,55}\b(?:state|city|age|gender|country)\b)",
    re.I,
)
_DEMOGRAPHIC_TERM = re.compile(r"\b(?:state|city|age|gender|country|demographic|cohort|their|those|them)\b", re.I)
_ID_REFERENCE = re.compile(r"\b(?:their|those|them|this cohort|that cohort)\b.{0,30}\b(?:ids?|customers?|users?)\b|\b(?:ids?|customers?|users?)\b.{0,30}\b(?:their|those|them)\b", re.I)
_LOCATION_IDS = re.compile(r"\b(?:list|show|give|find)\b.{0,45}\b(?:customer|user)?\s*ids?\b.{0,25}\b(?:in|from)\s+(?!brand\b|product\b)[A-Za-z]", re.I)
_FORGED_AUTHORITY = re.compile(r"\b(?:i am|i'm|as the|pretend i am|my role is)\s+(?:the\s+)?(?:ceo|admin|administrator|system)\b", re.I)


_CREDENTIAL = re.compile(
    r"\b(?:api[_ -]?keys?|passwords?|passwd|secrets?|access[_ -]?tokens?|bearer|credentials?)\b"
    r"|\b(?:sk-|AIza)[A-Za-z0-9_-]{16,}|\b[A-Za-z0-9_-]{32,}\b"
    r"|\b\d{3}-\d{2}-\d{4}\b|\b(?:\d[ -]?){13,19}\b", re.I)


def _supplemental_pii(value: str) -> bool:
    # This scanner can miss PII or mistake retail names for people. PERSON is
    # deliberately excluded; structural field policy prevents warehouse names.
    try:
        from presidio_analyzer import AnalyzerEngine  # type: ignore[import-not-found]
    except ImportError:
        return False
    try:
        return bool(AnalyzerEngine().analyze(
            text=value, language="en",
            entities=["EMAIL_ADDRESS", "PHONE_NUMBER", "US_SSN", "CREDIT_CARD"],
            score_threshold=0.85,
        ))
    except Exception:
        return False


def screen_question(question: str, *, prior_demographic: bool = False) -> str:
    if not isinstance(question, str) or not 2 <= len(question.strip()) <= 2000:
        raise PolicyError("invalid_question")
    clean = " ".join(question.split())
    if _CREDENTIAL.search(clean):
        raise PolicyError("pii_request_blocked")
    if (_EMAIL.search(clean) or _PHONE.search(clean) or _ADDRESS.search(clean) or _STREET.search(clean)
            or _COORDINATES.search(clean) or _PRIVATE_FIELD.search(clean) or _IDENTITY_LOOKUP.search(clean)):
        raise PolicyError("pii_request_blocked")
    if _RAW_SQL.search(clean):
        raise PolicyError("unsupported_request")
    if _INJECTION.search(clean) or _FORGED_AUTHORITY.search(clean):
        raise PolicyError("instruction_attack_blocked")
    if (_LOCATION_IDS.search(clean) or (_DEMOGRAPHIC_ID.search(clean) and _DEMOGRAPHIC_TERM.search(clean))
            or (prior_demographic and _ID_REFERENCE.search(clean))):
        raise PolicyError("demographic_membership_blocked")
    if _supplemental_pii(clean):
        raise PolicyError("pii_request_blocked")
    return clean


def screen_model_prose(value: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 500:
        raise PolicyError("unsafe_report_text")
    if (re.search(r"\d", value) or _EMAIL.search(value) or _PHONE.search(value)
            or _ADDRESS.search(value) or _PRIVATE_FIELD.search(value)
            or _COORDINATES.search(value) or _INJECTION.search(value)
            or _RAW_SQL.search(value)
            or re.search(r"\b(?:higher|lower|increased|decreased|grew|growth|declined|spiked|caused|best|worst|more|less)\b", value, re.I)
            or _supplemental_pii(value)):
        raise PolicyError("unsafe_report_text")
    return value.strip()
