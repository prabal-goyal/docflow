"""Webhook signing and SSRF protection. Pure functions, no database or network."""

import pytest

from docflow.ssrf import UnsafeUrlError, is_public, validate_target
from docflow.webhooks import canonical_body, sign, verify

SECRET = "whsec_test"
TIMESTAMP = 1795000000
BODY = b'{"id":"evt_1","type":"submission.approved"}'
# Computed independently with hmac.new(SECRET, f"{TIMESTAMP}.".encode() + BODY, sha256).
EXPECTED = "v1=325345cad6646e831af39ac98366bba6c94fbe8dde221bf61ae7c68ff0588118"


# --- signing -----------------------------------------------------------------------------------


def test_signature_matches_a_known_value() -> None:
    assert sign(SECRET, TIMESTAMP, BODY) == EXPECTED


def test_valid_signature_verifies() -> None:
    assert verify(SECRET, str(TIMESTAMP), EXPECTED, BODY, now=TIMESTAMP + 10)


@pytest.mark.parametrize(
    ("secret", "timestamp", "body", "now"),
    [
        ("whsec_other", str(TIMESTAMP), BODY, TIMESTAMP),  # wrong secret
        (SECRET, str(TIMESTAMP), BODY + b" ", TIMESTAMP),  # body changed
        (SECRET, str(TIMESTAMP + 1), BODY, TIMESTAMP),  # timestamp changed
        (SECRET, str(TIMESTAMP), BODY, TIMESTAMP + 301),  # replayed 5+ minutes later
        (SECRET, "not-a-number", BODY, TIMESTAMP),
    ],
)
def test_tampered_or_stale_requests_fail(
    secret: str, timestamp: str, body: bytes, now: int
) -> None:
    assert not verify(secret, timestamp, EXPECTED, body, now=now)


def test_canonical_body_is_deterministic() -> None:
    assert canonical_body({"b": 1, "a": [2, 3]}) == canonical_body({"a": [2, 3], "b": 1})
    assert canonical_body({"b": 1, "a": 2}) == b'{"a":2,"b":1}'


# --- SSRF --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "address",
    [
        "10.0.0.5",
        "172.16.3.4",
        "192.168.1.1",
        "127.0.0.1",
        "169.254.169.254",  # cloud metadata service
        "100.64.0.1",  # carrier-grade NAT
        "0.0.0.0",
        "::1",
        "fd00::1",
        "fe80::1",
        "::ffff:10.0.0.1",  # IPv4-mapped private address
        "224.0.0.1",  # multicast
    ],
)
def test_non_public_addresses_are_blocked(address: str) -> None:
    assert not is_public(address)


@pytest.mark.parametrize("address", ["93.184.216.34", "2606:2800:220:1:248:1893:25c8:1946"])
def test_public_addresses_are_allowed(address: str) -> None:
    assert is_public(address)


def test_https_url_with_public_address_is_accepted() -> None:
    validate_target("https://hooks.customer.example/docflow", False, lambda host: ["93.184.216.34"])


@pytest.mark.parametrize(
    "url",
    [
        "http://hooks.customer.example/x",  # not https
        "ftp://hooks.customer.example/x",
        "https:///no-host",
        "https://user:pass@hooks.customer.example/x",  # credentials in the URL
    ],
)
def test_malformed_or_insecure_urls_are_rejected(url: str) -> None:
    with pytest.raises(UnsafeUrlError):
        validate_target(url, False, lambda host: ["93.184.216.34"])


def test_host_resolving_to_any_private_address_is_rejected() -> None:
    """DNS can return several addresses; one internal address is enough to refuse."""
    with pytest.raises(UnsafeUrlError, match="non-public"):
        validate_target(
            "https://evil.example/x", False, lambda host: ["93.184.216.34", "169.254.169.254"]
        )


def test_local_development_allows_http_and_private_targets() -> None:
    validate_target("http://receiver:9000/hook", True, lambda host: ["172.18.0.5"])
