"""Where embedding requests may go: one host check used when an address is set (``admin``) and
again on every request (``embeddings``), which also connects to the very address it checked.

- Host names: only ``[a-z0-9.-]`` (lower-cased, one trailing dot dropped) or a bracketed IPv6
  literal. Percent-encoding and numeric shorthands (``2852039166``, ``0xa9fea9fe``, octal or
  short dotted forms) are refused, so a host cannot be disguised as something it is not.
- Every address the name resolves to must be public (globally routable: shared address space
  such as 100.64.0.0/10 counts as local). Link-local, cloud-metadata, unspecified,
  multicast and reserved addresses are always refused; loopback and private ones only with
  ``GDRAG_ALLOW_LOCAL_EMBEDDINGS=1`` (environment only).
- Plain http is allowed only to a literal loopback or private address, with that same setting.
"""

from __future__ import annotations

import ipaddress
import os
import re
import socket
from collections.abc import Callable
from urllib.parse import urlsplit

LOCAL_ENV = "GDRAG_ALLOW_LOCAL_EMBEDDINGS"
METADATA_HOSTS = {"metadata.google.internal", "metadata", "metadata.goog"}
_NAME = re.compile(r"[a-z0-9.-]+")
_NUMERIC_LABEL = re.compile(r"\d+|0x[0-9a-f]*")
IP = ipaddress.IPv4Address | ipaddress.IPv6Address


class DestinationRefused(ValueError):
    """The embedding address is not allowed; the message names the rule, not secrets."""


def local_allowed() -> bool:
    return os.environ.get(LOCAL_ENV, "").strip().lower() in {"1", "true", "yes", "on"}


def _resolve(host: str, port: int) -> list[IP]:
    infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return [ipaddress.ip_address(str(info[4][0]).split("%")[0]) for info in infos]


resolver: Callable[[str, int], list[IP]] = _resolve  # tests replace this; no network there


def origin(base_url: str) -> str:
    """``scheme://host:port`` of an address, lower-cased; the raw text when it cannot be parsed
    (so an unparsable address never equals another origin). Holds no secret."""
    try:
        scheme, host, port, _ = parse(base_url)
    except ValueError:
        return f"?{str(base_url or '').strip()}"
    return f"{scheme}://{host}:{port}"


def parse(base_url: str) -> tuple[str, str, int, str]:
    """(scheme, host, port, path) of an allowed address shape, or DestinationRefused."""
    text = str(base_url or "").strip()
    url = urlsplit(text)
    if "%" in url.netloc:
        raise DestinationRefused("base_url must not contain percent-encoding in the host")
    try:
        port = url.port
    except ValueError:
        raise DestinationRefused("base_url is not a valid address") from None
    if url.scheme not in {"https", "http"}:
        raise DestinationRefused("base_url must be an https address such as https://host/api/v1")
    if url.username or url.password or url.query or url.fragment:
        raise DestinationRefused("base_url must not contain a user name, password or query")
    netloc_host = url.netloc.rsplit("@", 1)[-1]
    bracketed = netloc_host.startswith("[")
    host = (url.hostname or "").lower()
    if host.endswith(".") and not bracketed:
        host = host[:-1]
    if not host:
        raise DestinationRefused("base_url has no host")
    if bracketed:
        try:
            ipaddress.IPv6Address(host)
        except ValueError:
            raise DestinationRefused("base_url has an invalid IPv6 address") from None
    else:
        if not _NAME.fullmatch(host) or ".." in host or host.startswith((".", "-")):
            raise DestinationRefused("base_url host may contain only letters, digits, . and -")
        labels = host.split(".")
        if all(_NUMERIC_LABEL.fullmatch(label) for label in labels):
            try:
                if str(ipaddress.IPv4Address(host)) != host:
                    raise ValueError
            except ValueError:
                raise DestinationRefused(
                    "base_url host is a numeric shorthand; write the address as a.b.c.d"
                ) from None
    if host in METADATA_HOSTS:
        raise DestinationRefused("base_url points at a link-local or cloud metadata address")
    default = 443 if url.scheme == "https" else 80
    return url.scheme, host, port or default, url.path or "/"


def literal(host: str) -> IP | None:
    try:
        return ipaddress.ip_address(host)
    except ValueError:
        return None


def _public(address: IP) -> bool:
    """Only globally routable addresses are public. Shared address space (100.64.0.0/10:
    Tailscale, CGNAT, Alibaba metadata 100.100.100.200) and the like are local, not public."""
    mapped = getattr(address, "ipv4_mapped", None)
    return address.is_global and (mapped is None or mapped.is_global)


def is_local(address: IP) -> bool:
    """A non-public address that is allowed (with the local flag) as an http destination."""
    return not _public(address)


def check_address(address: IP, allow_local: bool) -> None:
    if address.is_link_local or address in _METADATA_NETS:
        raise DestinationRefused("base_url points at a link-local or cloud metadata address")
    if address.is_unspecified or address.is_multicast or address.is_reserved:
        raise DestinationRefused("base_url points at an address that is not a server")
    mapped = getattr(address, "ipv4_mapped", None)
    if mapped is not None:
        check_address(mapped, allow_local)
    if not _public(address) and not allow_local:
        raise DestinationRefused(
            f"base_url points at a loopback or private address; allowed only with {LOCAL_ENV}=1"
        )


_METADATA_NETS = (ipaddress.ip_network("fd00:ec2::254/128"),)


def check(base_url: str, allow_local: bool | None = None) -> tuple[str, str, int, str, IP]:
    """Validate the address and resolve it; return (scheme, host, port, path, address to use).
    Every resolved address must pass, so a name with one bad record is refused."""
    allow = local_allowed() if allow_local is None else allow_local
    scheme, host, port, path = parse(base_url)
    fixed = literal(host)
    if fixed is not None:
        check_address(fixed, allow)  # link-local/metadata named first, whatever the scheme
    if scheme == "http" and (fixed is None or not is_local(fixed) or not allow):
        raise DestinationRefused(
            "base_url must use https (plain http is allowed only to a literal loopback or "
            f"private address, with {LOCAL_ENV}=1)"
        )
    try:
        addresses = [fixed] if fixed is not None else resolver(host, port)
    except (OSError, ValueError):
        raise DestinationRefused("base_url host name could not be resolved") from None
    if not addresses:
        raise DestinationRefused("base_url host name could not be resolved")
    for address in addresses:
        check_address(address, allow)
    return scheme, host, port, path, addresses[0]
