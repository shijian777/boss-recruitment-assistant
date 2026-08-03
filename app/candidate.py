from __future__ import annotations

import hashlib
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from app.selectors import normalize_space


STABLE_QUERY_KEYS = {"id", "geekid", "candidateid", "encryptgeekid", "securityid"}


def _digest(prefix: str, value: str) -> str:
    return f"{prefix}:{hashlib.sha256(value.encode('utf-8')).hexdigest()}"


def canonical_candidate_url(url: str) -> str:
    raw = normalize_space(url)
    if not raw:
        return ""
    parts = urlsplit(raw)
    query = [
        (key.casefold(), value)
        for key, value in parse_qsl(parts.query, keep_blank_values=False)
        if key.casefold() in STABLE_QUERY_KEYS and value
    ]
    query.sort()
    return urlunsplit((parts.scheme.casefold(), parts.netloc.casefold(), parts.path.rstrip("/"), urlencode(query), ""))


def make_candidate_key(
    *,
    stable_id: str = "",
    candidate_url: str = "",
    stable_fields: tuple[str, ...] | list[str] = (),
    fallback_text: str = "",
) -> str:
    stable = normalize_space(stable_id)
    if stable:
        return _digest("id", stable.casefold())
    canonical_url = canonical_candidate_url(candidate_url)
    if canonical_url:
        return _digest("url", canonical_url)
    normalized_fields = [normalize_space(item).casefold() for item in stable_fields if normalize_space(item)]
    if len(normalized_fields) >= 2:
        return _digest("fields", "|".join(normalized_fields))
    fallback = normalize_space(fallback_text).casefold()
    if fallback:
        return _digest("text", fallback)
    return ""
