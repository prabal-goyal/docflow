"""Guards outbound webhook requests against server-side request forgery: a customer-supplied URL
must not make DocFlow call into its own network (databases, the cloud metadata service, ...)."""

import ipaddress
import socket
from collections.abc import Callable
from urllib.parse import urlsplit

Resolver = Callable[[str], list[str]]


class UnsafeUrlError(ValueError):
    """The URL is not an acceptable webhook target."""


def resolve_host(host: str) -> list[str]:
    """All addresses the host resolves to (IPv4 and IPv6)."""
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except socket.gaierror as error:
        raise UnsafeUrlError(f"Cannot resolve host {host!r}") from error
    return sorted({str(info[4][0]) for info in infos})


def is_public(address: str) -> bool:
    ip = ipaddress.ip_address(address)
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped  # ::ffff:10.0.0.1 is really 10.0.0.1
    # is_global is False for private, loopback, link-local (169.254.x.x, the cloud metadata
    # service), shared, reserved and unspecified ranges.
    return ip.is_global and not ip.is_multicast


def validate_target(url: str, allow_private: bool, resolve: Resolver = resolve_host) -> None:
    """Raises UnsafeUrlError unless the URL is HTTPS and every address of its host is public.
    allow_private is for local development only (a mock receiver inside Docker)."""
    parts = urlsplit(url)
    if parts.scheme != "https" and not (allow_private and parts.scheme == "http"):
        raise UnsafeUrlError("Webhook URLs must use https")
    if not parts.hostname:
        raise UnsafeUrlError("Webhook URL has no host")
    if parts.username or parts.password:
        raise UnsafeUrlError("Webhook URLs must not contain credentials")
    if allow_private:
        return
    addresses = resolve(parts.hostname)
    if not addresses:
        raise UnsafeUrlError(f"Host {parts.hostname!r} has no addresses")
    blocked = [address for address in addresses if not is_public(address)]
    if blocked:
        raise UnsafeUrlError(f"Host {parts.hostname!r} resolves to a non-public address")
