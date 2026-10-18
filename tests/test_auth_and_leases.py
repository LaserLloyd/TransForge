"""StudioForge bearer-key support and the fail-closed lease read.

A fake StudioForge runs in-process on an ephemeral loopback port
(tests/_fakesf.py); the socket guard forbids anything else.

Run from the repo root:  python -m unittest discover -s tests
"""
import argparse
import contextlib
import importlib.util
import io
import os
import socket
import sys
import unittest
import urllib.error
from unittest import mock

import _netguard

_netguard.install(strict_exit=True)

from _fakesf import FakeStudioForge  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_spec = importlib.util.spec_from_file_location(
    "transforge_auth_mod", os.path.join(REPO, "transforge.py"))
tf = importlib.util.module_from_spec(_spec)
sys.modules["transforge_auth_mod"] = tf
_spec.loader.exec_module(tf)

KEY = "sk-test-0123456789abcdef"
KEY_ENVS = ("TRANSFORGE_LLM_API_KEY", "STUDIOFORGE_API_KEY", "STUDIOFORGE_MCP_PIN",
            "MY_CUSTOM_KEY_VAR")


def clean_env(**extra):
    """os.environ without any key/PIN variables, plus *extra*, and a proxy
    bypass for loopback (a developer machine may export HTTP_PROXY)."""
    env = {k: v for k, v in os.environ.items() if k not in KEY_ENVS}
    env["NO_PROXY"] = env["no_proxy"] = "127.0.0.1,localhost"
    env.update(extra)
    return env


def make_site(**site):
    return tf.SiteConfig("t", {}, site)


OK_CHAT = {"model": "vendor/m", "choices": [{"message": {"content": "ok"},
                                              "finish_reason": "stop"}]}


class _EnvCase(unittest.TestCase):
    def use_env(self, **extra):
        p = mock.patch.dict(os.environ, clean_env(**extra), clear=True)
        p.start()
        self.addCleanup(p.stop)
        q = mock.patch.object(tf, "PIN_ENV_FILE", "")
        q.start()
        self.addCleanup(q.stop)


class TestBearerKeyResolution(_EnvCase):
    def test_no_key_by_default(self):
        self.use_env()
        rig = tf.Rig(make_site())
        self.assertEqual(rig.api_key(), "")
        self.assertEqual(rig.api_key_source(), "")

    def test_primary_env_var(self):
        self.use_env(TRANSFORGE_LLM_API_KEY=KEY)
        rig = tf.Rig(make_site())
        self.assertEqual(rig.api_key(), KEY)
        self.assertEqual(rig.api_key_source(), "env TRANSFORGE_LLM_API_KEY")

    def test_shared_studioforge_env_var_is_a_fallback(self):
        self.use_env(STUDIOFORGE_API_KEY="shared-key")
        self.assertEqual(tf.Rig(make_site()).api_key(), "shared-key")

    def test_primary_beats_fallback_beats_config(self):
        self.use_env(TRANSFORGE_LLM_API_KEY="primary", STUDIOFORGE_API_KEY="shared")
        self.assertEqual(tf.Rig(make_site(api_key="cfg")).api_key(), "primary")
        self.use_env(STUDIOFORGE_API_KEY="shared")
        self.assertEqual(tf.Rig(make_site(api_key="cfg")).api_key(), "shared")
        self.use_env()
        rig = tf.Rig(make_site(api_key="cfg"))
        self.assertEqual(rig.api_key(), "cfg")
        self.assertEqual(rig.api_key_source(), "config")

    def test_api_key_env_names_a_custom_variable_and_wins(self):
        self.use_env(MY_CUSTOM_KEY_VAR="custom", TRANSFORGE_LLM_API_KEY="primary")
        rig = tf.Rig(make_site(api_key_env="MY_CUSTOM_KEY_VAR"))
        self.assertEqual(rig.api_key(), "custom")

    def test_whitespace_is_stripped(self):
        self.use_env(TRANSFORGE_LLM_API_KEY="  " + KEY + "\r\n")
        self.assertEqual(tf.Rig(make_site()).api_key(), KEY)

    def test_key_is_not_part_of_the_translation_config_hash(self):
        a = make_site(api_key="one").cfg_hash()
        b = make_site(api_key="two").cfg_hash()
        self.assertEqual(a, b)   # rotating a key must not mark pages STALE


