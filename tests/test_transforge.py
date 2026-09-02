"""Offline tests for transforge.py — pure logic only, no network.

Run from the repo root:  python3 -m unittest discover -s tests
"""
import importlib.util
import json
import os
import re
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


class TestGlossaryDeltas(unittest.TestCase):
    def setUp(self):
        self.site = make_site(no_translate=["OpenClaw", "Kiwix", "dsh"])

    def test_no_terms_no_findings(self):
        inj, inc = tf.glossary_deltas("plain text", "プレーンテキスト", self.site)
        self.assertEqual((inj, inc), ([], []))

    def test_injection_detected(self):
        inj, inc = tf.glossary_deltas(
            "ask your assistant to search",
            "OpenClaw に検索を頼んでください", self.site)
        self.assertEqual(inj, ["OpenClaw (0 -> 1)"])
        self.assertEqual(inc, [])

    def test_increase_is_warning_not_injection(self):
        inj, inc = tf.glossary_deltas(
            "Kiwix serves the files; it is fast.",
            "Kiwix がファイルを配信します。Kiwix は高速です。", self.site)
        self.assertEqual(inj, [])
        self.assertEqual(inc, ["Kiwix (1 -> 2)"])

    def test_word_boundaries(self):
        # "dsh" inside another ASCII word must not count
        inj, inc = tf.glossary_deltas("the goldshine tool", "goldshine ツール", self.site)
        self.assertEqual((inj, inc), ([], []))
        inj, _ = tf.glossary_deltas("a tool", "dsh というツール", self.site)
        self.assertEqual(inj, ["dsh (0 -> 1)"])

    def test_verify_structure_fails_on_injection(self):
        problems = tf.verify_structure(
            "<p>ask your assistant</p>", "<p>OpenClaw に頼む</p>", "ja", self.site)
        self.assertTrue(any("glossary term injected" in p for p in problems))

    def test_verify_structure_allows_increase(self):
        problems = tf.verify_structure(
            "<p>Kiwix serves files; it is fast. 日本語</p>",
            "<p>Kiwix がファイルを配信します。Kiwix は高速です。これは十分に長い日本語の段落であり、"
            "サイズ比の下限チェックを満たすための追加の文章です。構造は同一です。"
            "さらに文章を続けて、二百文字の最小サイズ下限を確実に超えるようにします。"
            "この段落には見出しもコードブロックもリンクも含まれていないため、"
            "構造検証は文字数と用語の増加のみを評価します。まだ足りない場合に備えて、"
            "もう一文だけ追加しておきます。これで十分な長さになったはずです。</p>", "ja", self.site)
        self.assertEqual(problems, [])

    def test_hyphenated_source_variant_not_injection(self):
        site = make_site(no_translate=["Claude Code"])
        inj, inc = tf.glossary_deltas(
            "Reasonix, the Claude-Code-style agent",
            "Claude Codeスタイルのエージェント", site)
        self.assertEqual(inj, [])


