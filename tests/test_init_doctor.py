"""`init` writes a readable starter config, `doctor` is read-only, the log is metadata only.

Run from the repo root:  python -m unittest discover -s tests
"""
import contextlib
import importlib.util
import io
import json
import os
import sys
import tempfile
import tomllib
import types
import unittest

import _netguard

_netguard.install(strict_exit=True)  # hermetic: no real sockets

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_spec = importlib.util.spec_from_file_location(
    "transforge_init_doctor_mod", os.path.join(REPO, "transforge.py"))
tf = importlib.util.module_from_spec(_spec)
sys.modules["transforge_init_doctor_mod"] = tf
_spec.loader.exec_module(tf)


def init_args(**kw):
    base = dict(name="example", root="~/sites/example", languages="ja,de",
                endpoint="http://localhost:1234", model="a/b/c", force=False)
    base.update(kw)
    return types.SimpleNamespace(**base)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.old = (tf.CONFIG_PATH, tf.STATE_DIR)
        tf.CONFIG_PATH = os.path.join(self.tmp.name, "cfg", "config.toml")
        tf.STATE_DIR = os.path.join(self.tmp.name, "state")
        self.addCleanup(lambda: setattr(tf, "CONFIG_PATH", self.old[0]))
        self.addCleanup(lambda: setattr(tf, "STATE_DIR", self.old[1]))

    def run_cmd(self, fn, args):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            try:
                rc = fn(args)
            except SystemExit as e:
                rc = e.code
        return rc, out.getvalue()


class InitTests(Base):
    def test_writes_a_loadable_config_and_refuses_to_overwrite(self):
        rc, _ = self.run_cmd(tf.cmd_init, init_args(root='C:\\sites\\"x"'))
        self.assertEqual(rc, 0)
        with open(tf.CONFIG_PATH, "rb") as f:
            cfg = tomllib.load(f)
        self.assertEqual(cfg["sites"]["example"]["languages"], ["ja", "de"])
        self.assertEqual(cfg["sites"]["example"]["root"], 'C:\\sites\\"x"')
        self.assertEqual(cfg["sites"]["example"]["lang_names"]["ja"], "Japanese")
        before = open(tf.CONFIG_PATH).read()
        rc, out = self.run_cmd(tf.cmd_init, init_args(model="other"))
        self.assertEqual(rc, 1)
        self.assertIn("already exists", out)
        self.assertEqual(open(tf.CONFIG_PATH).read(), before)
        rc, _ = self.run_cmd(tf.cmd_init, init_args(model="other", force=True))
        self.assertEqual(rc, 0)
        self.assertIn("other", open(tf.CONFIG_PATH).read())

    def test_rejects_bad_names_and_languages(self):
        self.assertEqual(self.run_cmd(tf.cmd_init, init_args(name="a b"))[0], tf.USAGE_ERROR)
        self.assertEqual(self.run_cmd(tf.cmd_init, init_args(languages='ja,"x'))[0], tf.USAGE_ERROR)
        self.assertFalse(os.path.exists(tf.CONFIG_PATH))


class DoctorTests(Base):
    def make_site(self):
        root = os.path.join(self.tmp.name, "site")
        os.makedirs(os.path.join(root, "content", "pages"))
        self.run_cmd(tf.cmd_init, init_args(
            root=root.replace("\\", "/"), model="a/b/c"))
        return root

    def doctor(self, models=None, exc=None):
        orig = tf.Rig.models

        def fake(self_):
            if exc:
                raise exc
            return [{"id": m} for m in models]
        tf.Rig.models = fake
        self.addCleanup(lambda: setattr(tf.Rig, "models", orig))
        return self.run_cmd(tf.cmd_doctor, types.SimpleNamespace(site=None))

    def test_missing_config_fails_without_traceback(self):
        rc, out = self.run_cmd(tf.cmd_doctor, types.SimpleNamespace(site=None))
        self.assertEqual(rc, 1)
        self.assertIn("transforge init", out)

    def test_content_dir_missing_and_model_missing_are_reported(self):
        self.make_site()                       # only content/pages exists
        rc, out = self.doctor(models=["x/y/z"])
        self.assertEqual(rc, 1)
        self.assertIn("FAIL [example] content dir: content/projects", out)
        self.assertIn("ok   [example] content dir: content/pages", out)
        self.assertIn("FAIL [example] model", out)

    def test_all_good(self):
        root = self.make_site()
        os.makedirs(os.path.join(root, "content", "projects"))
        rc, out = self.doctor(models=["a/b/c"])
        self.assertEqual(rc, 0, out)
        self.assertIn("all checks passed", out)

    def test_unreachable_rig_is_a_failed_check(self):
        self.make_site()
        rc, out = self.doctor(exc=ConnectionError("refused"))
        self.assertEqual(rc, 1)
        self.assertIn("unreachable", out)


class LogTests(Base):
    def test_metadata_only_and_rotates(self):
        tf.log_event("command", cmd="run", exit=0)
        path = os.path.join(tf.STATE_DIR, "transforge.log")
        row = json.loads(open(path).read().splitlines()[0])
        self.assertEqual((row["event"], row["cmd"], row["exit"]), ("command", "run", 0))
        with open(path, "a") as f:
            f.write("x" * (tf.LOG_MAX_BYTES + 1))
        tf.log_event("command", cmd="run", exit=6)
        self.assertTrue(os.path.exists(path + ".1"))
        self.assertEqual(len(open(path).read().splitlines()), 1)

    def test_unwritable_state_dir_never_raises(self):
        tf.STATE_DIR = os.path.join(self.tmp.name, "file")
        open(tf.STATE_DIR, "w").close()        # a file where the dir should be
        tf.log_event("command", cmd="run", exit=0)


if __name__ == "__main__":
    unittest.main()