class TestBearerHeaderOnTheWire(_EnvCase):
    def _server(self):
        sf = FakeStudioForge()
        sf.route("GET", "/v1/models", 200, {"data": [{"id": "vendor/m"}]})
        sf.route("GET", "/api/status", 200, {"loaded": []})
        sf.route("GET", "/api/leases", 200, {"leases": []})
        sf.route("POST", "/v1/chat/completions", 200, OK_CHAT)
        sf.route("POST", "/api/models/", 200, {"ok": True}, exact=False)
        return sf

    def test_every_request_carries_the_bearer_header(self):
        self.use_env(TRANSFORGE_LLM_API_KEY=KEY, STUDIOFORGE_MCP_PIN="1234abcd")
        with self._server() as sf:
            rig = tf.Rig(make_site(endpoint=sf.url, model="vendor/m"))
            rig.models()
            rig.status()
            rig.leases()
            rig.chat([{"role": "user", "content": "hi"}], 10, 0.3)
            rig.load_recommended("vendor/m", 16384, 3)
            rig.unload("vendor/m")
        self.assertEqual(len(sf.requests), 6)
        for r in sf.requests:
            self.assertEqual(r["headers"].get("Authorization"), "Bearer " + KEY,
                             r["path"])
            self.assertEqual(r["headers"].get("X-SF-Client"), "transforge")

    def test_no_key_means_no_authorization_header(self):
        self.use_env()
        with self._server() as sf:
            tf.Rig(make_site(endpoint=sf.url)).models()
        self.assertNotIn("Authorization", sf.requests[0]["headers"])

    def test_config_key_is_sent_too(self):
        self.use_env()
        with self._server() as sf:
            tf.Rig(make_site(endpoint=sf.url, api_key=KEY)).status()
        self.assertEqual(sf.requests[0]["headers"]["Authorization"], "Bearer " + KEY)

    def test_pin_still_sent_on_load_and_unload_but_not_elsewhere(self):
        """Deprecated for one release: X-MCP-Pin keeps riding load/unload."""
        self.use_env(TRANSFORGE_LLM_API_KEY=KEY, STUDIOFORGE_MCP_PIN="1234abcd")
        with self._server() as sf:
            rig = tf.Rig(make_site(endpoint=sf.url))
            rig.models()
            rig.load_recommended("vendor/m", 16384, 3)
            rig.unload("vendor/m")
        by_path = {r["path"].split("/")[-1]: r["headers"] for r in sf.requests}
        self.assertNotIn("X-MCP-Pin", by_path["models"])
        self.assertEqual(by_path["load-recommended"]["X-MCP-Pin"], "1234abcd")
        self.assertEqual(by_path["unload"]["X-MCP-Pin"], "1234abcd")

    def test_api_key_alone_is_enough_for_load(self):
        self.use_env(TRANSFORGE_LLM_API_KEY=KEY)
        with self._server() as sf:
            tf.Rig(make_site(endpoint=sf.url)).load_recommended("vendor/m", 8192, 3)
        h = sf.requests[0]["headers"]
        self.assertEqual(h["Authorization"], "Bearer " + KEY)
        self.assertNotIn("X-MCP-Pin", h)

    def test_load_without_any_credential_is_still_refused_locally(self):
        self.use_env()
        rig = tf.Rig(make_site(endpoint="http://127.0.0.1:9"))
        with self.assertRaises(RuntimeError) as cm:
            rig.load_recommended("vendor/m", 8192, 3)
        self.assertIn("TRANSFORGE_LLM_API_KEY", str(cm.exception))


class TestKeyIsNeverShown(_EnvCase):
    def test_config_command_reports_source_not_value(self):
        self.use_env(TRANSFORGE_LLM_API_KEY=KEY)
        site = make_site(api_key="literal-config-key-value")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            tf.cmd_config(argparse.Namespace(site=None), {"t": site})
        text = out.getvalue()
        self.assertIn("api_key = (set; from env TRANSFORGE_LLM_API_KEY)", text)
        self.assertNotIn(KEY, text)
        self.assertNotIn("literal-config-key-value", text)

    def test_config_command_marks_literal_config_key_without_printing_it(self):
        self.use_env()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            tf.cmd_config(argparse.Namespace(site=None),
                          {"t": make_site(api_key="literal-config-key-value")})
        self.assertIn("api_key = (set; from config)", out.getvalue())
        self.assertNotIn("literal-config-key-value", out.getvalue())

    def test_refusal_messages_do_not_contain_the_key(self):
        self.use_env(TRANSFORGE_LLM_API_KEY=KEY)
        with FakeStudioForge() as sf:
            sf.route("GET", "/api/leases", 401, {"error": {"code": "unauthorized"}})
            rig = tf.Rig(make_site(endpoint=sf.url))
            with self.assertRaises(tf.RigAuthError) as cm:
                rig.leases()
        self.assertNotIn(KEY, str(cm.exception))
        err = urllib.error.HTTPError("http://x/v1/models", 401, "Unauthorized", None, None)
        msg = tf.http_refusal_message(err)
        self.assertNotIn(KEY, msg)
        self.assertIn("TRANSFORGE_LLM_API_KEY", msg)


