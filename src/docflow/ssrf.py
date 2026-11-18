"""Guards outbound webhook requests against server-side request forgery: a customer-supplied URL
must not make DocFlow call into its own network (databases, the cloud metadata service, ...)."""

import ipaddress
import socket
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit

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


def validate_target(url: str, allow_private: bool, resolve: Resolver = resolve_host) -> list[str]:
    """Raises UnsafeUrlError unless the URL is HTTPS and every address of its host is public.
    Returns the vetted addresses (empty when allow_private skips the check: local development
    only, for a mock receiver inside Docker)."""
    parts = urlsplit(url)
    if parts.scheme != "https" and not (allow_private and parts.scheme == "http"):
        raise UnsafeUrlError("Webhook URLs must use https")
    if not parts.hostname:
        raise UnsafeUrlError("Webhook URL has no host")
    if parts.username or parts.password:
        raise UnsafeUrlError("Webhook URLs must not contain credentials")
    if allow_private:
        return []
    addresses = resolve(parts.hostname)
    if not addresses:
        raise UnsafeUrlError(f"Host {parts.hostname!r} has no addresses")
    blocked = [address for address in addresses if not is_public(address)]
    if blocked:
        raise UnsafeUrlError(f"Host {parts.hostname!r} resolves to a non-public address")
    return addresses


@dataclass(frozen=True)
class PinnedTarget:
    """Where to connect, and the name to present for HTTP (Host) and TLS (SNI and certificate)."""

    url: str
    host_header: str | None = None
    sni_hostname: str | None = None


def pin_to_vetted_address(url: str, addresses: list[str]) -> PinnedTarget:
    """Connect to an address the SSRF check approved instead of letting the HTTP client resolve
    the name again. That closes the DNS-rebinding window (a second lookup could return an internal
    address), and choosing IPv4 suits a server whose Docker network has no IPv6. The certificate
    is still verified against the hostname, through SNI."""
    if not addresses:
        return PinnedTarget(url)  # local development: no check was made, nothing to pin
    ipv4 = [a for a in addresses if ipaddress.ip_address(a).version == 4]
    if not ipv4:
        raise UnsafeUrlError("The webhook host has no IPv4 address, which this server needs")
    parts = urlsplit(url)
    hostname = parts.hostname or ""
    port = f":{parts.port}" if parts.port else ""
    return PinnedTarget(
        url=urlunsplit(parts._replace(netloc=f"{ipv4[0]}{port}")),
        host_header=f"{hostname}{port}",
        sni_hostname=hostname,
    )
