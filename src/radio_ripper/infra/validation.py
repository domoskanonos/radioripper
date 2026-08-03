"""URL validation utilities for radio_ripper."""

from __future__ import annotations

from urllib.parse import urlparse

from radio_ripper.infra.errors import InvalidUrlError

# Disallowed URL schemes to prevent SSRF attacks
_DISALLOWED_SCHEMES = frozenset({"file", "ftp", "gopher", "data", "javascript", "vbscript"})

# Reserved IPv4 ranges that should not be accessed (RFC 1918, RFC 3927, etc.)
_RESERVED_IP_PREFIXES = (
    "0.",  # Current network
    "10.",  # Private network
    "127.",  # Loopback
    "169.254.",  # Link-local
    "172.16.",  # Private network (172.16.0.0 - 172.31.255.255)
    "172.17.",
    "172.18.",
    "172.19.",
    "172.20.",
    "172.21.",
    "172.22.",
    "172.23.",
    "172.24.",
    "172.25.",
    "172.26.",
    "172.27.",
    "172.28.",
    "172.29.",
    "172.30.",
    "172.31.",
    "192.168.",  # Private network
    "224.",  # Multicast (224.0.0.0 - 239.255.255.255)
    "225.",
    "226.",
    "227.",
    "228.",
    "229.",
    "230.",
    "231.",
    "232.",
    "233.",
    "234.",
    "235.",
    "236.",
    "237.",
    "238.",
    "239.",
    "240.",  # Reserved (240.0.0.0 - 255.255.255.255)
)


def validate_stream_url(url: str) -> str:
    """
    Validate a stream URL for security and protocol compliance.

    Args:
        url: The URL to validate

    Returns:
        The validated URL (normalized)

    Raises:
        InvalidUrlError: If the URL is invalid, uses a disallowed scheme,
                        or points to a reserved/private IP address
    """
    if not url or not isinstance(url, str):
        raise InvalidUrlError("URL must be a non-empty string")

    url = url.strip()

    try:
        parsed = urlparse(url)
    except Exception as e:
        raise InvalidUrlError(f"Failed to parse URL: {e}") from e

    # Check scheme
    scheme = parsed.scheme.lower()
    if not scheme:
        raise InvalidUrlError("URL must have a scheme (e.g., http:// or https://)")

    if scheme in _DISALLOWED_SCHEMES:
        raise InvalidUrlError(f"URL scheme '{scheme}' is not allowed for security reasons")

    if scheme not in ("http", "https"):
        raise InvalidUrlError(f"Only http and https schemes are supported, got: {scheme}")

    # Check hostname
    hostname = parsed.hostname
    if not hostname:
        raise InvalidUrlError("URL must have a valid hostname")

    # Check for localhost variants
    if hostname.lower() in ("localhost", "localhost.localdomain"):
        raise InvalidUrlError("Cannot access localhost URLs")

    # Check for reserved/private IP addresses
    # Note: This is a basic check. For production, consider using ipaddress module
    # for more robust IP validation
    if hostname.replace(".", "").isdigit():  # Looks like an IP
        for prefix in _RESERVED_IP_PREFIXES:
            if hostname.startswith(prefix):
                raise InvalidUrlError(f"Cannot access reserved/private IP address: {hostname}")

    return url


__all__ = ["validate_stream_url"]
