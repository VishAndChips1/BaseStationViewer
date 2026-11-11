"""Turn whatever the user typed into a safe host, and build URLs from it.

People paste `http://192.168.1.50:8080/`, `192.168.1.50:8080`, `rover.local`,
`[fe80::1]:8080` or a bare IPv6 address. Dropping any of those straight into
an f-string produced URLs like `http://http://1.2.3.4:8080:8080/latest.bin`,
which raises inside the worker thread. Everything that builds a URL goes
through here instead.
"""

import http.client
import ipaddress
import re
from urllib.parse import urlsplit

_NAME = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9._-]{0,251}[A-Za-z0-9])?$")


def parse_host_input(text: str) -> tuple[str, int | None]:
    """Return (host, port-or-None) from free-form input.

    Returns ("", None) for empty input. Raises ValueError with a message that is
    fit to show the user when the text cannot be a host.
    """
    t = (text or "").strip()
    if not t:
        return "", None
    if any(c.isspace() for c in t):
        raise ValueError("The address can't contain spaces")

    # A bare IPv6 address has several colons and would be mistaken for host:port.
    if "://" not in t and not t.startswith("[") and t.count(":") >= 2:
        try:
            return str(ipaddress.ip_address(t)), None
        except ValueError:
            pass

    try:
        parts = urlsplit(t if "://" in t else "//" + t)
        host = parts.hostname
        port = parts.port
    except ValueError as exc:
        raise ValueError("That address doesn't look right") from exc

    if not host:
        raise ValueError("That address doesn't look right")
    if port is not None and not 1 <= port <= 65535:
        raise ValueError("The port must be between 1 and 65535")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        if not _NAME.match(host):
            raise ValueError("That address doesn't look right") from None
    return host, port


def host_for_url(host: str) -> str:
    """IPv6 literals need brackets inside a URL."""
    return f"[{host}]" if ":" in host and not host.startswith("[") else host


def http_url(host: str, port: int, path: str) -> str:
    return f"http://{host_for_url(host)}:{int(port)}{path}"


def read_body(resp, limit: int) -> bytes:
    """Read a whole HTTP body, at most `limit` bytes (+1, so the caller can tell
    it was cut off).

    Raises http.client.IncompleteRead when the server hung up before delivering
    the Content-Length it promised. `resp.read(n)` does not: it returns the short
    body quietly, which then looks like a garbled reply instead of what it is,
    a dropped connection.
    """
    data = resp.read(limit + 1)
    declared = resp.headers.get("Content-Length")
    if declared and declared.isdigit() and len(data) <= limit and len(data) < int(declared):
        raise http.client.IncompleteRead(data, int(declared) - len(data))
    return data
