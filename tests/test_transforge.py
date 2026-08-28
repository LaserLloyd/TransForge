"""Offline tests for transforge.py — pure logic only, no network.

Run from the repo root:  python3 -m unittest discover -s tests
"""
import importlib.util
import json
import os
import stat
import sys
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_spec = importlib.util.spec_from_file_location(
    "transforge_mod", os.path.join(REPO, "transforge.py"))
tf = importlib.util.module_from_spec(_spec)
sys.modules["transforge_mod"] = tf
_spec.loader.exec_module(tf)


def make_site(name="t", **site):
    """Build a SiteConfig directly, never touching ~/.config."""
    return tf.SiteConfig(name, {}, site)


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


# ------------------------------------------------------------ frontmatter
class TestSplitFrontmatter(unittest.TestCase):
    def test_with_frontmatter(self):
        text = "---\ntitle: Hi\nexcerpt: There\n---\n\n<p>body</p>\n"
        fm, body = tf.split_frontmatter(text)
        self.assertEqual(fm, "title: Hi\nexcerpt: There")
        self.assertEqual(body.strip(), "<p>body</p>")

    def test_without_frontmatter(self):
        text = "<p>no frontmatter here</p>\n"
        fm, body = tf.split_frontmatter(text)
        self.assertIsNone(fm)
        self.assertEqual(body, text)

    def test_unclosed_delimiter(self):
        text = "---\ntitle: Hi\n\n<p>body</p>\n"
        fm, body = tf.split_frontmatter(text)
        self.assertIsNone(fm)
        self.assertEqual(body, text)

    def test_empty_text(self):
        self.assertEqual(tf.split_frontmatter(""), (None, ""))

    def test_empty_frontmatter_block(self):
        fm, body = tf.split_frontmatter("---\n---\nbody\n")
        self.assertEqual(fm, "")
        self.assertEqual(body.strip(), "body")


# ---------------------------------------------------------------- chunking
class TestSplitChunks(unittest.TestCase):
    def setUp(self):
        self.site = make_site(max_chunk_chars=200)
        self.tr = tf.Translator(self.site, rig=None)

    def test_splits_at_h2(self):
        body = ("<p>intro</p>\n\n"
                "<h2>One</h2>\n<p>a</p>\n\n"
                "<h2>Two</h2>\n<p>b</p>\n")
        chunks = self.tr.split_chunks(body)
        self.assertEqual(len(chunks), 3)
        self.assertTrue(chunks[0].startswith("<p>intro"))
        self.assertTrue(chunks[1].startswith("<h2>One"))
        self.assertTrue(chunks[2].startswith("<h2>Two"))

    def test_no_h2_single_chunk(self):
        body = "<p>short</p>\n"
        self.assertEqual(self.tr.split_chunks(body), ["<p>short</p>"])

    def test_oversized_h2_falls_back_to_h3(self):
        para = "<p>" + "x" * 150 + "</p>"
        body = "<h2>Big</h2>\n" + para + "\n\n<h3>Sub</h3>\n" + para + "\n"
        chunks = self.tr.split_chunks(body)
        self.assertGreater(len(chunks), 1)
        self.assertTrue(any(c.startswith("<h3>Sub") for c in chunks))

    def test_oversized_h3_falls_back_to_paragraph_packing(self):
        para = "<p>" + "y" * 150 + "</p>"
        body = "<h2>Big</h2>\n" + "\n\n".join([para] * 5) + "\n"
        chunks = self.tr.split_chunks(body)
        self.assertGreaterEqual(len(chunks), 5)
        for c in chunks:
            # each packed chunk holds at most one oversized paragraph
            self.assertLessEqual(len(c), self.site.max_chunk_chars + len(para))

    def test_content_preserved(self):
        para = "<p>" + "z" * 120 + "</p>"
        body = ("<p>intro</p>\n\n"
                "<h2>One</h2>\n" + "\n\n".join([para] * 4) + "\n\n"
                "<h3>Sub</h3>\n<p>tail</p>\n\n"
                "<h2>Two</h2>\n<p>end</p>\n")
        chunks = self.tr.split_chunks(body)
        src_lines = [l for l in body.split("\n") if l.strip()]
        out_lines = [l for c in chunks for l in c.split("\n") if l.strip()]
        self.assertEqual(src_lines, out_lines)

    def test_blank_only_chunks_dropped(self):
        chunks = self.tr.split_chunks("\n\n\n")
        self.assertEqual(chunks, [])

    def test_indented_h2_still_splits(self):
        body = "<p>a</p>\n   <h2>Two</h2>\n<p>b</p>\n"
        chunks = self.tr.split_chunks(body)
        self.assertEqual(len(chunks), 2)


