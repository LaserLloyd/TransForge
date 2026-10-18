"""StudioForge contract tests: pin every StudioForge field TransForge reads.

Fixtures live in tests/contract_studioforge/fixtures (derived from this client's
code, see the README beside them).  A StudioForge contract change that breaks
one of these tests is a *visible* break, not a silent behaviour change.

Run from the repo root:  python -m unittest discover -s tests
"""
import argparse
import contextlib
import copy
import importlib.util
import io
import json
import os
import sys
import unittest
import urllib.error
from unittest import mock

import _netguard

_netguard.install(strict_exit=True)

from _fakesf import FakeStudioForge  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIX = os.path.join(REPO, "tests", "contract_studioforge", "fixtures")
_spec = importlib.util.spec_from_file_location(
    "transforge_contract_mod", os.path.join(REPO, "transforge.py"))
tf = importlib.util.module_from_spec(_spec)
sys.modules["transforge_contract_mod"] = tf
_spec.loader.exec_module(tf)


def fixture(name):
    with open(os.path.join(FIX, name + ".json"), encoding="utf-8") as f:
        return json.load(f)


def make_site(**site):
    return tf.SiteConfig("t", {}, site)


def http_error_from(fx):
    """The urllib HTTPError a client would see for this error fixture."""
    import email.message
    hdrs = email.message.Message()
    for k, v in fx.get("headers", {}).items():
        hdrs[k] = v
    return urllib.error.HTTPError(
        "http://sf.invalid/v1/chat/completions", fx["http_status"], "err", hdrs,
        io.BytesIO(json.dumps(fx["body"]).encode("utf-8")))


class _EnvCase(unittest.TestCase):
    def setUp(self):
        env = {k: v for k, v in os.environ.items()
               if k not in ("TRANSFORGE_LLM_API_KEY", "STUDIOFORGE_API_KEY",
                            "STUDIOFORGE_MCP_PIN")}
        env["NO_PROXY"] = env["no_proxy"] = "127.0.0.1,localhost"
        p = mock.patch.dict(os.environ, env, clear=True)
        p.start()
        self.addCleanup(p.stop)
        sleeps = mock.patch.object(tf.time, "sleep", lambda s: None)
        sleeps.start()
        self.addCleanup(sleeps.stop)


