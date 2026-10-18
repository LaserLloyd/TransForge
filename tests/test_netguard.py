"""The socket guard itself: it must refuse what it promises to refuse."""
import socket
import unittest

import _netguard

_netguard.install(strict_exit=True)


class TestGuard(unittest.TestCase):
    def setUp(self):
        self.addCleanup(_netguard.VIOLATIONS.clear)   # these refusals are expected

    def _refused(self, address):
        s = socket.socket()
        self.addCleanup(s.close)
        with self.assertRaises(_netguard.NetworkBlockedError):
            s.connect(address)

    def test_non_loopback_is_refused(self):
        self._refused(("192.0.2.10", 443))
        self._refused(("203.0.113.2", 1234))

    def test_reserved_loopback_ports_are_refused(self):
        for port in (1234, 1235, 1236, 8080, 8700, 8288, 7865, 8765, 8777):
            self._refused(("127.0.0.1", port))

    def test_connect_ex_is_guarded_too(self):
        s = socket.socket()
        self.addCleanup(s.close)
        with self.assertRaises(_netguard.NetworkBlockedError):
            s.connect_ex(("127.0.0.1", 1234))

    def test_made_up_hostnames_are_not_resolved(self):
        with self.assertRaises(_netguard.NetworkBlockedError):
            socket.getaddrinfo("rig", 1234)

    def test_ephemeral_loopback_port_the_test_opens_is_allowed(self):
        srv = socket.socket()
        self.addCleanup(srv.close)
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        cli = socket.socket()
        self.addCleanup(cli.close)
        cli.connect(srv.getsockname())     # must not raise
        self.assertEqual(_netguard.VIOLATIONS, [])


    def _listener(self):
        srv = socket.socket()
        self.addCleanup(srv.close)
        srv.bind(("127.0.0.1", 0))
        srv.listen(4)
        return srv.getsockname()[1]

    def test_asyncio_connections_are_guarded_too(self):
        """asyncio's default loop on Windows (proactor / ConnectEx) never calls
        socket.connect; the guard must still refuse a reserved port there.
        Uses a listener this test owns, temporarily treated as reserved."""
        import asyncio

        port = self._listener()
        saved = _netguard.BLOCKED_LOOPBACK_PORTS
        _netguard.BLOCKED_LOOPBACK_PORTS = frozenset({port})
        self.addCleanup(setattr, _netguard, "BLOCKED_LOOPBACK_PORTS", saved)

        async def attempt():
            _reader, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.close()

        with self.assertRaises(OSError):
            asyncio.run(attempt())
        self.assertTrue(any(str(port) in v for v in _netguard.VIOLATIONS), _netguard.VIOLATIONS)

    def test_asyncio_connection_to_an_ephemeral_port_is_allowed(self):
        import asyncio

        port = self._listener()

        async def attempt():
            _reader, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.close()

        asyncio.run(attempt())
        self.assertEqual(_netguard.VIOLATIONS, [])


if __name__ == "__main__":
    unittest.main()