# ------------------------------------------------------------ verification
IDENT_SRC = """<p>Intro paragraph with a <a href="https://example.com/">link</a>.</p>

<h2>Heading</h2>
<p>Body text that is long enough to clear the size floor comfortably here.</p>
<pre><code>echo hello</code></pre>
<figure><img src="/img/a.png" alt="a"></figure>
<svg viewBox="0 0 10 10"><path d="M0 0"/><text x="1" y="2">Label</text></svg>
<table><tr><td>cell</td></tr></table>
"""

IDENT_JA = """<p>導入の段落。<a href="https://example.com/">リンク</a>が入ります。</p>

<h2>見出し</h2>
<p>サイズ下限を十分に上回る長さの本文テキストがここに入ります。</p>
<pre><code>echo hello</code></pre>
<figure><img src="/img/a.png" alt="あ"></figure>
<svg viewBox="0 0 10 10"><path d="M0 0"/><text x="1" y="2">ラベル</text></svg>
<table><tr><td>セル</td></tr></table>
"""


class TestVerifyStructure(unittest.TestCase):
    def setUp(self):
        self.site = make_site(link_dirs=[])

    def test_identical_structure_passes(self):
        self.assertEqual(
            tf.verify_structure(IDENT_SRC, IDENT_JA, "ja", self.site), [])

    def test_dropped_link_caught(self):
        out = IDENT_JA.replace('<a href="https://example.com/">リンク</a>', "リンク")
        problems = tf.verify_structure(IDENT_SRC, out, "ja", self.site)
        self.assertTrue(any(p.startswith("links:") for p in problems), problems)

    def test_dropped_svg_caught(self):
        out = IDENT_JA.replace(
            '<svg viewBox="0 0 10 10"><path d="M0 0"/><text x="1" y="2">ラベル</text></svg>',
            "")
        problems = tf.verify_structure(IDENT_SRC, out, "ja", self.site)
        self.assertTrue(any(p.startswith("svg open") for p in problems), problems)
        self.assertTrue(any(p.startswith("svg close") for p in problems), problems)

    def test_missing_cjk_for_ja(self):
        problems = tf.verify_structure(IDENT_SRC, IDENT_SRC, "ja", self.site)
        self.assertTrue(any("script characters" in p for p in problems), problems)

    def test_script_check_skipped_for_unlisted_lang(self):
        problems = tf.verify_structure(IDENT_SRC, IDENT_SRC, "fr", self.site)
        self.assertEqual(problems, [])

    def test_size_ratio_floor(self):
        src = "<p>" + ("word " * 300) + "</p>"
        out = "<p>court</p>"
        problems = tf.verify_structure(src, out, "fr", self.site)
        self.assertTrue(any("suspiciously small" in p for p in problems), problems)

    def test_arabic_script_check(self):
        src = "<p>" + ("word " * 60) + "</p>"
        out = "<p>" + ("كلمة " * 60) + "</p>"
        self.assertEqual(tf.verify_structure(src, out, "ar", self.site), [])
        problems = tf.verify_structure(src, src, "ar", self.site)
        self.assertTrue(any("script characters" in p for p in problems), problems)


