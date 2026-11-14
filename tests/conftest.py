"""Shared test setup: embedding host names resolve without network."""

from __future__ import annotations

import ipaddress
from collections.abc import Iterator
from typing import Any

import pytest

try:
    from gdrive_rag import netcheck
except ImportError:  # older code without the address checks (used to prove tests fail first)
    netcheck = None  # type: ignore[assignment]

PUBLIC = ipaddress.ip_address("93.184.216.34")
# names that resolve to an address the checks must refuse (a public name pointing inside)
INSIDE = {
    "metadata.nip.example": ipaddress.ip_address("169.254.169.254"),
    "router.nip.example": ipaddress.ip_address("192.168.1.1"),
    "mixed.example": None,  # one good and one bad record
}


@pytest.fixture(autouse=True)
def fake_dns(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    if netcheck is None:
        yield
        return

    def resolve(host: str, port: int) -> list[Any]:
        if host == "mixed.example":
            return [PUBLIC, ipaddress.ip_address("10.0.0.7")]
        if host == "unresolvable.example":
            raise OSError("no such host")
        value = INSIDE.get(host)
        return [value] if value is not None else [PUBLIC]

    monkeypatch.setattr(netcheck, "resolver", resolve)
    yield
