"""Validate server-confirmed RDP endpoints without accepting credentials in status."""
from .ssh_endpoint import parse_ssh_endpoint


def parse_rdp_endpoint(data):
    if "rdp_access" not in data:
        return None, None
    endpoint = data["rdp_access"]
    if endpoint is None:
        return "", 3389
    if (not isinstance(endpoint, dict)
            or set(endpoint) != {"host", "port", "username"}
            or endpoint["username"] != "Admin"):
        raise ValueError("Invalid RDP endpoint.")
    return parse_ssh_endpoint({"ssh_access": {
        "host": endpoint["host"], "port": endpoint["port"],
    }})
