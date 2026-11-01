"""`backend = "openai"`: a generic cloud OpenAI-compatible API (e.g. MiniMax).

No lease/bench gate, no model load, no rig-only request fields, the rig's
fallback model is ignored, and HTTP 402 (plan window used up) is a stop.

Run from the repo root:  python -m unittest discover -s tests
"""
import importlib.util
import os
import sys
import unittest
from unittest import mock

import _netguard

_netguard.install(strict_exit=True)

from _fakesf import FakeStudioForge  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_spec = importlib.util.spec_from_file_location(
    "transforge_openai_mod", os.path.join(REPO, "transforge.py"))
tf = importlib.util.module_from_spec(_spec)
sys.modules["transforge_openai_mod"] = tf
_spec.loader.exec_module(tf)

KEY = "sk-cloud-test-0123456789"
MODEL = "Cloud-Model-1"
CHAT = {"model": MODEL, "choices": [{"message": {
    "content": "<think>hmm</think>bonjour"}, "finish_reason": "stop"}]}


def make_site(**kw):
    base = {"backend": "openai", "model": MODEL, "api_key_env": "CLOUD_KEY_VAR"}
    base.update(kw)
    return tf.SiteConfig("t", {}, base)


class TestOpenAIBackend(unittest.TestCase):
    def setUp(self):
        env = {k: v for k, v in os.environ.items()
               if k not in ("TRANSFORGE_LLM_API_KEY", "STUDIOFORGE_API_KEY")}
        env.update(CLOUD_KEY_VAR=KEY, NO_PROXY="127.0.0.1,localhost",
                   no_proxy="127.0.0.1,localhost")
        p = mock.patch.dict(os.environ, env, clear=True)
        p.start()
        self.addCleanup(p.stop)

    def server(self):
        sf = FakeStudioForge()
        sf.route("GET", "/v1/models", 200, {"data": [{"id": MODEL}]})
        sf.route("POST", "/v1/chat/completions", 200, CHAT)
        return sf

    def test_chat_has_no_rig_fields_and_no_lease_calls(self):
        with self.server() as sf:
            site = make_site(endpoint=sf.url, priority=3, disable_thinking=True)
            rig = tf.Rig(site)
            self.assertIsNone(rig.bench_lease())
            self.assertEqual(rig.status(), {})
            data = rig.chat([{"role": "user", "content": "hi"}], 50, 0.5)
        self.assertEqual(data["choices"][0]["message"]["content"], "bonjour")
        paths = [r["path"] for r in sf.requests]
        self.assertEqual(paths, ["/v1/chat/completions"])
        body = sf.requests[0]["body"]
        for k in ("priority", "chat_template_kwargs", "top_k"):
            self.assertNotIn(k, body)
        self.assertEqual(sf.requests[0]["headers"]["Authorization"], "Bearer " + KEY)
        self.assertIsNone(sf.requests[0]["headers"].get("X-SF-Client"))

    def test_explicit_extra_params_are_sent(self):
        with self.server() as sf:
            rig = tf.Rig(make_site(endpoint=sf.url, extra_params={"top_p": 0.9}))
            rig.chat([{"role": "user", "content": "hi"}], 50, 0.5)
        self.assertEqual(sf.requests[0]["body"]["top_p"], 0.9)

    def test_rig_key_env_never_goes_to_the_cloud(self):
        with mock.patch.dict(os.environ, {"TRANSFORGE_LLM_API_KEY": "rig-secret"}):
            self.assertEqual(tf.Rig(make_site(api_key_env="CLOUD_KEY_VAR")).api_key(), KEY)
            self.assertEqual(tf.Rig(make_site(api_key_env="UNSET_VAR")).api_key(), "")

    def test_warmup_only_checks_models(self):
        with self.server() as sf:
            site = make_site(endpoint=sf.url, concurrency=3)
            self.assertEqual(tf.warmup(site, tf.Rig(site), quiet=True), 3)
            self.assertEqual([r["path"] for r in sf.requests], ["/v1/models"])
            bad = make_site(endpoint=sf.url, model="Other")
            with self.assertRaises(SystemExit) as cm:
                tf.warmup(bad, tf.Rig(bad), quiet=True)
            self.assertEqual(cm.exception.code, 5)

    def test_concurrency_defaults_to_4_and_rig_fallback_ignored(self):
        site = tf.SiteConfig("t", {"fallback_model": "rig/model"},
                             {"backend": "openai"})
        self.assertEqual(site.concurrency, 4)
        self.assertEqual(site.fallback_model, "")
        site = make_site(openai_fallback_model="Cloud-Other")
        self.assertEqual(site.fallback_model, "Cloud-Other")
        rig_site = tf.SiteConfig("t", {"fallback_model": "rig/model"}, {})
        self.assertEqual(rig_site.fallback_model, "rig/model")
        self.assertEqual(rig_site.backend, "studioforge")

    def test_402_is_quota_exhausted_without_retry(self):
        with self.server() as sf:
            sf.route("POST", "/v1/chat/completions", 402,
                     {"error": {"message": "usage limit"}})
            rig = tf.Rig(make_site(endpoint=sf.url))
            with mock.patch.object(tf.time, "sleep") as slept:
                with self.assertRaises(tf.QuotaExhausted):
                    rig.chat([{"role": "user", "content": "hi"}], 50, 0.5)
            slept.assert_not_called()
        self.assertEqual(len(sf.requests), 1)
        self.assertEqual(tf.QUOTA_EXHAUSTED, 8)
        self.assertFalse(issubclass(tf.QuotaExhausted, RuntimeError))

    def test_invalid_backend_is_a_config_error(self):
        with self.assertRaises(SystemExit) as cm:
            tf.SiteConfig("t", {}, {"backend": "bogus"})
        self.assertEqual(cm.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