class TestErrorEnvelope(_EnvCase):
    """error.code + error.studioforge.{retry_after_s, lease} + Retry-After."""

    def test_gpu_leased_fields_are_parsed(self):
        fx = fixture("error_gpu_leased")
        code, retry, lease, _ = tf.sf_error(http_error_from(fx))
        sf = fx["body"]["error"]["studioforge"]
        self.assertEqual(code, "gpu_leased")
        self.assertEqual(retry, sf["retry_after_s"])
        self.assertEqual(lease, sf["lease"])
        self.assertEqual(lease["kind"], "benchmark")
        self.assertEqual(lease["holder_family"], "crucibleforge")

    def test_retry_after_header_is_the_fallback_when_the_body_has_none(self):
        fx = copy.deepcopy(fixture("error_gpu_leased"))
        del fx["body"]["error"]["studioforge"]["retry_after_s"]
        _, retry, _, _ = tf.sf_error(http_error_from(fx))
        self.assertEqual(retry, 30.0)       # from the Retry-After header

    def test_retry_after_s_must_live_under_error_studioforge(self):
        """Sensitivity check: if StudioForge moved retry_after_s elsewhere the
        client would stop seeing it -- this is what the contract test catches."""
        fx = copy.deepcopy(fixture("error_priority_hold"))
        fx["headers"] = {}
        moved = fx["body"]["error"].pop("studioforge")
        fx["body"]["error"]["details"] = moved           # a "refactored" envelope
        _, retry, _, _ = tf.sf_error(http_error_from(fx))
        self.assertIsNone(retry)

    def test_gpu_leased_in_chat_hands_the_lease_to_the_caller(self):
        fx = fixture("error_gpu_leased")
        with FakeStudioForge() as sf:
            sf.route("POST", "/v1/chat/completions", fx["http_status"], fx["body"],
                     fx["headers"])
            rig = tf.Rig(make_site(endpoint=sf.url, model="vendor/m"))
            with self.assertRaises(tf.RigLeased) as cm:
                rig.chat([{"role": "user", "content": "x"}], 10, 0.3)
        self.assertEqual(cm.exception.lease["holder"], "crucibleforge-judge")
        self.assertEqual(cm.exception.retry_after_s, 30)
        self.assertTrue(tf._is_bench_lease(cm.exception.lease))
        self.assertEqual(len(sf.requests), 1)            # not retried

    def test_priority_hold_is_retried_then_succeeds(self):
        hold = fixture("error_priority_hold")
        ok = fixture("chat_completion")["response"]
        with FakeStudioForge() as sf:
            sf.route("POST", "/v1/chat/completions", 200, ok)
            rig = tf.Rig(make_site(endpoint=sf.url, model=ok["model"]))
            # first answer: priority_hold; second: success
            answers = [(hold["http_status"], hold["body"], hold["headers"]),
                       (200, ok, {})]
            real = sf._srv.lookup
            sf._srv.lookup = lambda m, p: answers.pop(0) if answers else real(m, p)
            data = rig.chat([{"role": "user", "content": "x"}], 10, 0.3)
        self.assertEqual(len(sf.requests), 2)
        self.assertEqual(data["choices"][0]["message"]["content"], "hola")

    def test_insufficient_vram_and_context_exceeded_are_final(self):
        for name in ("error_insufficient_vram", "error_context_exceeded"):
            fx = fixture(name)
            with self.subTest(name), FakeStudioForge() as sf:
                sf.route("POST", "/v1/chat/completions", fx["http_status"],
                         fx["body"], fx["headers"])
                rig = tf.Rig(make_site(endpoint=sf.url, model="vendor/m"))
                with self.assertRaises(RuntimeError) as cm:
                    rig.chat([{"role": "user", "content": "x"}], 10, 0.3)
                self.assertIn(fx["body"]["error"]["code"], str(cm.exception))
                self.assertEqual(len(sf.requests), 1)    # never retried

    def test_gpu_leased_on_load_by_a_benchmark_exits_6(self):
        fx = fixture("error_gpu_leased")
        lr = fixture("load_recommended")
        with FakeStudioForge() as sf:
            sf.route("GET", "/api/leases", 200, {"leases": []})
            sf.route("GET", "/v1/models", 200,
                     {"data": [{"id": lr["model_id"]}]})
            sf.route("GET", "/api/status", 200, {"loaded": []})
            sf.route("POST", "/api/models/", fx["http_status"], fx["body"],
                     fx["headers"], exact=False)
            site = make_site(endpoint=sf.url, model=lr["model_id"])
            with mock.patch.dict(os.environ, {"TRANSFORGE_LLM_API_KEY": "k"}), \
                    contextlib.redirect_stderr(io.StringIO()), \
                    self.assertRaises(SystemExit) as cm:
                tf.warmup(site, tf.Rig(site), quiet=True)
        self.assertEqual(cm.exception.code, 6)


class TestLeaseRecords(_EnvCase):
    def test_kind_and_holder_family_classify_benchmarks(self):
        fx = fixture("leases")
        got = [l["id"] for l in fx["response"]["leases"] if tf._is_bench_lease(l)]
        self.assertEqual(got, fx["expect_benchmark_ids"])

    def test_kind_render_is_believed_even_for_a_crucibleforge_looking_holder(self):
        self.assertFalse(tf._is_bench_lease(
            {"holder": "crucibleforge", "kind": "render", "holder_family": "other"}))

    def test_legacy_records_fall_back_to_the_holder_prefix(self):
        for rec in fixture("leases")["legacy_records_without_kind_or_family"]:
            self.assertTrue(tf._is_bench_lease(rec), rec)

    def test_lease_list_shape_over_http(self):
        fx = fixture("leases")
        with FakeStudioForge() as sf:
            sf.route("GET", "/api/leases", 200, fx["response"])
            rig = tf.Rig(make_site(endpoint=sf.url))
            leases = rig.leases()
            bench = rig.bench_lease()
        self.assertEqual(len(leases), 3)
        self.assertEqual(bench["id"], "l-bench")


