"""Shared request-origin checks for browser-only control surfaces."""
from __future__ import annotations

from urllib.parse import urlsplit


def same_origin(headers) -> bool:
    """Return True only when a browser request's Origin matches its Host."""
    origin = headers.get("origin", "")
    host = headers.get("host", "")
    if not origin or not host or origin == "null":
        return False
    try:
        parsed_origin = urlsplit(origin)
        parsed_host = urlsplit("//" + host)
        if parsed_origin.scheme not in ("http", "https"):
            return False
        if (
            parsed_origin.username or parsed_origin.password
            or parsed_origin.path not in ("", "/")
            or parsed_origin.query or parsed_origin.fragment
            or parsed_host.username or parsed_host.password
            or parsed_host.path or parsed_host.query or parsed_host.fragment
        ):
            return False
        origin_name = (parsed_origin.hostname or "").lower().rstrip(".")
        host_name = (parsed_host.hostname or "").lower().rstrip(".")
        if not origin_name or origin_name != host_name:
            return False
        default_port = 443 if parsed_origin.scheme == "https" else 80
        origin_port = parsed_origin.port or default_port
        host_port = parsed_host.port or default_port
        return origin_port == host_port
    except ValueError:
        return False
