"""Server-side redaction. Runs on every ingested span regardless of SDK configuration.

Two mechanisms:
  1. Pattern redaction over all string attribute values (emails, phones, cards, secrets/API keys).
  2. Field deny-list from the project's redaction_rules: {"deny_fields": ["gen_ai.prompt", ...],
     "extra_patterns": {"name": "regex"}}.
Returns the redacted attributes and the number of redactions applied so the count is auditable.
"""
import re
from typing import Any

DEFAULT_PATTERNS = {
    "email": re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"),
    "phone": re.compile(r"(?<!\d)(?:\+?\d{1,3}[\s-]?)?(?:\(?\d{3}\)?[\s-]?)\d{3}[\s-]?\d{4}(?!\d)"),
    "card": re.compile(r"(?<!\d)(?:\d[ -]?){13,16}(?!\d)"),
    "secret": re.compile(r"(?:sk|mk|pk|ghp|xox[baprs])[-_][A-Za-z0-9_\-]{8,}"),
    "ssn": re.compile(r"(?<!\d)\d{3}-\d{2}-\d{4}(?!\d)"),
}


class Redactor:
    def __init__(self, rules: dict | None = None):
        rules = rules or {}
        self.deny_fields = set(rules.get("deny_fields", []))
        self.patterns = dict(DEFAULT_PATTERNS)
        for name, pat in rules.get("extra_patterns", {}).items():
            self.patterns[name] = re.compile(pat)
        self.disabled = set(rules.get("disable_patterns", []))

    def _scrub_str(self, s: str) -> tuple[str, int]:
        n = 0
        for name, pat in self.patterns.items():
            if name in self.disabled:
                continue
            s, k = pat.subn(f"[REDACTED:{name}]", s)
            n += k
        return s, n

    def _scrub(self, v: Any) -> tuple[Any, int]:
        if isinstance(v, str):
            return self._scrub_str(v)
        if isinstance(v, list):
            out, n = [], 0
            for x in v:
                y, k = self._scrub(x); out.append(y); n += k
            return out, n
        if isinstance(v, dict):
            out, n = {}, 0
            for key, x in v.items():
                if key in self.deny_fields:
                    out[key] = "[REDACTED:field]"; n += 1; continue
                y, k = self._scrub(x); out[key] = y; n += k
            return out, n
        return v, 0

    def apply(self, attributes: dict, store_payloads: bool = True) -> tuple[dict, int]:
        attrs = dict(attributes)
        n = 0
        if not store_payloads:
            for k in list(attrs):
                if k.startswith("gen_ai.prompt") or k.startswith("gen_ai.completion") or k.endswith(".payload"):
                    attrs[k] = "[NOT_STORED:policy]"; n += 1
        attrs, k = self._scrub(attrs)
        return attrs, n + k