class TestLeaseReadFailsClosed(_EnvCase):
    def _rig(self, sf):
        return tf.Rig(make_site(endpoint=sf.url, model="vendor/m"))

    def test_401_and_403_raise_instead_of_reading_as_no_lease(self):
        self.use_env()
        for code in (401, 403):
            with self.subTest(code=code), FakeStudioForge() as sf:
                sf.route("GET", "/api/leases", code, {"error": {"code": "forbidden"}})
                rig = self._rig(sf)
                with self.assertRaises(tf.RigAuthError) as cm:
                    rig.leases()
                msg = str(cm.exception)
                self.assertIn(str(code), msg)
                self.assertIn("TRANSFORGE_LLM_API_KEY", msg)
                with self.assertRaises(tf.RigAuthError):
                    rig.bench_lease()

    def test_warmup_stops_before_touching_the_model(self):
        self.use_env()
        with FakeStudioForge() as sf:
            sf.route("GET", "/api/leases", 401, {"error": {"code": "unauthorized"}})
            sf.route("GET", "/v1/models", 200, {"data": [{"id": "vendor/m"}]})
            site = make_site(endpoint=sf.url, model="vendor/m")
            with self.assertRaises(tf.RigAuthError):
                tf.warmup(site, tf.Rig(site), quiet=True)
        paths = [r["path"] for r in sf.requests]
        self.assertEqual(paths, ["/api/leases"])   # nothing else was attempted

    def test_cmd_text_exits_2_with_a_clear_message_and_sends_no_chat(self):
        self.use_env()
        with FakeStudioForge() as sf:
            sf.route("GET", "/api/leases", 403, {"error": {"code": "forbidden"}})
            site = make_site(endpoint=sf.url, model="vendor/m", languages=["ja"])
            args = argparse.Namespace(site="t", text="hello", to="ja", from_lang=None,
                                      model=None, json=False, no_warmup=False)
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                rc = tf.cmd_text(args, {"t": site})
        self.assertEqual(rc, 2)
        self.assertIn("refused the lease list", err.getvalue())
        self.assertFalse([r for r in sf.requests if "chat" in r["path"]])

    def test_main_maps_it_to_exit_code_2(self):
        self.use_env()
        with FakeStudioForge() as sf:
            sf.route("GET", "/api/leases", 401, {"error": {"code": "unauthorized"}})
            site = make_site(endpoint=sf.url, model="vendor/m")
            err = io.StringIO()
            with mock.patch.object(tf, "load_config", lambda: ({}, {"t": site})), \
                    mock.patch.object(sys, "argv", ["transforge", "warmup", "--site", "t"]), \
                    contextlib.redirect_stderr(err), \
                    self.assertRaises(SystemExit) as cm:
                tf.main()
        self.assertEqual(cm.exception.code, 2)
        self.assertIn("refused the lease list", err.getvalue())

    def test_a_good_key_reads_the_list_and_finds_the_benchmark(self):
        self.use_env(TRANSFORGE_LLM_API_KEY=KEY)
        with FakeStudioForge() as sf:
            sf.route("GET", "/api/leases", 200, {"leases": [
                {"holder": "crucibleforge", "kind": "benchmark"}]})
            lease = self._rig(sf).bench_lease()
        self.assertEqual(lease["holder"], "crucibleforge")

    # ---- documented fail-OPEN cases: no gate to consult, the next call reports
    def test_server_without_the_endpoint_or_erroring_is_still_no_lease(self):
        self.use_env()
        for code in (404, 500, 502):
            with self.subTest(code=code), FakeStudioForge() as sf:
                sf.route("GET", "/api/leases", code, {"error": {"code": "x"}})
                self.assertEqual(self._rig(sf).leases(), [])

    def test_unreachable_studioforge_is_still_no_lease(self):
        self.use_env()
        s = socket.socket()
        s.bind(("127.0.0.1", 0))          # ephemeral port nobody listens on
        port = s.getsockname()[1]
        s.close()
        rig = tf.Rig(make_site(endpoint=f"http://127.0.0.1:{port}"))
        self.assertEqual(rig.leases(), [])
        self.assertIsNone(rig.bench_lease())


