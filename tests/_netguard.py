"""Test-suite socket guard (hermetic tests).

Importing and calling ``install()`` makes every outbound connect that is not
to an *ephemeral loopback port the test itself opened* fail loudly:

* any connect to a non-loopback address is refused;
* any connect to a loopback port that a live service of the household may own
  (1234 1235 1236 8080 8700 8288 7865 8765 8777) is refused;
* name resolution of anything but localhost / IP literals is refused (a
  made-up hostname such as ``rig`` could resolve on the owner's LAN).

A refused attempt raises ``NetworkBlockedError`` (a ``ConnectionRefusedError``,
so code that handles "server down" still behaves) AND is recorded in
``VIOLATIONS`` so the suite can fail even when the code under test swallowed
the error.  Tests must use fakes (``monkeypatch``/``mock``/an in-process
server on port 0), never a real endpoint.

This file is identical in CrucibleForge, TransForge and MailForge on purpose.
"""
from __future__ import annotations

import atexit
import ipaddress
import os
import socket
import sys

BLOCKED_LOOPBACK_PORTS = frozenset(
    {1234, 1235, 1236, 8080, 8700, 8288, 7865, 8765, 8777})

VIOLATIONS: list[str] = []
_installed = False


class NetworkBlockedError(ConnectionRefusedError):
    """Raised for any connect/resolve the hermetic-test guard refuses."""


def _host_str(host) -> str:
    if isinstance(host, (bytes, bytearray)):
        host = host.decode("ascii", "replace")
    return str(host or "")


def _is_loopback_host(host) -> bool:
    h = _host_str(host).split("%", 1)[0].strip("[]")
    if h == "" or h.lower() == "localhost":
        return True          # "" / None = wildcard bind or local resolution
    try:
        ip = ipaddress.ip_address(h)
    except ValueError:
        return False
    if getattr(ip, "ipv4_mapped", None) is not None:
        ip = ip.ipv4_mapped
    return ip.is_loopback


def _refuse(what: str):
    VIOLATIONS.append(what)
    if os.environ.get("NETGUARD_TRACE"):      # debugging aid: who tried to connect?
        import traceback
        traceback.print_stack(limit=12)
    raise NetworkBlockedError(f"test network guard: refused {what}")


def check_address(address) -> None:
    """Raise NetworkBlockedError unless *address* is an allowed connect target."""
    if not isinstance(address, tuple) or len(address) < 2:
        return               # AF_UNIX path etc.: not network
    host, port = address[0], address[1]
    if not _is_loopback_host(host):
        _refuse(f"connect to non-loopback {_host_str(host)}:{port}")
    try:
        port = int(port)
    except (TypeError, ValueError):
        return
    if port in BLOCKED_LOOPBACK_PORTS:
        _refuse(f"connect to reserved loopback port {_host_str(host)}:{port}")


def _guard_asyncio_proactor() -> None:
    """Guard asyncio's Windows proactor too.

    ``ProactorEventLoop`` (the default loop on Windows) connects with
    ``ConnectEx`` on the raw socket handle and never calls
    ``socket.socket.connect``, so without this every asyncio / anyio / httpx
    ``AsyncClient`` connection bypassed the guard on Windows.  Selector loops
    (Linux, macOS) already go through the patched ``socket.connect``.
    """
    try:
        from asyncio import windows_events
    except ImportError:          # not Windows
        return
    proactor = getattr(windows_events, "IocpProactor", None)
    real = getattr(proactor, "connect", None)
    if real is None or getattr(real, "_netguard", False):
        return

    def connect(self, conn, address):
        check_address(address)
        return real(self, conn, address)

    connect._netguard = True
    proactor.connect = connect


def install(strict_exit: bool = False) -> None:
    """Install the guard (idempotent).

    strict_exit: also force a non-zero process exit at interpreter shutdown if
    any attempt was refused (for runners without a session hook, e.g.
    ``python -m unittest``).  pytest suites use ``pytest_sessionfinish``.
    """
    global _installed
    if _installed:
        return
    _installed = True

    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex
    real_getaddrinfo = socket.getaddrinfo

    def connect(self, address):
        check_address(address)
        return real_connect(self, address)

    def connect_ex(self, address):
        check_address(address)
        return real_connect_ex(self, address)

    def getaddrinfo(host, *args, **kwargs):
        if host is not None and not _is_loopback_host(host):
            try:
                ipaddress.ip_address(_host_str(host).split("%", 1)[0])
            except ValueError:
                _refuse(f"name resolution of {_host_str(host)!r}")
        return real_getaddrinfo(host, *args, **kwargs)

    socket.socket.connect = connect
    socket.socket.connect_ex = connect_ex
    socket.getaddrinfo = getaddrinfo
    _guard_asyncio_proactor()

    if strict_exit:
        def _report():
            if VIOLATIONS:
                sys.stderr.write(
                    "\nNETGUARD: %d refused network attempt(s):\n  %s\n"
                    % (len(VIOLATIONS), "\n  ".join(VIOLATIONS)))
                sys.stderr.flush()
                sys.stdout.flush()
                os._exit(3)
        atexit.register(_report)
