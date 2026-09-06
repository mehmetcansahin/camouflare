from __future__ import annotations

from ipaddress import ip_address

from publicsuffixlist import PublicSuffixList

_PUBLIC_SUFFIX_LIST = PublicSuffixList()


def is_public_suffix(domain: str) -> bool:
    """Return whether a cookie Domain names a registry-controlled suffix."""

    normalized = domain.strip().lower().lstrip(".").rstrip(".")
    if not normalized:
        return False
    try:
        ip_address(normalized)
        return False
    except ValueError:
        pass
    try:
        ascii_domain = normalized.encode("idna").decode("ascii")
    except UnicodeError:
        return True
    suffix = _PUBLIC_SUFFIX_LIST.publicsuffix(ascii_domain)
    return suffix is not None and suffix.lower().rstrip(".") == ascii_domain