class TestHttpRefusalHint(unittest.TestCase):
    def test_401_hint_names_the_env_var_other_codes_do_not(self):
        e401 = urllib.error.HTTPError("http://x/v1/models", 401, "Unauthorized", None, None)
        e500 = urllib.error.HTTPError("http://x/v1/models", 500, "Boom", None, None)
        self.assertIn("TRANSFORGE_LLM_API_KEY", tf.http_refusal_message(e401))
        self.assertNotIn("TRANSFORGE_LLM_API_KEY", tf.http_refusal_message(e500))

    def test_401_and_403_map_to_exit_2_other_refusals_to_1(self):
        for code, rc in ((401, 2), (403, 2), (404, 1), (500, 1)):
            e = urllib.error.HTTPError("http://x/v1/models", code, "x", None, None)
            self.assertEqual(tf.http_refusal_exit_code(e), rc, code)


class TestRefusedElsewhereThanLeases(_EnvCase):
    """README: exit 2 = 'StudioForge refused credentials'. That must hold when
    the 401 arrives on /v1/models or /api/status, not only on /api/leases."""

    def _text_args(self):
        return argparse.Namespace(site="t", text="hello", to="ja", from_lang=None,
                                  model=None, json=False, no_warmup=False)

    def test_cmd_text_401_on_models_exits_2(self):
        self.use_env()
        with FakeStudioForge() as sf:
            sf.route("GET", "/api/leases", 200, {"leases": []})
            sf.route("GET", "/v1/models", 401, {"error": {"code": "unauthorized"}})
            site = make_site(endpoint=sf.url, model="vendor/m", languages=["ja"])
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                rc = tf.cmd_text(self._text_args(), {"t": site})
        self.assertEqual(rc, 2)
        self.assertIn("TRANSFORGE_LLM_API_KEY", err.getvalue())
        self.assertFalse([r for r in sf.requests if "chat" in r["path"]])

    def test_cmd_text_500_on_models_still_exits_1(self):
        self.use_env()
        with FakeStudioForge() as sf:
            sf.route("GET", "/api/leases", 200, {"leases": []})
            sf.route("GET", "/v1/models", 500, {"error": {"code": "boom"}})
            site = make_site(endpoint=sf.url, model="vendor/m", languages=["ja"])
            with contextlib.redirect_stderr(io.StringIO()):
                rc = tf.cmd_text(self._text_args(), {"t": site})
        self.assertEqual(rc, 1)


class TestMainHandlesRigErrorsForEveryCommand(_EnvCase):
    """`models` and `warmup` have no handler of their own: main() must still
    turn an unreachable or refusing StudioForge into the documented exit code
    and one line on stderr, never a traceback."""

    def _main(self, site, *argv):
        err = io.StringIO()
        with mock.patch.object(tf, "load_config", lambda: ({}, {"t": site})), \
                mock.patch.object(sys, "argv", ["transforge", *argv]), \
                contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(err), \
                self.assertRaises(SystemExit) as cm:
            tf.main()
        return cm.exception.code, err.getvalue()

    def _dead_port(self):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        return port

    def test_models_and_warmup_unreachable_exit_4(self):
        self.use_env()
        site = make_site(endpoint=f"http://127.0.0.1:{self._dead_port()}", model="vendor/m")
        for argv in (("models",), ("warmup", "--site", "t")):
            with self.subTest(argv=argv):
                code, err = self._main(site, *argv)
                self.assertEqual(code, 4)
                self.assertIn("rig unreachable", err)
                self.assertNotIn("Traceback", err)

    def test_models_401_exits_2_with_the_hint(self):
        self.use_env()
        with FakeStudioForge() as sf:
            sf.route("GET", "/api/status", 401, {"error": {"code": "unauthorized"}})
            sf.route("GET", "/v1/models", 401, {"error": {"code": "unauthorized"}})
            code, err = self._main(make_site(endpoint=sf.url), "models")
        self.assertEqual(code, 2)
        self.assertIn("HTTP 401", err)
        self.assertIn("TRANSFORGE_LLM_API_KEY", err)
        self.assertNotIn("Traceback", err)