# ---------------------------------------------------- internal link rewrite
class LinkFixture(unittest.TestCase):
    """Site tree with content/projects/foo.md + foo.ja.md (no zh sibling)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        write(os.path.join(self.root, "content/projects/foo.md"), "<p>x</p>\n")
        write(os.path.join(self.root, "content/projects/foo.ja.md"), "<p>x</p>\n")
        write(os.path.join(self.root, "content/pages/about.md"), "<p>x</p>\n")
        write(os.path.join(self.root, "content/pages/about.ja.md"), "<p>x</p>\n")
        self.site = make_site(
            root=self.root,
            content_dirs=["content/projects", "content/pages"],
            pages_dir="content/pages",
            link_dirs=["projects", "about"],
            languages=["ja", "zh"])
        self.addCleanup(self.tmp.cleanup)


class TestRewriteInternalLinks(LinkFixture):
    def test_prefixes_when_translation_exists(self):
        text = '<a href="/projects/foo/">Foo</a>'
        self.assertEqual(tf.rewrite_internal_links(text, "ja", self.site),
                         '<a href="/ja/projects/foo/">Foo</a>')

    def test_leaves_bare_when_translation_missing(self):
        text = '<a href="/projects/foo/">Foo</a>'
        self.assertEqual(tf.rewrite_internal_links(text, "zh", self.site), text)

    def test_bare_dir_link_uses_pages_sibling(self):
        text = '<a href="/about/">About</a>'
        self.assertEqual(tf.rewrite_internal_links(text, "ja", self.site),
                         '<a href="/ja/about/">About</a>')

    def test_external_and_unlisted_links_untouched(self):
        text = ('<a href="https://example.com/projects/foo/">e</a>'
                '<a href="/lasers/bar/">u</a>')
        self.assertEqual(tf.rewrite_internal_links(text, "ja", self.site), text)

    def test_no_link_dirs_is_noop(self):
        site = make_site(root=self.root, link_dirs=[])
        text = '<a href="/projects/foo/">Foo</a>'
        self.assertEqual(tf.rewrite_internal_links(text, "ja", site), text)

    def test_anchor_stripped_when_testing_existence(self):
        self.assertTrue(tf._translation_exists(self.site, "projects", "foo#top", "ja"))
        self.assertFalse(tf._translation_exists(self.site, "projects", "foo", "zh"))

    SRC = '<p>' + ("word " * 80) + '<a href="/projects/foo/">Foo</a></p>'
    OUT = '<p>' + ("語句 " * 80) + '<a href="/projects/foo/">フー</a></p>'

    def test_verify_flags_unprefixed_existing_translation(self):
        problems = tf.verify_structure(self.SRC, self.OUT, "ja", self.site)
        self.assertTrue(any("should be /ja/-prefixed" in p for p in problems),
                        problems)

    def test_verify_accepts_correctly_prefixed_link(self):
        out = tf.rewrite_internal_links(self.OUT, "ja", self.site)
        self.assertIn('href="/ja/projects/foo/"', out)
        self.assertEqual(tf.verify_structure(self.SRC, out, "ja", self.site), [])

    def test_verify_flags_prefix_without_translation(self):
        src = '<p>' + ("word " * 80) + '<a href="/projects/foo/">Foo</a></p>'
        out = '<p>' + ("词语 " * 80) + '<a href="/zh/projects/foo/">Foo</a></p>'
        problems = tf.verify_structure(src, out, "zh", self.site)
        self.assertTrue(any("no translation" in p for p in problems), problems)


# ----------------------------------------------------------------- manifest
class TestManifest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self._orig = tf.STATE_DIR
        tf.STATE_DIR = os.path.join(self.tmp.name, "state")
        self.addCleanup(lambda: setattr(tf, "STATE_DIR", self._orig))

    def test_set_get_roundtrip(self):
        m = tf.Manifest("site1")
        self.assertIsNone(m.get("a.md", "ja"))
        m.set("a.md", "ja", {"src_sha": "s", "out_sha": "o"})
        self.assertEqual(m.get("a.md", "ja")["src_sha"], "s")
        m.save()
        m2 = tf.Manifest("site1")
        self.assertEqual(m2.get("a.md", "ja")["out_sha"], "o")

    def test_save_writes_versioned_json(self):
        m = tf.Manifest("site1")
        m.set("a.md", "ja", {"src_sha": "s"})
        m.save()
        with open(m.path, encoding="utf-8") as f:
            data = json.load(f)
        self.assertEqual(data["version"], 1)
        self.assertIn("saved_at", data)
        self.assertIn("a.md|ja", data["entries"])

    def test_save_mode_is_0600_and_no_tmp_left(self):
        m = tf.Manifest("site1")
        m.set("a.md", "ja", {"src_sha": "s"})
        m.save()
        mode = stat.S_IMODE(os.stat(m.path).st_mode)
        self.assertEqual(mode, 0o600)
        self.assertFalse(os.path.exists(m.path + ".tmp"))

    def test_prune_drops_unknown_keys(self):
        m = tf.Manifest("site1")
        m.set("a.md", "ja", {"src_sha": "s"})
        m.set("b.md", "ja", {"src_sha": "s"})
        m.prune({"a.md|ja"})
        self.assertIsNotNone(m.get("a.md", "ja"))
        self.assertIsNone(m.get("b.md", "ja"))

    def test_separate_sites_separate_files(self):
        a, b = tf.Manifest("site1"), tf.Manifest("site2")
        self.assertNotEqual(a.path, b.path)


# ----------------------------------------------------------------- classify
class TestClassify(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = self.tmp.name
        self._orig = tf.STATE_DIR
        tf.STATE_DIR = os.path.join(self.tmp.name, "state")
        self.addCleanup(lambda: setattr(tf, "STATE_DIR", self._orig))
        self.rel = "content/projects/foo.md"
        write(os.path.join(self.root, self.rel), "<p>source</p>\n")
        self.site = make_site(root=self.root,
                              content_dirs=["content/projects"],
                              languages=["ja"])
        self.m = tf.Manifest("classify")

    def sib(self, text):
        p = os.path.join(self.root, tf.sibling_path(self.rel, "ja"))
        write(p, text)
        return p

    def record(self, src_text=None, out_text=None, cfg=None):
        src = src_text if src_text is not None else \
            tf.read_text(os.path.join(self.root, self.rel))
        out = out_text if out_text is not None else \
            tf.read_text(os.path.join(self.root, tf.sibling_path(self.rel, "ja")))
        self.m.set(self.rel, "ja", {
            "src_sha": tf.sha256_text(src), "out_sha": tf.sha256_text(out),
            "cfg": cfg if cfg is not None else self.site.cfg_hash()})

    def test_missing(self):
        self.assertEqual(tf.classify(self.site, self.m, self.rel, "ja")[0], "MISSING")

    def test_untracked(self):
        self.sib("<p>訳</p>\n")
        self.assertEqual(tf.classify(self.site, self.m, self.rel, "ja")[0], "UNTRACKED")

    def test_current(self):
        self.sib("<p>訳</p>\n")
        self.record()
        self.assertEqual(tf.classify(self.site, self.m, self.rel, "ja")[0], "CURRENT")

    def test_stale_when_source_changes(self):
        self.sib("<p>訳</p>\n")
        self.record()
        write(os.path.join(self.root, self.rel), "<p>source v2</p>\n")
        self.assertEqual(tf.classify(self.site, self.m, self.rel, "ja")[0], "STALE")

    def test_edited_when_sibling_changes(self):
        self.sib("<p>訳</p>\n")
        self.record()
        self.sib("<p>手で直した</p>\n")
        self.assertEqual(tf.classify(self.site, self.m, self.rel, "ja")[0], "EDITED")

    def test_edited_wins_over_stale(self):
        self.sib("<p>訳</p>\n")
        self.record()
        self.sib("<p>手で直した</p>\n")
        write(os.path.join(self.root, self.rel), "<p>source v2</p>\n")
        self.assertEqual(tf.classify(self.site, self.m, self.rel, "ja")[0], "EDITED")

    def test_drift_on_cfg_hash_change(self):
        self.sib("<p>訳</p>\n")
        self.record(cfg="deadbeefdeadbeef")
        self.assertEqual(tf.classify(self.site, self.m, self.rel, "ja")[0], "DRIFT")

    def test_src_sha_returned(self):
        state, src_sha = tf.classify(self.site, self.m, self.rel, "ja")
        self.assertEqual(src_sha, tf.sha256_text("<p>source</p>\n"))


# ---------------------------------------------------------------- discovery
class TestDiscovery(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = self.tmp.name
        for name in ("foo.md", "foo.ja.md", "foo.zh-CN.md", "bar.html",
                     "bar.ja.html", "notes.txt"):
            write(os.path.join(self.root, "content/projects", name), "x")
        write(os.path.join(self.root, "content/pages/about.md"), "x")
        self.site = make_site(root=self.root,
                              content_dirs=["content/projects",
                                            "content/pages",
                                            "content/missing"])

    def test_skips_language_siblings_and_non_pages(self):
        self.assertEqual(tf.discover_sources(self.site), [
            "content/projects/bar.html",
            "content/projects/foo.md",
            "content/pages/about.md",
        ])

    def test_missing_dir_is_skipped_silently(self):
        self.assertNotIn("content/missing", " ".join(tf.discover_sources(self.site)))


class TestSiblingPath(unittest.TestCase):
    def test_md(self):
        self.assertEqual(tf.sibling_path("content/projects/foo.md", "ja"),
                         "content/projects/foo.ja.md")

    def test_html(self):
        self.assertEqual(tf.sibling_path("content/pages/about.html", "zh-CN"),
                         "content/pages/about.zh-CN.html")

    def test_dotted_stem(self):
        self.assertEqual(tf.sibling_path("a.b.md", "fr"), "a.b.fr.md")


# --------------------------------------------------------------- cfg_hash
class TestCfgHash(unittest.TestCase):
    def base(self, **over):
        kw = dict(model="m1", prompt_template="instruct",
                  no_translate=["OpenClaw"], style={"ja": "polite"},
                  site_description="desc", translate_fields=["title"],
                  temperature=0.3)
        kw.update(over)
        return make_site(**kw)

    def test_stable_across_instances(self):
        self.assertEqual(self.base().cfg_hash(), self.base().cfg_hash())

    def test_changes_with_no_translate(self):
        self.assertNotEqual(self.base().cfg_hash(),
                            self.base(no_translate=["OpenClaw", "DisPatch"]).cfg_hash())

    def test_unchanged_by_concurrency(self):
        self.assertEqual(self.base(concurrency="auto").cfg_hash(),
                         self.base(concurrency=4).cfg_hash())

    def test_changes_with_model_and_template_and_style(self):
        b = self.base().cfg_hash()
        self.assertNotEqual(b, self.base(model="m2").cfg_hash())
        self.assertNotEqual(b, self.base(prompt_template="hunyuan-mt").cfg_hash())
        self.assertNotEqual(b, self.base(style={"ja": "plain"}).cfg_hash())

    def test_length_is_16(self):
        self.assertEqual(len(self.base().cfg_hash()), 16)


# ------------------------------------------------------------- misc helpers
class TestMisc(unittest.TestCase):
    def test_count(self):
        self.assertEqual(tf.count('href="a" href="b"', r'href="'), 2)

    def test_write_atomic_mode_and_content(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "f.md")
            tf.write_atomic(p, "hello")
            self.assertEqual(tf.read_text(p), "hello")
            self.assertEqual(stat.S_IMODE(os.stat(p).st_mode), 0o644)
            self.assertFalse(os.path.exists(p + ".tmp"))

    def test_site_defaults_applied(self):
        s = make_site()
        self.assertEqual(s.model, tf.DEFAULTS["model"])
        self.assertEqual(s.priority, 3)
        self.assertEqual(s.concurrency, "auto")

    def test_site_override_beats_defaults(self):
        s = tf.SiteConfig("t", {"model": "d"}, {"model": "s"})
        self.assertEqual(s.model, "s")

    def test_lang_name_falls_back_to_code(self):
        s = make_site(lang_names={"ja": "Japanese"})
        self.assertEqual(s.lang_name("ja"), "Japanese")
        self.assertEqual(s.lang_name("xx"), "xx")


if __name__ == "__main__":
    unittest.main()
