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


if __name__ == "__main__":
    unittest.main()
