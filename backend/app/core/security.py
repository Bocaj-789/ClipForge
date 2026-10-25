"""Small, dependency-free security helpers: redaction, path containment, safe names."""

from __future__ import annotations

import re
import secrets
import unicodedata
from pathlib import Path

_SECRET_PATTERNS = [
    # OAuth / bearer tokens, Google API keys, refresh tokens, generic key=value secrets.
    re.compile(r"(?i)(bearer\s+)[A-Za-z0-9\-._~+/]+=*"),
    re.compile(r"AIza[0-9A-Za-z\-_]{30,}"),
    re.compile(r"ya29\.[0-9A-Za-z\-_.]+"),
    re.compile(r"1//[0-9A-Za-z\-_]{20,}"),
    re.compile(
        r"(?i)((?:access_token|refresh_token|client_secret|api_key|apikey|key|password|token|secret|"
        r"session|cookie|csrf)[\"']?\s*[:=]\s*[\"']?)[^\s\"'&,;]+"
    ),
]


def redact(text: str) -> str:
    """Remove anything that looks like a credential from free text."""
    out = text
    for pat in _SECRET_PATTERNS:
        if pat.groups:
            out = pat.sub(lambda m: m.group(1) + "[REDACTED]", out)
        else:
            out = pat.sub("[REDACTED]", out)
    return out


_SENSITIVE_KEYS = re.compile(r"(?i)(token|secret|password|api_?key|cookie|authorization|session|csrf)")


def redact_mapping(data: object) -> object:
    """Recursively redact values whose keys look sensitive, and scrub strings."""
    if isinstance(data, dict):
        return {
            k: ("[REDACTED]" if _SENSITIVE_KEYS.search(str(k)) else redact_mapping(v))
            for k, v in data.items()
        }
    if isinstance(data, list):
        return [redact_mapping(v) for v in data]
    if isinstance(data, str):
        return redact(data)
    return data


class PathContainmentError(ValueError):
    pass


def contained_path(base: Path, candidate: Path | str) -> Path:
    """Resolve ``candidate`` and ensure it stays inside ``base`` (no traversal/symlink escape)."""
    base_r = base.resolve()
    p = Path(candidate)
    if not p.is_absolute():
        p = base_r / p
    p_r = p.resolve()
    if p_r != base_r and base_r not in p_r.parents:
        raise PathContainmentError("path escapes allowed directory")
    return p_r


_SAFE_CHARS = re.compile(r"[^A-Za-z0-9._-]+")


def safe_filename(stem_hint: str, ext: str, max_len: int = 60) -> str:
    """Generate a collision-resistant, filesystem-safe filename. Never trust user names."""
    norm = unicodedata.normalize("NFKD", stem_hint).encode("ascii", "ignore").decode()
    stem = _SAFE_CHARS.sub("-", norm).strip("-.")[:max_len] or "file"
    ext = _SAFE_CHARS.sub("", ext.lstrip(".")).lower()[:8] or "bin"
    return f"{stem}-{secrets.token_hex(6)}.{ext}"


_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def clean_text(value: str, max_len: int) -> str:
    """Strip control characters and bound length for any untrusted display text."""
    return _CONTROL.sub("", value).strip()[:max_len]
