"""`transforge run` and the benchmark lease gate: exit codes and --no-warmup.

* a lease list that becomes unreadable (401/403) MID-run is a credentials
  problem: exit 2, not 6 ("leased, back off and retry");
* --no-warmup still reads the lease list before the first job (the in-run
  gate only re-reads every 60 s);
* a 401/403 on load-recommended is a refusal (exit 2 with the PIN hint), not a
  generic "warmup failed" (exit 1).

Run from the repo root:  python -m unittest discover -s tests
"""
import argparse
import contextlib
import importlib.util
import io
import itertools
import os
import sys
import tempfile
import unittest
from unittest import mock

import _netguard

_netguard.install(strict_exit=True)  # hermetic: no real sockets

from _fakesf import FakeStudioForge  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_spec = importlib.util.spec_from_file_location(
    "transforge_run_gate_mod", os.path.join(REPO, "transforge.py"))
tf = importlib.util.module_from_spec(_spec)
sys.modules["transforge_run_gate_mod"] = tf
_spec.loader.exec_module(tf)

KEY_ENVS = ("TRANSFORGE_LLM_API_KEY", "STUDIOFORGE_API_KEY", "STUDIOFORGE_MCP_PIN")


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


class ScriptedRig:
    """Stands in for tf.Rig inside cmd_run; bench_lease() follows a script."""
    script = []

    def __init__(self, site):
        self.site = site
        self.lease_reads = 0

    def bench_lease(self):
        step = ScriptedRig.script[min(self.lease_reads, len(ScriptedRig.script) - 1)]
        self.lease_reads += 1
        if isinstance(step, Exception):
            raise step
        return step

    def models(self):
        return [{"id": self.site.model}]

    def live_parallel(self, model_id):
        return 2

    def loaded_row(self, model_id):
        return {"model_id": model_id, "state": "ready", "plan": {"parallel": 2}}

    def wait_ready(self, model_id, timeout_s=900):
        return self.loaded_row(model_id)

    def pin(self):
        return ""

    def api_key(self):
        return "k"


class _RunCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        orig = tf.STATE_DIR
        tf.STATE_DIR = os.path.join(self.tmp.name, "state")
        self.addCleanup(lambda: setattr(tf, "STATE_DIR", orig))
        self.root = os.path.join(self.tmp.name, "site")
        for n in ("a", "b"):
            write(os.path.join(self.root, "content", f"{n}.md"), f"# {n}\n\nHello.\n")
        env = {k: v for k, v in os.environ.items() if k not in KEY_ENVS}
        env["NO_PROXY"] = env["no_proxy"] = "127.0.0.1,localhost"
        p = mock.patch.dict(os.environ, env, clear=True)
        p.start()
        self.addCleanup(p.stop)
        q = mock.patch.object(tf, "PIN_ENV_FILE", "")
        q.start()
        self.addCleanup(q.stop)
        self.translated = []

    def site(self, **kw):
        cfg = {"root": self.root, "languages": ["ja"], "content_dirs": ["content"],
               "model": "vendor/m", "concurrency": 1}
        cfg.update(kw)
        return tf.SiteConfig("t", {}, cfg)

    def run_cmd(self, site, no_warmup=False, rig_cls=None):
        args = argparse.Namespace(site="t", langs=None, files=None, force=False,
                                  limit=None, dry_run=False, no_warmup=no_warmup,
                                  workers=1)

        def fake_translate(site_, rig_, text, lang, log=None, **kw):
            self.translated.append(lang)
            return text, None

        err = io.StringIO()
        patches = [mock.patch.object(tf, "translate_with_recovery", fake_translate)]
        if rig_cls is not None:
            patches.append(mock.patch.object(tf, "Rig", rig_cls))
        with contextlib.ExitStack() as st:
            for p in patches:
                st.enter_context(p)
            st.enter_context(contextlib.redirect_stdout(io.StringIO()))
            st.enter_context(contextlib.redirect_stderr(err))
            rc = tf.cmd_run(args, {"t": site})
        return rc, err.getvalue()


class TestMidRunRefusal(_RunCase):
    def test_lease_list_refused_mid_run_exits_2_not_6(self):
        ScriptedRig.script = [None, tf.RigAuthError("StudioForge refused the lease list (HTTP 403)")]
        clock = itertools.count(1_000_000, 61)     # every lease_gate is due
        with mock.patch.object(tf.time, "time", lambda: next(clock)):
            rc, err = self.run_cmd(self.site(), rig_cls=ScriptedRig)
        self.assertEqual(rc, 2)
        self.assertIn("refused the lease list", err)
        self.assertEqual(self.translated, [])     # no job started after it


class TestNoWarmupStillChecksTheLease(_RunCase):
    def test_benchmark_lease_stops_before_the_first_job(self):
        ScriptedRig.script = [{"holder": "crucibleforge", "kind": "benchmark"}]
        rc, err = self.run_cmd(self.site(), no_warmup=True, rig_cls=ScriptedRig)
        self.assertEqual(rc, 6)
        self.assertIn("crucibleforge", err)
        self.assertEqual(self.translated, [])

    def test_refused_lease_list_exits_2_before_the_first_job(self):
        ScriptedRig.script = [tf.RigAuthError("StudioForge refused the lease list (HTTP 401)")]
        rc, err = self.run_cmd(self.site(), no_warmup=True, rig_cls=ScriptedRig)
        self.assertEqual(rc, 2)
        self.assertEqual(self.translated, [])

    def test_no_lease_runs_the_jobs(self):
        ScriptedRig.script = [None]
        rc, _ = self.run_cmd(self.site(), no_warmup=True, rig_cls=ScriptedRig)
        self.assertEqual(rc, 0)
        self.assertEqual(self.translated, ["ja", "ja"])


class TestLoadRefusal(_RunCase):
    def test_401_on_load_recommended_exits_2_with_the_pin_hint(self):
        os.environ["TRANSFORGE_LLM_API_KEY"] = "sk-test-load"
        with FakeStudioForge() as sf:
            sf.route("GET", "/api/leases", 200, {"leases": []})
            sf.route("GET", "/v1/models", 200, {"data": [{"id": "vendor/m"}]})
            sf.route("GET", "/api/status", 200, {"loaded": []})
            sf.route("POST", "/api/models/", 401, {"error": {"code": "unauthorized"}},
                     exact=False)
            rc, err = self.run_cmd(self.site(endpoint=sf.url))
        self.assertEqual(rc, 2)
        self.assertIn("HTTP 401", err)
        self.assertIn("STUDIOFORGE_MCP_PIN", err)
        self.assertNotIn("sk-test-load", err)
        self.assertEqual(self.translated, [])


if __name__ == "__main__":
    unittest.main()
