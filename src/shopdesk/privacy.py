"""PII handling.

Two levels:
- `redact_for_model` strips data the model never needs (card numbers, IBANs). Applied to
  customer input before it reaches the LLM or the checkpointer.
- `redact` strips every PII category we recognise. Applied to logs and traces.
"""

import re
from collections.abc import Mapping
from typing import Any

EMAIL = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
PHONE_CANDIDATE = re.compile(r"(?<![\w/:.-])\+?\(?\d[\d\s().-]{7,}\d(?![\w:])")
ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}")
CARD_CANDIDATE = re.compile(r"\b(?:\d[ -]?){12,18}\d\b")
IBAN = re.compile(r"\b[A-Z]{2}\d{2}(?:\s?[A-Z0-9]{4}){2,7}(?:\s?[A-Z0-9]{1,3})?\b")
IPV4 = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
BEARER = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{16,}")

SENSITIVE_KEYS = frozenset(
    {
        "authorization",
        "access_token",
        "refresh_token",
        "api_key",
        "password",
        "client_secret",
        "code_verifier",
        "address",
        "shipping_address",
        "billing_address",
        "customer_id",
        "email",
        "first_name",
        "requester_email",
        "requester_name",
    }
)


def _luhn_ok(digits: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        n = int(ch)
        if i % 2:
            n *= 2
            if n > 9:
                n -= 9
        total += n
    return total % 10 == 0


def _redact_cards(text: str) -> str:
    def repl(match: re.Match[str]) -> str:
        digits = re.sub(r"\D", "", match.group())
        # Long order/tracking numbers are common in support chats; only Luhn-valid runs are cards.
        return "[CARD]" if 13 <= len(digits) <= 19 and _luhn_ok(digits) else match.group()

    return CARD_CANDIDATE.sub(repl, text)


def _redact_phones(text: str) -> str:
    def repl(match: re.Match[str]) -> str:
        raw = match.group()
        digits = re.sub(r"\D", "", raw)
        if ISO_DATE.match(raw) or not 9 <= len(digits) <= 15:
            return raw
        return "[PHONE]"

    return PHONE_CANDIDATE.sub(repl, text)


def _iban_ok(candidate: str) -> bool:
    iban = candidate.replace(" ", "")
    if not 15 <= len(iban) <= 34:
        return False
    rearranged = iban[4:] + iban[:4]
    # ISO 13616 mod-97; this is what stops parcel tracking numbers being treated as IBANs.
    return int("".join(str(int(ch, 36)) for ch in rearranged)) % 97 == 1


def _redact_ibans(text: str) -> str:
    return IBAN.sub(lambda m: "[IBAN]" if _iban_ok(m.group()) else m.group(), text)


def redact_for_model(text: str) -> str:
    return _redact_ibans(_redact_cards(text))


def redact(text: str) -> str:
    text = BEARER.sub("Bearer [TOKEN]", text)
    text = redact_for_model(text)
    text = EMAIL.sub("[EMAIL]", text)
    text = IPV4.sub("[IP]", text)
    return _redact_phones(text)


def scrub(value: Any) -> Any:
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, Mapping):
        return {
            k: "[REDACTED]" if str(k).lower() in SENSITIVE_KEYS else scrub(v)
            for k, v in value.items()
        }
    if isinstance(value, list | tuple):
        return [scrub(v) for v in value]
    return value


def mask_email(email: str) -> str:
    local, _, domain = email.partition("@")
    if not domain:
        return "[EMAIL]"
    return f"{local[:1]}***@{domain}"
