"""Manifest keys are the same on every OS, and older manifests are re-keyed.

Before 0.2.0 a Windows run stored mixed keys ('content/projects\\foo.md|ja');
0.2.0's normpath'd discovery produced 'content\\projects\\foo.md' on Windows and
'content/projects/foo.md' on Linux. A key that does not match the discovered
path reads as UNTRACKED and the next status/plan/run prunes it, losing the
src_sha that makes a STALE sibling STALE.

Run from the repo root:  python -m unittest discover -s tests
"""
import importlib.util
import json
import os
import sys
import tempfile
import unittest

import _netguard

_netguard.install(strict_exit=True)  # hermetic: no real sockets

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_spec = importlib.util.spec_from_file_location(
    "transforge_manifest_keys_mod", os.path.join(REPO, "transforge.py"))
tf = importlib.util.module_from_spec(_spec)
sys.modules["transforge_manifest_keys_mod"] = tf
_spec.loader.exec_module(tf)


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


class _SiteCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        orig = tf.STATE_DIR
        tf.STATE_DIR = os.path.join(self.tmp.name, "state")
        self.addCleanup(lambda: setattr(tf, "STATE_DIR", orig))
        self.root = os.path.join(self.tmp.name, "site")
        self.src = "# Foo\n\nHello world.\n"
        self.out = "# Foo\n\nこんにちは。\n"
        write(os.path.join(self.root, "content", "projects", "foo.md"), self.src)
        write(os.path.join(self.root, "content", "projects", "foo.ja.md"), self.out)

    def site(self, content_dirs=("content/projects",)):
        return tf.SiteConfig("s", {}, {"root": self.root, "languages": ["ja"],
                                       "content_dirs": list(content_dirs)})

    def write_manifest(self, entries):
        os.makedirs(tf.STATE_DIR, exist_ok=True)
        with open(os.path.join(tf.STATE_DIR, "s-manifest.json"), "w",
                  encoding="utf-8") as f:
            json.dump({"version": 1, "entries": entries}, f)

    def entry(self, src_text=None, stamp="2026-01-01T00:00:00Z", site=None):
        site = site or self.site()
        return {"src_sha": tf.sha256_text(src_text or self.src),
                "out_sha": tf.sha256_text(self.out), "cfg": site.cfg_hash(),
                "translated_at": stamp}


class TestManifestKey(unittest.TestCase):
    def test_one_spelling_for_every_os_and_history(self):
        for rel in ("content/projects/foo.md", "content\\projects\\foo.md",
                    "content/projects\\foo.md", "./content/projects/foo.md",
                    "content//projects/foo.md",
                    os.path.join("content", "projects", "foo.md")):
            with self.subTest(rel=rel):
                self.assertEqual(tf.manifest_key(rel, "ja"),
                                 "content/projects/foo.md|ja")


class TestOlderManifestsStillMatch(_SiteCase):
    LEGACY = ("content/projects\\foo.md",     # Windows, before 0.2.0 (mixed)
              "content\\projects\\foo.md",    # Windows, 0.2.0 normpath
              "content/projects/foo.md")      # Linux / the container

    def test_every_legacy_spelling_classifies_current_not_untracked(self):
        for rel in self.LEGACY:
            with self.subTest(rel=rel):
                self.write_manifest({f"{rel}|ja": self.entry()})
                site = self.site()
                m = tf.Manifest("s")
                rows = tf.scan_site(site, m)
                self.assertEqual([s for s, _ in rows.values()], ["CURRENT"])
                self.assertEqual(tf.prune_manifest(m, rows), 0)

    def test_stale_survives_the_rekey(self):
        """The regression: an UNTRACKED page cannot be STALE, and the README's
        old remedy (`accept --all`) then accepted the outdated sibling."""
        for rel in self.LEGACY:
            with self.subTest(rel=rel):
                self.write_manifest({f"{rel}|ja": self.entry(src_text="# Old\n")})
                rows = tf.scan_site(self.site(), tf.Manifest("s"))
                self.assertEqual([s for s, _ in rows.values()], ["STALE"])

    def test_dot_slash_content_dir_on_linux_too(self):
        self.write_manifest({"./content/projects/foo.md|ja": self.entry()})
        site = self.site(content_dirs=("./content/projects",))
        rows = tf.scan_site(site, tf.Manifest("s"))
        self.assertEqual([s for s, _ in rows.values()], ["CURRENT"])

    def test_save_persists_the_canonical_key(self):
        self.write_manifest({"content\\projects\\foo.md|ja": self.entry()})
        m = tf.Manifest("s")
        self.assertEqual(m.migrated, 1)
        m.save()
        with open(m.path, encoding="utf-8") as f:
            keys = list(json.load(f)["entries"])
        self.assertEqual(keys, ["content/projects/foo.md|ja"])

    def test_two_spellings_of_one_page_keep_the_newer_entry(self):
        self.write_manifest({
            "content/projects\\foo.md|ja": self.entry(src_text="# Old\n",
                                                      stamp="2026-01-01T00:00:00Z"),
            "content\\projects\\foo.md|ja": self.entry(stamp="2026-09-01T00:00:00Z")})
        m = tf.Manifest("s")
        self.assertEqual(list(m.entries), ["content/projects/foo.md|ja"])
        self.assertEqual(m.get(os.path.join("content", "projects", "foo.md"), "ja")
                         ["translated_at"], "2026-09-01T00:00:00Z")

    def test_set_and_prune_use_the_same_key_as_get(self):
        m = tf.Manifest("s")
        native = os.path.join("content", "projects", "foo.md")
        m.set(native, "ja", {"src_sha": "x"})
        self.assertIsNotNone(m.get("content/projects/foo.md", "ja"))
        self.assertEqual(m.prune({f"{native}|ja"}), 0)
        self.assertEqual(m.prune({"content/projects/other.md|ja"}), 1)


if __name__ == "__main__":
    unittest.main()