# ------------------------------------------------------------- exit codes
class TestUsageExitCodes(unittest.TestCase):
    """Usage/config errors must exit 2, not the bare sys.exit(str) default 1."""

    def test_unknown_site_exits_2(self):
        with self.assertRaises(SystemExit) as cm:
            tf.pick_site({"a": object()}, "nope")
        self.assertEqual(cm.exception.code, 2)

    def test_ambiguous_site_exits_2(self):
        with self.assertRaises(SystemExit) as cm:
            tf.pick_site({"a": object(), "b": object()}, None)
        self.assertEqual(cm.exception.code, 2)

    def test_missing_config_exits_2(self):
        orig = tf.CONFIG_PATH
        tf.CONFIG_PATH = os.path.join(tempfile.gettempdir(), "no-such-transforge.toml")
        self.addCleanup(lambda: setattr(tf, "CONFIG_PATH", orig))
        with self.assertRaises(SystemExit) as cm:
            tf.load_config()
        self.assertEqual(cm.exception.code, 2)

    def test_bad_toml_exits_2(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "config.toml")
            write(p, "[defaults\nmodel = broken\n")
            orig = tf.CONFIG_PATH
            tf.CONFIG_PATH = p
            self.addCleanup(lambda: setattr(tf, "CONFIG_PATH", orig))
            with self.assertRaises(SystemExit) as cm:
                tf.load_config()
            self.assertEqual(cm.exception.code, 2)

    def test_no_sites_exits_2(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "config.toml")
            write(p, "[defaults]\nmodel = \"m\"\n")
            orig = tf.CONFIG_PATH
            tf.CONFIG_PATH = p
            self.addCleanup(lambda: setattr(tf, "CONFIG_PATH", orig))
            with self.assertRaises(SystemExit) as cm:
                tf.load_config()
            self.assertEqual(cm.exception.code, 2)

    def test_bad_concurrency_exits_2(self):
        with self.assertRaises(SystemExit) as cm:
            make_site(concurrency="lots")
        self.assertEqual(cm.exception.code, 2)

    def test_numeric_string_concurrency_is_coerced(self):
        self.assertEqual(make_site(concurrency="4").concurrency, 4)
        self.assertEqual(make_site(concurrency=4).concurrency, 4)
        self.assertEqual(make_site().concurrency, "auto")

    def test_corrupt_manifest_exits_2(self):
        with tempfile.TemporaryDirectory() as d:
            orig = tf.STATE_DIR
            tf.STATE_DIR = d
            self.addCleanup(lambda: setattr(tf, "STATE_DIR", orig))
            write(os.path.join(d, "s-manifest.json"), "{not json")
            with self.assertRaises(SystemExit) as cm:
                tf.Manifest("s")
            self.assertEqual(cm.exception.code, 2)


# ------------------------------------------------------- link-dir escaping
class TestLinkDirEscaping(unittest.TestCase):
    """A link dir may contain regex metacharacters; it must be escaped, and
    it must not accidentally match a similar-looking path."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = self.tmp.name
        write(os.path.join(self.root, "content/c++/foo.md"), "<p>x</p>\n")
        write(os.path.join(self.root, "content/c++/foo.ja.md"), "<p>x</p>\n")
        self.site = make_site(
            root=self.root, content_dirs=["content/c++"],
            pages_dir="content/pages", link_dirs=["c++"], languages=["ja"])

    def test_pattern_is_escaped(self):
        self.assertEqual(tf.link_dirs_pattern(self.site), re.escape("c++"))

    def test_metachar_dir_is_rewritten(self):
        text = '<a href="/c++/foo/">Foo</a>'
        self.assertEqual(tf.rewrite_internal_links(text, "ja", self.site),
                         '<a href="/ja/c++/foo/">Foo</a>')

    def test_metachar_dir_does_not_match_regex_expansion(self):
        # unescaped, "c++" would match "c" followed by one-or-more "+"
        text = '<a href="/c/foo/">C</a>'
        self.assertEqual(tf.rewrite_internal_links(text, "ja", self.site), text)


# ------------------------------------------------------------ path helpers
class TestUnderRoot(unittest.TestCase):
    def test_descendant(self):
        self.assertTrue(tf.under_root("/a/site/content/x.md", "/a/site"))

    def test_same_path(self):
        self.assertTrue(tf.under_root("/a/site", "/a/site"))

    def test_prefix_sibling_is_not_under_root(self):
        self.assertFalse(tf.under_root("/a/site-backup/x.md", "/a/site"))

    def test_trailing_slash_root(self):
        self.assertTrue(tf.under_root("/a/site/x.md", "/a/site/"))

    def test_normalize_files_rejects_prefix_sibling(self):
        site = make_site(root="/a/site")
        self.assertEqual(tf.normalize_files(site, ["/a/site/content/x.md"]),
                         ["content/x.md"])
        self.assertEqual(tf.normalize_files(site, ["/a/site-backup/content/x.md"]),
                         ["/a/site-backup/content/x.md"])


# --------------------------------------------------------- manifest prune
class TestPruneManifest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        orig = tf.STATE_DIR
        tf.STATE_DIR = os.path.join(self.tmp.name, "state")
        self.addCleanup(lambda: setattr(tf, "STATE_DIR", orig))

    def test_prune_returns_removed_count(self):
        m = tf.Manifest("s")
        m.set("a.md", "ja", {"src_sha": "s"})
        m.set("gone.md", "ja", {"src_sha": "s"})
        self.assertEqual(m.prune({"a.md|ja"}), 1)
        self.assertEqual(m.prune({"a.md|ja"}), 0)

    def test_prune_manifest_persists(self):
        m = tf.Manifest("s")
        m.set("a.md", "ja", {"src_sha": "s"})
        m.set("gone.md", "ja", {"src_sha": "s"})
        m.save()
        removed = tf.prune_manifest(m, [("a.md", "ja")])
        self.assertEqual(removed, 1)
        self.assertIsNone(tf.Manifest("s").get("gone.md", "ja"))
        self.assertIsNotNone(tf.Manifest("s").get("a.md", "ja"))

    def test_prune_manifest_noop_leaves_entries(self):
        m = tf.Manifest("s")
        m.set("a.md", "ja", {"src_sha": "s"})
        m.save()
        self.assertEqual(tf.prune_manifest(m, [("a.md", "ja")]), 0)
        self.assertIsNotNone(tf.Manifest("s").get("a.md", "ja"))


if __name__ == "__main__":
    unittest.main()


# ---------------------------------------------- chunk structural contract
CHUNK_SRC = (
    '<h2>Setup</h2>\n'
    '<p>Run <code>transforge status</code> then <code>transforge run</code>, '
    'and read <a href="/projects/x/">the notes</a>.</p>\n'
    '<pre><code>transforge run --site laserlloyd</code></pre>\n'
    '<p><img src="/a.png"> a caption sentence with enough words to clear the floor.</p>'
)


def _es(text):
    """A faithful-shaped Spanish rendering of CHUNK_SRC's prose."""
    return (text.replace("Setup", "Instalación")
                .replace("then", "y luego")
                .replace("the notes", "las notas")
                .replace("a caption sentence with enough words to clear the floor",
                         "una frase de pie de foto con palabras suficientes"))


