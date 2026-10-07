"""Validate the optional, server-confirmed public SSH endpoint."""
import re


def parse_ssh_endpoint(data):
    if "ssh_access" not in data:
        return None, None  # Older provisioners must not erase manually saved access.
    endpoint = data["ssh_access"]
    if endpoint is None:
        return "", 22
    if not isinstance(endpoint, dict) or set(endpoint) != {"host", "port"}:
        raise ValueError("Invalid SSH endpoint.")
    host, port = endpoint["host"], endpoint["port"]
    label = r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
    if (not isinstance(host, str) or len(host) > 253
            or not re.fullmatch(rf"{label}(?:\.{label})*", host)
            or type(port) is not int or not 1 <= port <= 65535):
        raise ValueError("Invalid SSH endpoint.")
    return host, port