class TestRedirectsAreNotFollowed(_EnvCase):
    """urllib's default redirect handler re-sends every header (Authorization,
    X-MCP-Pin) to whatever host Location names. The rig client must not follow
    a redirect at all, and a redirected lease list must fail closed."""

    def _front_and_back(self, front, back, path):
        front.route("GET", path, 302, {"moved": True},
                    headers={"Location": back.url + path})
        back.route("GET", path, 200, {"leases": [], "data": []})

    def test_bearer_key_and_pin_never_reach_the_redirect_target(self):
        self.use_env(TRANSFORGE_LLM_API_KEY=KEY, STUDIOFORGE_MCP_PIN="1234abcd")
        with FakeStudioForge() as front, FakeStudioForge() as back:
            self._front_and_back(front, back, "/v1/models")
            rig = tf.Rig(make_site(endpoint=front.url))
            with self.assertRaises(urllib.error.HTTPError) as cm:
                rig.models()
            cm.exception.close()
        self.assertEqual(cm.exception.code, 302)
        self.assertIn("not followed", str(cm.exception.reason))
        self.assertEqual(len(front.requests), 1)
        self.assertEqual(back.requests, [])        # nothing, so no key either

    def test_redirected_lease_list_fails_closed(self):
        self.use_env(TRANSFORGE_LLM_API_KEY=KEY)
        with FakeStudioForge() as front, FakeStudioForge() as back:
            self._front_and_back(front, back, "/api/leases")
            rig = tf.Rig(make_site(endpoint=front.url))
            with self.assertRaises(tf.RigAuthError) as cm:
                rig.bench_lease()
        self.assertIn("redirect", str(cm.exception))
        self.assertNotIn(KEY, str(cm.exception))
        self.assertEqual(back.requests, [])

    def test_cmd_text_on_a_redirecting_endpoint_exits_2_and_sends_no_chat(self):
        self.use_env(TRANSFORGE_LLM_API_KEY=KEY)
        with FakeStudioForge() as front, FakeStudioForge() as back:
            self._front_and_back(front, back, "/api/leases")
            site = make_site(endpoint=front.url, model="vendor/m", languages=["ja"])
            args = argparse.Namespace(site="t", text="hello", to="ja", from_lang=None,
                                      model=None, json=False, no_warmup=False)
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                rc = tf.cmd_text(args, {"t": site})
        self.assertEqual(rc, 2)
        self.assertEqual(back.requests, [])
        self.assertFalse([r for r in front.requests if "chat" in r["path"]])

    def test_review_client_does_not_follow_redirects_either(self):
        if REPO not in sys.path:
            sys.path.insert(0, REPO)
        import review as _review
        with FakeStudioForge() as front, FakeStudioForge() as back:
            front.route("POST", "/v1/chat/completions", 307, {"moved": True},
                        headers={"Location": back.url + "/v1/chat/completions"})
            rc = mock.Mock(base_url=front.url + "/v1", model="m", temperature=0.0,
                           max_tokens=10, reasoning_effort="", timeout_s=5)
            rc.api_key.return_value = KEY
            with self.assertRaises(RuntimeError) as cm:
                _review.ReviewClient(rc)._post("s", "u", 10, "")
        self.assertIn("HTTP 307", str(cm.exception))
        self.assertEqual(back.requests, [])


class TestPinDeprecationNotice(_EnvCase):
    def _load(self, sf):
        tf.Rig(make_site(endpoint=sf.url)).load_recommended("vendor/m", 8192, 3)

    def setUp(self):
        p = mock.patch.object(tf, "_PIN_WARNED", [])
        p.start()
        self.addCleanup(p.stop)

    def test_pin_as_the_only_credential_warns_once(self):
        self.use_env(STUDIOFORGE_MCP_PIN="1234abcd")
        err = io.StringIO()
        with FakeStudioForge() as sf, contextlib.redirect_stderr(err):
            sf.route("POST", "/api/models/", 200, {"ok": True}, exact=False)
            self._load(sf)
            self._load(sf)
        self.assertEqual(err.getvalue().count("STUDIOFORGE_MCP_PIN"), 1)
        self.assertIn("deprecated", err.getvalue())
        self.assertIn("TRANSFORGE_LLM_API_KEY", err.getvalue())
        self.assertNotIn("1234abcd", err.getvalue())

    def test_no_notice_when_an_api_key_is_set(self):
        self.use_env(TRANSFORGE_LLM_API_KEY=KEY, STUDIOFORGE_MCP_PIN="1234abcd")
        err = io.StringIO()
        with FakeStudioForge() as sf, contextlib.redirect_stderr(err):
            sf.route("POST", "/api/models/", 200, {"ok": True}, exact=False)
            self._load(sf)
        self.assertEqual(err.getvalue(), "")


if __name__ == "__main__":
    unittest.main()
