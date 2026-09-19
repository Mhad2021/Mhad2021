"""Work out which address other machines on the network can reach us on.

Used by the ``serve`` command so nobody has to hunt through ifconfig output
to tell their team what URL to open.
"""
from __future__ import annotations

import ipaddress
import socket


def primary_lan_ip() -> str | None:
    """The address this machine uses to reach the local network.

    Opens a UDP socket toward a public address and reads back the local end.
    No packet is sent, and it works with no internet connection, because UDP
    connect only sets the route.
    """
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("8.8.8.8", 80))
        address = probe.getsockname()[0]
    except OSError:
        return None
    finally:
        probe.close()

    return address if _is_private(address) else None


def _is_private(address: str) -> bool:
    try:
        parsed = ipaddress.ip_address(address)
    except ValueError:
        return False
    return parsed.is_private and not parsed.is_loopback


def mdns_hostname() -> str | None:
    """This machine's .local name, if the network can resolve it.

    macOS and Windows 10+ both answer mDNS queries for <hostname>.local, and
    the name follows the machine when DHCP hands it a different address. For a
    LAN deployment that matters: an agent configured with a raw IP breaks the
    next time the lease changes, while one configured with a name does not.
    """
    import sys

    raw = socket.gethostname()
    if not raw or raw in ("localhost", "localhost.localdomain"):
        return None

    # macOS already reports the Bonjour name; elsewhere append .local.
    name = raw if raw.endswith(".local") else f"{raw.split('.')[0]}.local"

    # Only suggest it where the OS actually runs an mDNS responder.
    if sys.platform not in ("darwin", "win32"):
        return None

    return name.lower()


def all_lan_ips() -> list[str]:
    """Every private address this host answers on, best guess first."""
    found: list[str] = []

    if primary := primary_lan_ip():
        found.append(primary)

    try:
        hostname = socket.gethostname()
        for info in socket.getaddrinfo(hostname, None, socket.AF_INET):
            address = info[4][0]
            if _is_private(address) and address not in found:
                found.append(address)
    except (OSError, socket.gaierror):
        pass

    return found