class TestModelsAndStatus(_EnvCase):
    def test_v1_models_additive_fields_are_ignored(self):
        fx = fixture("models")["response"]
        with FakeStudioForge() as sf:
            sf.route("GET", "/v1/models", 200, fx)
            models = tf.Rig(make_site(endpoint=sf.url)).models()
        self.assertEqual([m["id"] for m in models],
                         ["vendor/repo-GGUF/model-Q5_K_S",
                          "vendor/other-GGUF/other-Q4_K_M"])

    def test_status_loaded_row_fields(self):
        st = fixture("status")["response"]
        mid = st["loaded"][0]["model_id"]
        with FakeStudioForge() as sf:
            sf.route("GET", "/api/status", 200, st)
            rig = tf.Rig(make_site(endpoint=sf.url))
            self.assertEqual(rig.loaded_row(mid)["state"], "ready")
            self.assertEqual(rig.live_parallel(mid), 3)
            self.assertEqual(rig.live_parallel("not/loaded"), 0)

    def test_models_command_renders_loaded_state_from_both_endpoints(self):
        with FakeStudioForge() as sf:
            sf.route("GET", "/v1/models", 200, fixture("models")["response"])
            sf.route("GET", "/api/status", 200, fixture("status")["response"])
            site = make_site(endpoint=sf.url)
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                tf.cmd_models(argparse.Namespace(site=None), {"t": site})
        text = out.getvalue()
        self.assertIn("LOADED  vendor/repo-GGUF/model-Q5_K_S  parallel=3 "
                      "ctx/slot=16384 state=ready", text)
        self.assertIn("vendor/other-GGUF/other-Q4_K_M", text)


class TestRequestShapes(_EnvCase):
    def test_load_recommended_path_and_body(self):
        fx = fixture("load_recommended")
        with FakeStudioForge() as sf:
            sf.route("POST", "/api/models/", 200, fx["response"], exact=False)
            os.environ["TRANSFORGE_LLM_API_KEY"] = "k"
            out = tf.Rig(make_site(endpoint=sf.url)).load_recommended(
                fx["model_id"], fx["request_body"]["ctx_size"],
                fx["request_body"]["priority"])
        self.assertEqual(sf.requests[0]["method"], "POST")
        self.assertEqual(sf.requests[0]["path"], fx["expect_path"])
        self.assertEqual(sf.requests[0]["body"], fx["request_body"])
        self.assertEqual(out, fx["response"])

    def test_chat_request_keys_priority_and_client_header(self):
        fx = fixture("chat_completion")
        with FakeStudioForge() as sf:
            sf.route("POST", "/v1/chat/completions", 200, fx["response"])
            rig = tf.Rig(make_site(endpoint=sf.url, model=fx["response"]["model"]))
            data = rig.chat([{"role": "user", "content": "x"}], 50, 0.3)
        req = sf.requests[0]
        self.assertEqual(sorted(req["body"]), sorted(fx["expect_request_keys"]))
        self.assertEqual(req["body"]["priority"], 3)
        self.assertEqual(req["body"]["chat_template_kwargs"], {"enable_thinking": False})
        for k, v in fx["expect_request_headers"].items():
            self.assertEqual(req["headers"].get(k), v)
        # additive response fields tolerated; <think> stripped from content
        self.assertEqual(data["choices"][0]["message"]["content"], "hola")


if __name__ == "__main__":
    unittest.main()
