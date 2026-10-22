"""Local network discovery helpers."""
from __future__ import annotations

import ipaddress
import os
import socket


def is_private_lan(ip: str) -> bool:
    try:
        a = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return a.version == 4 and a.is_private and not a.is_loopback and not a.is_link_local


def is_ip_literal(host: str) -> bool:
    try:
        ipaddress.ip_address(host.strip("[]"))
        return True
    except ValueError:
        return False


def is_loopback(host: str) -> bool:
    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return False


def _primary_ip() -> str | None:
    """The address the OS would use for LAN traffic (no packets are sent)."""
    for probe in ("10.255.255.255", "192.168.255.255", "8.8.8.8"):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect((probe, 1))
            ip = s.getsockname()[0]
            if is_private_lan(ip):
                return ip
        except OSError:
            continue
        finally:
            s.close()
    return None


def lan_addresses() -> list[str]:
    """Private IPv4 addresses of this machine, best guess first."""
    found: list[str] = []
    primary = _primary_ip()
    if primary:
        found.append(primary)
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if is_private_lan(ip) and ip not in found:
                found.append(ip)
    except OSError:
        pass
    # Developer/test hook: lets selftest.py run on machines without a LAN.
    if os.environ.get("LOCALDROP_TEST_LOOPBACK") == "1":
        found.append("127.0.0.2")
    return found


def connection_url(ip: str, port: int) -> str:
    return f"http://{ip}:{port}"
