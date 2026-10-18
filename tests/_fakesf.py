"""A tiny in-process fake StudioForge for tests: a real HTTP server on an
EPHEMERAL loopback port (the socket guard allows exactly that), recording every
request so a test can assert the headers and bodies that went on the wire.

    with FakeStudioForge() as sf:
        sf.route("GET", "/api/leases", 200, {"leases": []})
        rig = tf.Rig(make_site(endpoint=sf.url))
        ...
        sf.requests[0]["headers"]["Authorization"]

Never points at a real StudioForge; never uses a fixed port.
"""
import http.server
import json
import socketserver
import threading


class _Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):          # keep test output clean
        pass

    def _serve(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b""
        try:
            body = json.loads(raw.decode("utf-8")) if raw else None
        except ValueError:
            body = raw
        srv = self.server
        srv.requests.append({"method": self.command, "path": self.path,
                             "headers": self.headers,          # case-insensitive HTTPMessage
                             "body": body})
        status, payload, extra = srv.lookup(self.command, self.path)
        data = (payload if isinstance(payload, (bytes, bytearray))
                else json.dumps(payload).encode("utf-8"))
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        for k, v in extra.items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    do_GET = do_POST = do_DELETE = do_PUT = _serve


class _Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True

    def server_bind(self):               # skip HTTPServer's getfqdn() lookup
        socketserver.TCPServer.server_bind(self)
        self.server_name = "localhost"

    def lookup(self, method, path):
        p = path.split("?", 1)[0]
        for (m, pat, exact), resp in self.routes:
            if m == method and ((exact and p == pat) or (not exact and p.startswith(pat))):
                return resp
        return 404, {"error": {"code": "not_found"}}, {}


class FakeStudioForge:
    def __init__(self):
        self._srv = _Server(("127.0.0.1", 0), _Handler)
        self._srv.requests = []
        self._srv.routes = []
        self.requests = self._srv.requests
        self.url = "http://127.0.0.1:%d" % self._srv.server_address[1]
        self._t = threading.Thread(target=self._srv.serve_forever,
                                   kwargs={"poll_interval": 0.02}, daemon=True)

    def route(self, method, path, status, payload, headers=None, exact=True):
        self._srv.routes.insert(0, ((method, path, exact),
                                    (status, payload, headers or {})))

    def __enter__(self):
        self._t.start()
        return self

    def __exit__(self, *exc):
        self._srv.shutdown()
        self._srv.server_close()
        self._t.join(5)
        return False
