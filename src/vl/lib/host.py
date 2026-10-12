"""Whether a host the control plane reports is this machine (vl-node-spec.md §4c).

"On this machine" means the CP's ``host`` equals this machine's hostname or any
address on any of its interfaces. The interface addresses come from Linux
(``SIOCGIFADDR`` per interface for IPv4, ``/proc/net/if_inet6`` for IPv6), the
same platform the §4c process checks assume; anything that can't be read is
skipped rather than failing the command.
"""

from __future__ import annotations

import ipaddress
import socket
import struct
from collections.abc import Iterable
from pathlib import Path

_SIOCGIFADDR = 0x8915
_IF_INET6 = Path("/proc/net/if_inet6")


def _normalize(host: str) -> str:
    """An IP in canonical form (zone and brackets dropped), else a lowercased name."""
    h = host.strip().strip("[]")
    try:
        return str(ipaddress.ip_address(h.split("%", 1)[0]))
    except ValueError:
        return h.lower().rstrip(".")


def _ipv4_interface_addresses() -> set[str]:
    try:
        import fcntl
    except ImportError:  # not POSIX
        return set()
    found: set[str] = set()
    try:
        names = [name for _, name in socket.if_nameindex()]
    except OSError:
        return found
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        for name in names:
            try:
                packed = fcntl.ioctl(
                    s.fileno(), _SIOCGIFADDR, struct.pack("256s", name.encode()[:15])
                )
            except OSError:
                continue  # interface has no IPv4 address
            found.add(socket.inet_ntoa(packed[20:24]))
    return found


def _ipv6_interface_addresses(path: Path = _IF_INET6) -> set[str]:
    found: set[str] = set()
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return found
    for line in lines:
        fields = line.split()
        if not fields or len(fields[0]) != 32:
            continue
        try:
            found.add(str(ipaddress.IPv6Address(int(fields[0], 16))))
        except ValueError:
            continue
    return found


def _resolved_addresses(names: Iterable[str]) -> set[str]:
    found: set[str] = set()
    for name in names:
        try:
            infos = socket.getaddrinfo(name, None)
        except OSError:
            continue
        for info in infos:
            found.add(str(info[4][0]))
    return found


def local_identities() -> set[str]:
    """This machine's hostnames and interface addresses, normalized."""
    names = {n for n in (socket.gethostname(), socket.getfqdn()) if n}
    raw = names | _ipv4_interface_addresses() | _ipv6_interface_addresses() | _resolved_addresses(names)
    return {_normalize(h) for h in raw}


def is_this_machine(host: str | None, local: set[str] | None = None) -> bool:
    """Whether ``host`` (as the CP reports it) names this machine."""
    if not host or not host.strip():
        return False
    if local is None:
        local = local_identities()
    return _normalize(host) in local
