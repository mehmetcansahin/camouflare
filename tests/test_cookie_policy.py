from __future__ import annotations

import pytest

from camouflare.cookie_policy import is_public_suffix


@pytest.mark.parametrize("domain", ["com", ".co.uk", "github.io"])
def test_registry_and_private_suffixes_are_public_cookie_boundaries(domain: str) -> None:
    assert is_public_suffix(domain) is True


@pytest.mark.parametrize("domain", ["example.com", "attacker.co.uk", "site.github.io"])
def test_registrable_domains_are_not_public_suffixes(domain: str) -> None:
    assert is_public_suffix(domain) is False


def test_ip_addresses_are_not_treated_as_public_suffixes() -> None:
    assert is_public_suffix("127.0.0.1") is False
    assert is_public_suffix("::1") is False
