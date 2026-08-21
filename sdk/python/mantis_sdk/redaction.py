"""Client-side redaction: a SpanProcessor that scrubs attribute values before they leave the process.
The platform redacts again server-side; this exists so sensitive data never leaves the customer boundary."""
import re

from opentelemetry.sdk.trace import SpanProcessor

PATTERNS = {
    "email": re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"),
    "secret": re.compile(r"(?:sk|mk|pk|ghp|xox[baprs])[-_][A-Za-z0-9_\-]{8,}"),
    "card": re.compile(r"(?<!\d)(?:\d[ -]?){13,16}(?!\d)"),
}


class ClientRedactor(SpanProcessor):
    def __init__(self, deny_fields: list[str] | None = None, patterns: dict | None = None):
        self.deny = set(deny_fields or [])
        self.patterns = patterns or PATTERNS

    def _scrub(self, v):
        if isinstance(v, str):
            for n, p in self.patterns.items():
                v = p.sub(f"[REDACTED:{n}]", v)
        return v

    def on_end(self, span):
        # ReadableSpan attributes are immutable by contract; we rewrite the underlying mapping, which is
        # the accepted pattern for redaction processors.
        attrs = span._attributes  # noqa: SLF001
        if attrs is None:
            return
        # BoundedAttributes is frozen once the span ends; redact the backing dict in place.
        target = getattr(attrs, "_dict", attrs)
        for k in list(target.keys()):
            if k in self.deny:
                target[k] = "[REDACTED:field]"
            else:
                target[k] = self._scrub(target[k])

    def on_start(self, span, parent_context=None): ...
    def shutdown(self): ...
    def force_flush(self, timeout_millis=30000): return True
