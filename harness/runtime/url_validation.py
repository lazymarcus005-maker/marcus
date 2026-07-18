"""Shared SSRF-resistant URL validation for fetch_url across CLI and server."""

import ipaddress
import socket
from urllib.parse import urlparse


def validate_public_url(url: str) -> None:
    """Block non-public http(s) URLs and resist DNS rebinding.

    The host must resolve to exactly one IP address and that address must be
    globally routable. Redirects must be re-validated by callers.
    """
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError("fetch_url only supports public http(s) URLs")
    try:
        addresses = {
            info[4][0]
            for info in socket.getaddrinfo(
                parsed.hostname,
                parsed.port or (443 if parsed.scheme == "https" else 80),
                type=socket.SOCK_STREAM,
            )
        }
    except OSError as exc:
        raise ValueError(f"could not resolve URL host: {parsed.hostname}") from exc
    if not addresses:
        raise ValueError("URL host has no addresses")
    if len(addresses) != 1:
        raise ValueError("fetch_url refuses hosts with multiple DNS addresses")
    for address in addresses:
        ip = ipaddress.ip_address(address)
        if not ip.is_global:
            raise ValueError(
                "fetch_url refuses loopback, private, link-local, or reserved addresses"
            )