class FakeRig:
    """Returns a scripted reply per call; records the budgets it was asked for."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    def chat(self, messages, max_tokens, temperature):
        self.calls.append({"messages": messages, "max_tokens": max_tokens})
        text, finish = self.replies.pop(0)
        return {"choices": [{"message": {"content": text},
                             "finish_reason": finish}]}


class TestStructureDeltas(unittest.TestCase):
    def test_faithful_output_has_no_deltas(self):
        self.assertEqual(tf.structure_deltas(CHUNK_SRC, _es(CHUNK_SRC)), [])

    def test_dropped_inline_code_is_a_delta(self):
        out = _es(CHUNK_SRC).replace("<code>transforge status</code>", "transforge status")
        self.assertIn(("inline code", 3, 2), tf.structure_deltas(CHUNK_SRC, out))

    def test_invented_inline_code_is_a_delta(self):
        out = _es(CHUNK_SRC).replace("las notas", "<code>las notas</code>")
        self.assertIn(("inline code", 3, 4), tf.structure_deltas(CHUNK_SRC, out))

    def test_verify_structure_reports_every_delta(self):
        site = make_site(link_dirs=[])
        out = _es(CHUNK_SRC).replace("<code>transforge status</code>", "transforge status")
        problems = tf.verify_structure(CHUNK_SRC, out, "es", site)
        self.assertTrue(any(p.startswith("inline code: 3 -> 2") for p in problems), problems)


class TestTranslateChunkContract(unittest.TestCase):
    """The regression that broke translate-web-changes for es/zh: an inline
    <code> count drift is recoverable model noise, but had no retry — only
    href/src did — so it surfaced as a hard document failure."""

    def _translator(self, replies):
        site = make_site(link_dirs=[], languages=["es"],
                         lang_names={"es": "Spanish"})
        rig = FakeRig(replies)
        return tf.Translator(site, rig), rig

    def test_faithful_first_try_returns_immediately(self):
        good = _es(CHUNK_SRC)
        tr, rig = self._translator([(good, "stop")])
        self.assertEqual(tr.translate_chunk(CHUNK_SRC, "es"), good)
        self.assertEqual(len(rig.calls), 1)

    def test_inline_code_drift_is_retried_and_recovers(self):
        bad = _es(CHUNK_SRC).replace("<code>transforge status</code>", "transforge status")
        good = _es(CHUNK_SRC)
        tr, rig = self._translator([(bad, "stop"), (good, "stop")])
        self.assertEqual(tr.translate_chunk(CHUNK_SRC, "es"), good)
        self.assertEqual(len(rig.calls), 2, "a code-count drift must be retried")
        nudge = rig.calls[1]["messages"][-1]["content"]
        self.assertIn("inline code 3 -> 2", nudge)

    def test_persistent_drift_still_fails_hard(self):
        # A single <p> with no blank line and one closing tag cannot be
        # subdivided, so this exercises the terminal failure path.
        src = '<p>Alpha <code>uno</code> beta gamma delta epsilon zeta eta.</p>'
        bad = src.replace("<code>uno</code>", "uno")
        tr, rig = self._translator([(bad, "stop")] * 3)
        with self.assertRaises(RuntimeError) as cm:
            tr.translate_chunk(src, "es")
        self.assertIn("inline code 1->0", str(cm.exception))
        self.assertEqual(len(rig.calls), 3)

    def test_link_drift_still_retried(self):
        bad = _es(CHUNK_SRC).replace('<a href="/projects/x/">las notas</a>', "las notas")
        good = _es(CHUNK_SRC)
        tr, rig = self._translator([(bad, "stop"), (good, "stop")])
        self.assertEqual(tr.translate_chunk(CHUNK_SRC, "es"), good)
        self.assertIn("links 1 -> 0", rig.calls[1]["messages"][-1]["content"])

    def test_truncation_retries_on_the_larger_budget(self):
        good = _es(CHUNK_SRC)
        tr, rig = self._translator([("cut off", "length"), (good, "stop")])
        self.assertEqual(tr.translate_chunk(CHUNK_SRC, "es"), good)
        self.assertGreater(rig.calls[1]["max_tokens"], rig.calls[0]["max_tokens"])

    def test_contract_covers_every_document_gate_marker(self):
        """translate_chunk and verify_structure must judge the same markers —
        an invariant the document is rejected for with no chunk retry is
        exactly the defect this test exists to prevent."""
        self.assertEqual(
            [n for n, _ in tf.STRUCTURE_CHECKS],
            ["headings h2/h3", "svg open", "svg close",
             "code blocks open", "code blocks close", "inline code",
             "tables open", "tables close", "figures open", "figures close",
             "images", "links", "src attrs", "svg path d=", "svg text elements"])


# ------------------------------------------------------- code-span masking
class TestMaskCodeSpans(unittest.TestCase):
    def test_inline_and_block_spans_are_masked(self):
        masked, spans = tf.mask_code_spans(CHUNK_SRC)
        self.assertEqual(len(spans), 3)      # 2 inline + 1 <pre><code> block
        self.assertNotIn("<code", masked)
        self.assertEqual(masked.count("<tfspan"), 3)

    def test_roundtrip_is_lossless(self):
        masked, spans = tf.mask_code_spans(CHUNK_SRC)
        self.assertEqual(tf.restore_code_spans(masked, spans), CHUNK_SRC)

    def test_placeholder_matches_no_structure_check(self):
        """A placeholder must be invisible to every counted marker, or masking
        would corrupt the very check it exists to protect."""
        masked, _ = tf.mask_code_spans(CHUNK_SRC)
        for name, pat in tf.STRUCTURE_CHECKS:
            self.assertEqual(
                len(re.findall(pat, '<tfspan i="0"/>')), 0,
                f"placeholder collides with the {name!r} pattern")
        self.assertNotIn("<code", masked)

    def test_tolerates_every_shape_the_model_might_return(self):
        spans = ["<code>x</code>"]
        for variant in ('<tfspan i="0"/>', '<tfspan i="0">',
                        '<tfspan  i="0" />', '<tfspan i="0"></tfspan>',
                        '<tfspan i="0">x</tfspan>',
                        '<tfspan i="0">rewritten body</tfspan>'):
            self.assertEqual(tf.restore_code_spans(f"<p>a {variant} b</p>", spans),
                             "<p>a <code>x</code> b</p>", variant)

    def test_invented_index_is_left_alone_for_the_checker_to_catch(self):
        spans = ["<code>x</code>"]
        out = tf.restore_code_spans('<p><tfspan i="9"/></p>', spans)
        self.assertIn('<tfspan i="9"/>', out)
        self.assertEqual(tf.count(out, r"<code[\s>]"), 0)


class TestTranslateChunkMasking(unittest.TestCase):
    def _translator(self, responder):
        site = make_site(link_dirs=[], languages=["es"], lang_names={"es": "Spanish"})

        class Rig:
            def __init__(self):
                self.seen = []

            def chat(self, messages, max_tokens, temperature):
                # messages[1] is the chunk under translation; messages[-1] on a
                # retry is the corrective nudge, which must never be echoed.
                self.seen.append(messages[1]["content"])
                return {"choices": [{"message": {"content": responder(self.seen[-1])},
                                     "finish_reason": "stop"}]}

        rig = Rig()
        return tf.Translator(site, rig), rig

    def test_model_never_sees_a_code_tag(self):
        tr, rig = self._translator(lambda sent: _es(sent))
        out = tr.translate_chunk(CHUNK_SRC, "es")
        self.assertNotIn("<code", rig.seen[0], "code spans must be masked before sending")
        self.assertIn("<tfspan", rig.seen[0])
        self.assertEqual(tf.count(out, r"<code[\s>]"), 3)
        self.assertEqual(tf.count(out, r"<pre><code>"), 1)
        self.assertNotIn("<tfspan", out, "placeholders must not survive into output")

    def test_code_span_bodies_come_back_untouched(self):
        """The live bug: the model marked up plain-text repeats of a token it
        had seen inside <code>. With masking it cannot see the tag at all."""
        src = ('<p>The tool is <code>bench-llm</code>. I used bench-llm daily and '
               'bench-llm was retired; bench-llm still works fine for this.</p>')

        def mangle(sent):
            # Models the observed bias: the tag is only propagated onto bare
            # repeats when the model can SEE a <code> tag in its input. Masking
            # removes the cue, so this responder leaves the prose alone.
            if "<code" not in sent:
                return sent
            return sent.replace("bench-llm", "<code>bench-llm</code>")

        tr, _ = self._translator(mangle)
        out = tr.translate_chunk(src, "es")
        self.assertEqual(tf.count(out, r"<code[\s>]"), 1)
        self.assertIn("<code>bench-llm</code>", out)


class TestTranslateChunkSubdivision(unittest.TestCase):
    """A pure-MT model paraphrases long blocks and dissolves placeholders.
    Retrying the identical request cannot fix that; translating the block in
    smaller parts can, and that is the escalation under test."""

    MULTI = ('<p>Alpha uses <code>one</code> here.</p>\n\n'
             '<p>Beta uses <code>two</code> here.</p>\n\n'
             '<p>Gamma uses <code>three</code> here.</p>')

    def _translator(self, responder):
        site = make_site(link_dirs=[], languages=["es"], lang_names={"es": "Spanish"})

        class Rig:
            def __init__(self):
                self.seen = []

            def chat(self, messages, max_tokens, temperature):
                # messages[1] is the chunk under translation; messages[-1] on a
                # retry is the corrective nudge, which must never be echoed.
                self.seen.append(messages[1]["content"])
                return {"choices": [{"message": {"content": responder(self.seen[-1])},
                                     "finish_reason": "stop"}]}

        rig = Rig()
        return tf.Translator(site, rig), rig

    def test_drops_a_placeholder_on_a_long_block_but_not_a_short_one(self):
        def responder(sent):
            # Faithful on a single paragraph; drops the first placeholder when
            # handed the whole multi-paragraph block. This is the live failure
            # shape: <tfspan i="0"/> dissolved into paraphrased prose.
            if sent.count("<p>") > 1:
                return re.sub(r'<tfspan i="0"[^>]*>.*?</tfspan>', "uno",
                              sent, count=1, flags=re.S)
            return sent

        tr, rig = self._translator(responder)
        out = tr.translate_chunk(self.MULTI, "es")
        self.assertEqual(tf.count(out, r"<code[\s>]"), 3)
        self.assertEqual(tf.structure_deltas(self.MULTI, out), [])
        self.assertEqual(len([s for s in rig.seen if s.count("<p>") > 1]), 3,
                         "three whole-block attempts, then subdivision")
        self.assertEqual(len([s for s in rig.seen if s.count("<p>") == 1]), 3,
                         "one call per paragraph after escalation")

    def test_indivisible_block_still_fails_hard(self):
        single = '<p>Only <code>one</code> paragraph here, nothing to split on.</p>'
        tr, rig = self._translator(
            lambda sent: re.sub(r'<tfspan i="0"[^>]*>.*?</tfspan>', "uno",
                                sent, flags=re.S))
        with self.assertRaises(RuntimeError) as cm:
            tr.translate_chunk(single, "es")
        self.assertIn("inline code 1->0", str(cm.exception))

    def test_subdivision_depth_is_bounded(self):
        """A model that fails at every granularity must terminate, not recurse."""
        tr, rig = self._translator(
            lambda sent: re.sub(r'<tfspan i="0"[^>]*>.*?</tfspan>', "uno",
                                sent, flags=re.S))
        with self.assertRaises(RuntimeError):
            tr.translate_chunk(self.MULTI, "es")
        self.assertLess(len(rig.seen), 40, "recursion must be bounded")

    def test_truncated_block_escalates_to_subdivision(self):
        calls = {"n": 0}

        class Rig:
            def __init__(self):
                self.seen = []

            def chat(self, messages, max_tokens, temperature):
                sent = messages[1]["content"]
                self.seen.append(sent)
                calls["n"] += 1
                if sent.count("<p>") > 1:
                    return {"choices": [{"message": {"content": "cut"},
                                         "finish_reason": "length"}]}
                return {"choices": [{"message": {"content": sent},
                                     "finish_reason": "stop"}]}

        site = make_site(link_dirs=[], languages=["es"], lang_names={"es": "Spanish"})
        rig = Rig()
        out = tf.Translator(site, rig).translate_chunk(self.MULTI, "es")
        self.assertEqual(tf.count(out, r"<code[\s>]"), 3)


class TestRetryTemperature(unittest.TestCase):
    """A structural retry exists because the sample was unfaithful; repeating it
    at the same temperature is asking the same dice to land differently."""

    def test_first_try_uses_configured_temperature_retries_sample_down(self):
        src = '<p>Alpha <code>one</code> beta gamma delta epsilon zeta.</p>'
        temps = []

        class Rig:
            def chat(self, messages, max_tokens, temperature):
                temps.append(temperature)
                sent = messages[1]["content"]
                # unfaithful on the first sample, faithful afterwards
                body = (re.sub(r'<tfspan i="0"[^>]*>.*?</tfspan>', "uno", sent, flags=re.S)
                        if len(temps) == 1 else sent)
                return {"choices": [{"message": {"content": body},
                                     "finish_reason": "stop"}]}

        site = make_site(link_dirs=[], languages=["es"],
                         lang_names={"es": "Spanish"}, temperature=0.7)
        out = tf.Translator(site, Rig()).translate_chunk(src, "es")
        self.assertEqual(tf.count(out, r"<code[\s>]"), 1)
        self.assertEqual(temps[0], 0.7, "first pass keeps the configured temperature")
        self.assertLessEqual(temps[1], 0.2, "retries must sample down for fidelity")

    def test_a_low_configured_temperature_is_never_raised(self):
        src = '<p>Alpha <code>one</code> beta gamma delta epsilon zeta.</p>'
        temps = []

        class Rig:
            def chat(self, messages, max_tokens, temperature):
                temps.append(temperature)
                sent = messages[1]["content"]
                body = (re.sub(r'<tfspan i="0"[^>]*>.*?</tfspan>', "uno", sent, flags=re.S)
                        if len(temps) == 1 else sent)
                return {"choices": [{"message": {"content": body},
                                     "finish_reason": "stop"}]}

        site = make_site(link_dirs=[], languages=["es"],
                         lang_names={"es": "Spanish"}, temperature=0.05)
        tf.Translator(site, Rig()).translate_chunk(src, "es")
        self.assertEqual(temps[1], 0.05)


class TestSubdivideBlock(unittest.TestCase):
    def test_blank_lines_win_and_rejoin_with_a_blank_line(self):
        parts, joiner = tf.subdivide_block("<p>one</p>\n\n<p>two</p>")
        self.assertEqual(len(parts), 2)
        self.assertEqual(joiner, "\n\n")

    def test_a_list_with_no_blank_lines_splits_on_items(self):
        """The shape that broke the nightly: one <ul>, six <code> spans, not a
        blank line anywhere inside it."""
        ul = ("<ul>\n"
              + "".join(f"<li>item {i} uses <code>c{i}</code></li>\n" for i in range(6))
              + "</ul>")
        self.assertEqual(len(re.split(r"\n\s*\n", ul)), 1, "fixture must have no blank line")
        parts, joiner = tf.subdivide_block(ul)
        self.assertEqual(len(parts), 6)
        self.assertEqual(joiner, "\n")

    def test_table_rows_and_paragraphs_are_also_boundaries(self):
        rows = "<table><tr><td>a</td></tr><tr><td>b</td></tr></table>"
        self.assertEqual(len(tf.subdivide_block(rows)[0]), 2)
        paras = "<p>one</p><p>two</p><p>three</p>"
        self.assertEqual(len(tf.subdivide_block(paras)[0]), 3)

    def test_a_markup_only_tail_is_folded_into_its_neighbour(self):
        """Splitting after </li> leaves a bare </ul>; a part with no text has
        no countable marker, so anything the model returned for it would be
        accepted unchecked."""
        ul = "<ul>\n<li>a <code>x</code></li>\n<li>b <code>y</code></li>\n</ul>"
        parts, _ = tf.subdivide_block(ul)
        self.assertEqual(len(parts), 2)
        self.assertTrue(parts[-1].rstrip().endswith("</ul>"))
        for part in parts:
            self.assertRegex(re.sub(r"<[^>]*>", "", part), r"[^\W_]")

    def test_rejoining_list_parts_never_inserts_a_blank_line(self):
        """A blank line inside a raw <ul> ends the HTML block for the site's
        markdown renderer, so the list join must stay a single newline."""
        ul = "<ul>\n<li>a <code>x</code></li>\n<li>b <code>y</code></li>\n</ul>"
        parts, joiner = tf.subdivide_block(ul)
        self.assertNotIn("\n\n", joiner.join(p.strip() for p in parts))

    def test_indivisible_block_reports_so(self):
        self.assertEqual(tf.subdivide_block("<p>one single block</p>"), (None, None))

    def test_list_block_recovers_end_to_end(self):
        ul = ("<ul>\n"
              + "".join(f"<li>item {i} uses <code>c{i}</code> here</li>\n" for i in range(6))
              + "</ul>")

        class Rig:
            def chat(self, messages, max_tokens, temperature):
                sent = messages[1]["content"]
                # Faithful only once the list has been broken into single items.
                if sent.count("<li>") > 1:
                    return {"choices": [{"message": {"content": re.sub(
                        r'<tfspan i="0"[^>]*>.*?</tfspan>', "gone", sent, flags=re.S)},
                        "finish_reason": "stop"}]}
                return {"choices": [{"message": {"content": sent},
                                     "finish_reason": "stop"}]}

        site = make_site(link_dirs=[], languages=["es"], lang_names={"es": "Spanish"})
        out = tf.Translator(site, Rig()).translate_chunk(ul, "es")
        self.assertEqual(tf.count(out, r"<code[\s>]"), 6)
        self.assertNotIn("\n\n", out)
