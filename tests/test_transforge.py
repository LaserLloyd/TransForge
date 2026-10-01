"""Offline tests for transforge.py — pure logic only, no network.

Run from the repo root:  python3 -m unittest discover -s tests
"""
import argparse
import contextlib
import importlib.util
import io
import json
import os
import re
import stat
import sys
import tempfile
import unittest
from unittest import mock

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


# ---------------------------------------------- glossary prompt scoping
class TestGlossaryPromptScoping(unittest.TestCase):
    """The 2026-09-07 fix: no_translate is site-wide, but a segment's prompt
    must only carry the terms that ACTUALLY OCCUR in that segment's own
    source text -- sending the whole list regardless of relevance is what let
    Hy-MT2 (a pure-MT model) invent "OpenClaw" in German prose for a page
    that never mentions it (glossary_deltas caught it: 0 -> 1). Scoping the
    prompt removes the temptation instead of only detecting it after the
    fact."""

    def setUp(self):
        self.site = make_site(no_translate=["OpenClaw", "CrucibleForge", "DisPatch"])
        self.tr = tf.Translator(self.site, rig=None)

    def test_terms_for_only_returns_terms_present_in_text(self):
        text = "CrucibleForge benchmarks local models."
        terms = self.tr._terms_for(text)
        self.assertIn("CrucibleForge", terms)
        self.assertNotIn("OpenClaw", terms)
        self.assertNotIn("DisPatch", terms)

    def test_terms_for_empty_when_no_terms_present(self):
        self.assertEqual(self.tr._terms_for("just plain prose, no brands here"), "")

    def test_terms_for_empty_for_empty_text(self):
        self.assertEqual(self.tr._terms_for(""), "")
        self.assertEqual(self.tr._terms_for(None), "")

    def test_body_prompt_hunyuan_omits_absent_terms(self):
        site = make_site(prompt_template="hunyuan-mt", no_translate=["OpenClaw"],
                         lang_names={"de": "German"})
        tr = tf.Translator(site, rig=None)
        chunk = "<p>CrucibleForge is a benchmark tool.</p>"
        p = tr.body_prompt("de", chunk)
        self.assertNotIn("OpenClaw", p)

    def test_body_prompt_instruct_includes_only_present_terms(self):
        site = make_site(prompt_template="instruct",
                         no_translate=["OpenClaw", "CrucibleForge"],
                         lang_names={"de": "German"})
        tr = tf.Translator(site, rig=None)
        chunk = "<p>CrucibleForge is a benchmark tool.</p>"
        p = tr.body_prompt("de", chunk)
        self.assertIn("CrucibleForge", p)
        self.assertNotIn("OpenClaw", p)

    def test_fm_prompt_omits_absent_terms(self):
        site = make_site(prompt_template="instruct", no_translate=["OpenClaw"],
                         lang_names={"de": "German"})
        tr = tf.Translator(site, rig=None)
        p = tr.fm_prompt(["title"], "de", "title: A CrucibleForge deep dive")
        self.assertNotIn("OpenClaw", p)

    def test_text_prompt_omits_absent_terms(self):
        site = make_site(prompt_template="hunyuan-mt", no_translate=["OpenClaw"],
                         lang_names={"de": "German"})
        tr = tf.Translator(site, rig=None)
        p = tr.text_prompt("de", "CrucibleForge runs benchmarks.")
        self.assertNotIn("OpenClaw", p)


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
    '<pre><code>transforge run --site example</code></pre>\n'
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
    """The regression that broke the nightly translation job for es/zh: an inline
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


# --------------------------------------------------- `transforge text` (2.8)
class TestTranslateTextPrompt(unittest.TestCase):
    """Translator.text_prompt() / translate_text() -- the request-building
    path `transforge text` reuses. No HTML/JSON ever appears here (plain text
    only, by spec)."""

    def test_hunyuan_mt_is_terse_and_omits_source_by_default(self):
        site = make_site(prompt_template="hunyuan-mt",
                         lang_names={"ja": "Japanese"})
        tr = tf.Translator(site, rig=None)
        p = tr.text_prompt("ja", "Good morning")
        self.assertIn("into Japanese", p)
        self.assertNotIn("from ", p)

    def test_hunyuan_mt_mentions_an_explicit_non_english_source(self):
        site = make_site(prompt_template="hunyuan-mt",
                         lang_names={"ja": "Japanese", "es": "Spanish"})
        tr = tf.Translator(site, rig=None)
        p = tr.text_prompt("ja", "Buenos dias", from_lang="es")
        self.assertIn("from Spanish into Japanese", p)

    def test_default_from_en_does_not_produce_a_from_clause(self):
        """The CLI's own --from default is the literal string "en" (not
        None) so the JSON output always names a source -- but the PROMPT
        must still read as the tool's usual implicit-English default, not
        literally "from en"."""
        site = make_site(prompt_template="hunyuan-mt",
                         lang_names={"ja": "Japanese"})
        tr = tf.Translator(site, rig=None)
        p = tr.text_prompt("ja", "Good morning", from_lang="en")
        self.assertNotIn("from ", p)
        self.assertNotIn(" en ", p)

    def test_instruct_prompt_is_conversational_and_has_no_html_rules(self):
        site = make_site(prompt_template="instruct", lang_names={"ja": "Japanese"})
        tr = tf.Translator(site, rig=None)
        p = tr.text_prompt("ja", "Good morning")
        self.assertIn("professional translator", p)
        self.assertIn("plain text", p)
        for html_marker in ("<code>", "<pre>", "HTML", "JSON", "tag"):
            self.assertNotIn(html_marker, p)

    def test_terms_and_style_are_included_in_both_templates(self):
        for template in ("hunyuan-mt", "instruct"):
            site = make_site(prompt_template=template, lang_names={"ja": "Japanese"},
                             no_translate=["OpenClaw"], style={"ja": "polite desu/masu"})
            tr = tf.Translator(site, rig=None)
            p = tr.text_prompt("ja", "Ask OpenClaw for help")
            self.assertIn("OpenClaw", p, template)
            self.assertIn("polite desu/masu", p, template)

    def test_translate_text_returns_stripped_reply(self):
        site = make_site(lang_names={"ja": "Japanese"})
        rig = FakeRig([("  Ohayo gozaimasu  ", "stop")])
        tr = tf.Translator(site, rig)
        self.assertEqual(tr.translate_text("Good morning", "ja"), "Ohayo gozaimasu")
        self.assertEqual(rig.calls[0]["max_tokens"], site.max_tokens_body)

    def test_translate_text_retries_on_truncation_with_the_body_retry_budget(self):
        site = make_site(lang_names={"ja": "Japanese"})
        rig = FakeRig([("cut off mid", "length"), ("Ohayo gozaimasu", "stop")])
        tr = tf.Translator(site, rig)
        self.assertEqual(tr.translate_text("Good morning", "ja"), "Ohayo gozaimasu")
        self.assertEqual(len(rig.calls), 2)
        self.assertEqual(rig.calls[1]["max_tokens"], site.max_tokens_body_retry)

    def test_translate_text_raises_on_persistent_empty_output(self):
        site = make_site(lang_names={"ja": "Japanese"})
        rig = FakeRig([("", "stop"), ("", "stop")])
        tr = tf.Translator(site, rig)
        with self.assertRaises(RuntimeError):
            tr.translate_text("Good morning", "ja")


class _FakeHTTPResponse:
    def __init__(self, payload):
        self._payload = json.dumps(payload).encode("utf-8")

    def read(self):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class TestRigChatRequestBuilding(unittest.TestCase):
    """The HTTP layer, mocked: proves the request-building path `text` reuses
    (Rig.chat) really does put config temperature/extra_params (top_p/top_k/
    repeat) and the site's model on the wire -- the rig is never required."""

    def test_payload_carries_temperature_and_extra_params_from_config(self):
        site = make_site(model="vendor/m", temperature=0.42,
                         extra_params={"top_p": 0.6, "top_k": 20,
                                      "repeat_penalty": 1.05})
        rig = tf.Rig(site)
        captured = {}

        def fake_urlopen(req, timeout=30):
            captured["body"] = json.loads(req.data.decode("utf-8"))
            return _FakeHTTPResponse({
                "model": "vendor/m",
                "choices": [{"message": {"content": "hola"},
                            "finish_reason": "stop"}]})

        with mock.patch.object(tf.urllib.request, "urlopen", fake_urlopen):
            data = rig.chat([{"role": "user", "content": "hi"}], 100, site.temperature)
        self.assertEqual(data["choices"][0]["message"]["content"], "hola")
        body = captured["body"]
        self.assertEqual(body["model"], "vendor/m")
        self.assertEqual(body["temperature"], 0.42)
        self.assertEqual(body["top_p"], 0.6)
        self.assertEqual(body["top_k"], 20)
        self.assertEqual(body["repeat_penalty"], 1.05)
        self.assertEqual(body["max_tokens"], 100)


class FakeCliRig:
    """Stands in for Rig at the `cmd_text` call site (patches tf.Rig) so
    warmup()'s real branches run against scripted rig state -- no network, no
    live StudioForge rig required. `mode` selects which branch fires."""

    def __init__(self, site, mode="ok", chat_reply="(translated)"):
        self.site = site
        self.mode = mode
        self.chat_reply = chat_reply
        self.chat_calls = []

    def bench_lease(self):
        if self.mode == "leased":
            return {"holder": "crucibleforge", "kind": "benchmark"}
        return None

    def models(self):
        if self.mode == "unreachable":
            raise tf.urllib.error.URLError("connection refused")
        if self.mode == "missing":
            return [{"id": "some/other-model"}]
        return [{"id": self.site.model}]

    def live_parallel(self, model_id):
        return 2

    def loaded_row(self, model_id):
        return {"model_id": model_id, "state": "ready", "plan": {"parallel": 2}}

    def wait_ready(self, model_id, timeout_s=900):
        return self.loaded_row(model_id)

    def pin(self):
        return "unused-test-pin"

    def unload(self, model_id):
        raise AssertionError("unload() should not be reached in these tests")

    def load_recommended(self, model_id, ctx_size, priority):
        raise AssertionError("load_recommended() should not be reached — "
                              "the fake always reports parallel=2 resident")

    def chat(self, messages, max_tokens, temperature):
        self.chat_calls.append({"messages": messages, "max_tokens": max_tokens,
                                "temperature": temperature})
        return {"choices": [{"message": {"content": self.chat_reply},
                             "finish_reason": "stop"}]}


def _text_args(**kw):
    base = dict(text=None, to="ja", from_lang="en", model=None, json=False,
               site=None, no_warmup=False)
    base.update(kw)
    return argparse.Namespace(**base)


class TestCmdTextIO(unittest.TestCase):
    """argument vs stdin, --json shape -- the HTTP layer mocked via a fake
    Rig patched over tf.Rig, exactly like warmup()'s real branches would see
    a real rig, so no live StudioForge is required for the suite."""

    def _site(self, **kw):
        return make_site(model="vendor/m", languages=["ja"], **kw)

    def _run(self, args, site, mode="ok", chat_reply="Ohayo gozaimasu"):
        sites = {site.name: site}
        holder = {}

        def make_rig(s):
            holder["rig"] = FakeCliRig(s, mode=mode, chat_reply=chat_reply)
            return holder["rig"]

        buf = io.StringIO()
        with mock.patch.object(tf, "Rig", make_rig):
            with contextlib.redirect_stdout(buf):
                rc = tf.cmd_text(args, sites)
        return rc, buf.getvalue(), holder["rig"]

    def test_translates_the_positional_argument(self):
        rc, out, rig = self._run(_text_args(text="Good morning"), self._site())
        self.assertEqual(rc, 0)
        self.assertEqual(out.strip(), "Ohayo gozaimasu")
        self.assertEqual(len(rig.chat_calls), 1)
        self.assertIn("Good morning", rig.chat_calls[0]["messages"][-1]["content"])

    def test_translates_stdin_when_argument_omitted(self):
        with mock.patch.object(tf.sys, "stdin", io.StringIO("Good evening\n")):
            rc, out, rig = self._run(_text_args(text=None), self._site())
        self.assertEqual(rc, 0)
        self.assertEqual(out.strip(), "Ohayo gozaimasu")
        self.assertIn("Good evening", rig.chat_calls[0]["messages"][-1]["content"])

    def test_json_output_has_exactly_the_four_keys(self):
        rc, out, _ = self._run(_text_args(text="hi", json=True), self._site())
        self.assertEqual(rc, 0)
        data = json.loads(out.strip())
        self.assertEqual(set(data.keys()), {"from", "to", "model", "text"})
        self.assertEqual(data, {"from": "en", "to": "ja", "model": "vendor/m",
                                "text": "Ohayo gozaimasu"})

    def test_plain_output_is_bare_text_not_json(self):
        rc, out, _ = self._run(_text_args(text="hi"), self._site())
        self.assertEqual(out.strip(), "Ohayo gozaimasu")
        with self.assertRaises(json.JSONDecodeError):
            json.loads(out.strip())

    def test_model_override_is_used_and_does_not_mutate_the_shared_site(self):
        site = self._site()
        rc, out, rig = self._run(
            _text_args(text="hi", model="vendor/override", json=True), site)
        self.assertEqual(rc, 0)
        data = json.loads(out.strip())
        self.assertEqual(data["model"], "vendor/override")
        # the site object living in the `sites` dict must be untouched
        self.assertEqual(site.model, "vendor/m")

    def test_no_warmup_skips_warmup_entirely(self):
        site = self._site()
        sites = {site.name: site}

        class NoWarmupRig(FakeCliRig):
            def bench_lease(self):
                raise AssertionError("warmup must be skipped with --no-warmup")

            def models(self):
                raise AssertionError("warmup must be skipped with --no-warmup")

        buf = io.StringIO()
        with mock.patch.object(tf, "Rig", lambda s: NoWarmupRig(s)):
            with contextlib.redirect_stdout(buf):
                rc = tf.cmd_text(_text_args(text="hi", no_warmup=True), sites)
        self.assertEqual(rc, 0)
        self.assertEqual(buf.getvalue().strip(), "(translated)")


class TestCmdTextExitCodes(unittest.TestCase):
    """The exit-code mapping `text` reuses from warmup()/cmd_run: rig leased
    (crucibleforge doctrine) = 6, model missing on rig = 5, rig unreachable =
    4, empty input = 2 (usage error), any other warmup/translation failure =
    1. Every case below is offline -- no live rig, HTTP layer mocked."""

    def _site(self):
        return make_site(model="vendor/m", languages=["ja"])

    def test_rig_leased_by_a_benchmark_exits_6(self):
        site = self._site()
        with mock.patch.object(tf, "Rig", lambda s: FakeCliRig(s, mode="leased")):
            with self.assertRaises(SystemExit) as cm:
                tf.cmd_text(_text_args(text="hi"), {site.name: site})
        self.assertEqual(cm.exception.code, 6)

    def test_model_missing_on_rig_exits_5(self):
        site = self._site()
        with mock.patch.object(tf, "Rig", lambda s: FakeCliRig(s, mode="missing")):
            with self.assertRaises(SystemExit) as cm:
                tf.cmd_text(_text_args(text="hi"), {site.name: site})
        self.assertEqual(cm.exception.code, 5)

    def test_rig_unreachable_returns_4(self):
        site = self._site()
        with mock.patch.object(tf, "Rig", lambda s: FakeCliRig(s, mode="unreachable")):
            rc = tf.cmd_text(_text_args(text="hi"), {site.name: site})
        self.assertEqual(rc, 4)

    def test_rig_http_error_during_warmup_returns_1(self):
        site = self._site()

        class HTTPBrokenRig(FakeCliRig):
            def models(self):
                # HTTPError registers a tempfile finalizer on its fp
                # regardless of what's passed; close() it here or the GC
                # prints an unrelated-looking ResourceWarning during
                # whatever later test happens to collect it.
                err = tf.urllib.error.HTTPError(
                    "http://rig.example/v1/models", 500, "boom", None,
                    io.BytesIO(b""))
                err.close()
                raise err

        with mock.patch.object(tf, "Rig", lambda s: HTTPBrokenRig(s)):
            rc = tf.cmd_text(_text_args(text="hi"), {site.name: site})
        self.assertEqual(rc, 1)

    def test_warmup_runtime_error_returns_1(self):
        site = self._site()

        class BrokenRig(FakeCliRig):
            def models(self):
                raise RuntimeError("boom")

        with mock.patch.object(tf, "Rig", lambda s: BrokenRig(s)):
            rc = tf.cmd_text(_text_args(text="hi"), {site.name: site})
        self.assertEqual(rc, 1)

    def test_translation_failure_returns_1(self):
        site = self._site()

        class EmptyReplyRig(FakeCliRig):
            def chat(self, messages, max_tokens, temperature):
                return {"choices": [{"message": {"content": ""},
                                     "finish_reason": "stop"}]}

        with mock.patch.object(tf, "Rig", lambda s: EmptyReplyRig(s)):
            rc = tf.cmd_text(_text_args(text="hi"), {site.name: site})
        self.assertEqual(rc, 1)

    def test_empty_positional_argument_exits_2(self):
        site = self._site()
        with self.assertRaises(SystemExit) as cm:
            tf.cmd_text(_text_args(text=""), {site.name: site})
        self.assertEqual(cm.exception.code, 2)

    def test_whitespace_only_stdin_exits_2(self):
        site = self._site()
        with mock.patch.object(tf.sys, "stdin", io.StringIO("   \n\t \n")):
            with self.assertRaises(SystemExit) as cm:
                tf.cmd_text(_text_args(text=None), {site.name: site})
        self.assertEqual(cm.exception.code, 2)

    def test_completely_empty_stdin_exits_2(self):
        site = self._site()
        with mock.patch.object(tf.sys, "stdin", io.StringIO("")):
            with self.assertRaises(SystemExit) as cm:
                tf.cmd_text(_text_args(text=None), {site.name: site})
        self.assertEqual(cm.exception.code, 2)


class TestTextCliWiring(unittest.TestCase):
    """The argparse wiring in main(): --help exits before load_config() is
    ever reached, so this needs no ~/.config/transforge/config.toml."""

    def test_help_says_plain_text_only_no_structure_rules(self):
        with mock.patch.object(tf.sys, "argv", ["transforge", "text", "--help"]):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                with self.assertRaises(SystemExit) as cm:
                    tf.main()
        self.assertEqual(cm.exception.code, 0)
        help_text = buf.getvalue()
        self.assertIn("PLAIN TEXT", help_text)
        self.assertIn("--to", help_text)
        self.assertIn("--from", help_text)
        self.assertIn("--json", help_text)

    def test_to_is_required(self):
        with mock.patch.object(tf.sys, "argv", ["transforge", "text", "hi"]):
            with self.assertRaises(SystemExit) as cm:
                tf.main()
        self.assertEqual(cm.exception.code, 2)

    def test_top_level_help_lists_text_subcommand(self):
        with mock.patch.object(tf.sys, "argv", ["transforge", "--help"]):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                with self.assertRaises(SystemExit) as cm:
                    tf.main()
        self.assertEqual(cm.exception.code, 0)
        self.assertIn("text", buf.getvalue())


# ------------------------------------------------- --files resolution (D3)
class TestResolveFiles(unittest.TestCase):
    """A --files spec that matches no source page is a usage error, never an
    empty selection reported as '0 job(s)' / 'all requested siblings are
    current'. Regression guard for the 2026-09-18 publish, where the page
    still carried the scaffold's `translate: false` and every command said
    'nothing to do'."""

    def setUp(self):
        self.d = tempfile.TemporaryDirectory()
        self.addCleanup(self.d.cleanup)
        self.root = self.d.name
        write(os.path.join(self.root, "content/projects/foo.md"), "---\ntitle: Foo\n---\n\nbody\n")
        write(os.path.join(self.root, "content/projects/bar.md"), "---\ntitle: Bar\n---\n\nbody\n")
        write(os.path.join(self.root, "content/pages/foo.md"), "---\ntitle: Page\n---\n\nbody\n")
        self.site = make_site(root=self.root, content_dirs=["content/projects"],
                              languages=["ja"], lang_names={"ja": "Japanese"})

    def test_none_when_no_files_given(self):
        self.assertIsNone(tf.resolve_files(self.site, None))
        self.assertIsNone(tf.resolve_files(self.site, []))

    def test_relative_path(self):
        self.assertEqual(tf.resolve_files(self.site, ["content/projects/foo.md"]),
                         ["content/projects/foo.md"])

    def test_absolute_path(self):
        p = os.path.join(self.root, "content/projects/foo.md")
        self.assertEqual(tf.resolve_files(self.site, [p]), ["content/projects/foo.md"])

    def test_bare_slug(self):
        self.assertEqual(tf.resolve_files(self.site, ["foo"]), ["content/projects/foo.md"])

    def test_bare_filename(self):
        self.assertEqual(tf.resolve_files(self.site, ["foo.md"]), ["content/projects/foo.md"])

    def test_duplicates_collapse(self):
        self.assertEqual(
            tf.resolve_files(self.site, ["foo", "content/projects/foo.md"]),
            ["content/projects/foo.md"])

    def test_unmatched_exits_2(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err), self.assertRaises(SystemExit) as cm:
            tf.resolve_files(self.site, ["nope"])
        self.assertEqual(cm.exception.code, 2)
        self.assertIn("no source page matched --files", err.getvalue())

    def test_unmatched_reports_every_bad_spec(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err), self.assertRaises(SystemExit):
            tf.resolve_files(self.site, ["foo", "nope", "alsonope"])
        self.assertIn("nope", err.getvalue())
        self.assertIn("alsonope", err.getvalue())

    def test_opted_out_page_names_the_offending_key(self):
        """The page exists but discovery skips it: say WHICH key, because that
        is the actual fix and the silent version shipped an untranslated post."""
        write(os.path.join(self.root, "content/projects/skipme.md"),
              "---\ntitle: Skip\ntranslate: false\n---\n\nbody\n")
        err = io.StringIO()
        with contextlib.redirect_stderr(err), self.assertRaises(SystemExit):
            tf.resolve_files(self.site, ["skipme"])
        msg = err.getvalue()
        self.assertIn("translate: false", msg)
        self.assertIn("opts out of translation", msg)

    def test_en_only_page_names_the_offending_key(self):
        write(os.path.join(self.root, "content/projects/tpl.md"),
              "---\ntitle: Tpl\nen_only: true\n---\n\nbody\n")
        err = io.StringIO()
        with contextlib.redirect_stderr(err), self.assertRaises(SystemExit):
            tf.resolve_files(self.site, ["tpl"])
        self.assertIn("en_only: true", err.getvalue())

    def test_ambiguous_slug_exits_2(self):
        site = make_site(root=self.root,
                         content_dirs=["content/projects", "content/pages"],
                         languages=["ja"], lang_names={"ja": "Japanese"})
        err = io.StringIO()
        with contextlib.redirect_stderr(err), self.assertRaises(SystemExit) as cm:
            tf.resolve_files(site, ["foo"])
        self.assertEqual(cm.exception.code, 2)
        self.assertIn("ambiguous", err.getvalue())

    def test_ambiguous_slug_resolvable_by_full_path(self):
        site = make_site(root=self.root,
                         content_dirs=["content/projects", "content/pages"],
                         languages=["ja"], lang_names={"ja": "Japanese"})
        self.assertEqual(tf.resolve_files(site, ["content/pages/foo.md"]),
                         ["content/pages/foo.md"])

    def test_plan_accepts_files_and_scopes_the_plan(self):
        """`plan --files` must actually filter, not accept-and-ignore."""
        with tempfile.TemporaryDirectory() as state:
            orig = tf.STATE_DIR
            tf.STATE_DIR = state
            self.addCleanup(lambda: setattr(tf, "STATE_DIR", orig))
            args = argparse.Namespace(site="t", langs=None,
                                      files=["foo"], force=False)
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                rc = tf.cmd_plan(args, {"t": self.site})
            self.assertEqual(rc, 0)
            self.assertIn("content/projects/foo.md", out.getvalue())
            self.assertNotIn("content/projects/bar.md", out.getvalue())
            self.assertIn("1 job(s)", out.getvalue())

    def test_plan_with_unmatched_files_exits_2_not_zero_jobs(self):
        with tempfile.TemporaryDirectory() as state:
            orig = tf.STATE_DIR
            tf.STATE_DIR = state
            self.addCleanup(lambda: setattr(tf, "STATE_DIR", orig))
            args = argparse.Namespace(site="t", langs=None,
                                      files=["not-a-page"], force=False)
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), \
                    self.assertRaises(SystemExit) as cm:
                tf.cmd_plan(args, {"t": self.site})
            self.assertEqual(cm.exception.code, 2)
            self.assertNotIn("0 job(s)", out.getvalue())


# ------------------------------------------ glossary-bleed recovery (D4)
class ScriptedTranslator:
    """Stands in for Translator: replays a scripted (out_text, problems) per
    construction, and records the site knobs and excluded terms it was built
    with, so a test can assert the recovery ladder's shape."""

    def __init__(self, site, script, seen):
        self.site = site
        self.script = script
        self.seen = seen
        self.exclude_terms = ()

    def translate_document_checked(self, source_text, lang):
        step = self.script.pop(0)
        self.seen.append({"model": self.site.model,
                          "prompt_template": self.site.prompt_template,
                          "temperature": self.site.temperature,
                          "extra_params": dict(self.site.extra_params or {}),
                          "exclude_terms": tuple(self.exclude_terms)})
        return step


def _bleed_site(**over):
    kw = dict(link_dirs=[], languages=["zh"], lang_names={"zh": "Chinese"},
              no_translate=["DeepSeek", "OpenClaw"], temperature=0.7)
    kw.update(over)
    return make_site(**kw)


def _run_recovery(site, script):
    seen = []
    it = iter(script)

    def factory(s):
        return ScriptedTranslator(s, [next(it)], seen)

    out, via = tf.translate_with_recovery(site, None, SRC_NO_DEEPSEEK, "zh",
                                          make_translator=factory)
    return out, via, seen


SRC_NO_DEEPSEEK = "<p>A page that never mentions the vendor.</p>"
BLED = "<p>DeepSeek 的页面。</p>"
CLEAN = "<p>一个页面。</p>"
BLEED_PROBLEM = tf.GLOSSARY_PROBLEM_PREFIX + "DeepSeek (0 -> 1)"


class TestTranslateWithRecovery(unittest.TestCase):
    def test_clean_first_pass_reports_no_recovery(self):
        out, via, seen = _run_recovery(_bleed_site(), [(CLEAN, [])])
        self.assertEqual(out, CLEAN)
        self.assertIsNone(via)
        self.assertEqual(len(seen), 1)

    def test_bleed_then_clean_is_recovered_by_glossary_strip(self):
        out, via, seen = _run_recovery(
            _bleed_site(), [(BLED, [BLEED_PROBLEM]), (CLEAN, [])])
        self.assertEqual(out, CLEAN)
        self.assertEqual(via, "glossary-strip")
        self.assertEqual(len(seen), 2)

    def test_retry_drops_the_injected_term_from_the_prompt(self):
        _, _, seen = _run_recovery(
            _bleed_site(), [(BLED, [BLEED_PROBLEM]), (CLEAN, [])])
        self.assertEqual(seen[0]["exclude_terms"], ())
        self.assertIn("DeepSeek", seen[1]["exclude_terms"])
        self.assertNotIn("OpenClaw", seen[1]["exclude_terms"],
                         "only the term that actually bled is dropped")

    def test_a_term_used_by_the_source_is_never_dropped(self):
        """Dropping a term the source really uses would let the model translate
        a brand name away — worse than the bleed it is fixing."""
        site = _bleed_site()
        seen = []
        script = [("<p>DeepSeek DeepSeek</p>", [BLEED_PROBLEM]), (CLEAN, [])]
        it = iter(script)
        tf.translate_with_recovery(
            site, None, "<p>DeepSeek is here.</p>", "zh",
            make_translator=lambda s: ScriptedTranslator(s, [next(it)], seen))
        self.assertEqual(seen[1]["exclude_terms"], ())

    def test_retry_samples_down_and_reseeds(self):
        _, _, seen = _run_recovery(
            _bleed_site(), [(BLED, [BLEED_PROBLEM]), (CLEAN, [])])
        self.assertEqual(seen[0]["temperature"], 0.7)
        self.assertLessEqual(seen[1]["temperature"], 0.2)
        self.assertIn("seed", seen[1]["extra_params"])

    def test_retry_budget_is_honoured_then_fallback_model(self):
        site = _bleed_site(retry_on_verify_fail=2,
                           fallback_model="unsloth/Qwen3.8-27B-GGUF/Qwen3.8-27B-Q5_K_S",
                           fallback_prompt_template="instruct")
        out, via, seen = _run_recovery(site, [
            (BLED, [BLEED_PROBLEM]), (BLED, [BLEED_PROBLEM]),
            (BLED, [BLEED_PROBLEM]), (CLEAN, [])])
        self.assertEqual(via, "fallback-model")
        self.assertEqual(len(seen), 4, "1 first pass + 2 retries + 1 fallback")
        self.assertEqual(seen[3]["model"], site.fallback_model)
        self.assertEqual(seen[3]["prompt_template"], "instruct")

    def test_exhausted_ladder_raises(self):
        site = _bleed_site(retry_on_verify_fail=1, fallback_model="")
        with self.assertRaises(RuntimeError) as cm:
            _run_recovery(site, [(BLED, [BLEED_PROBLEM]), (BLED, [BLEED_PROBLEM])])
        self.assertIn("after recovery", str(cm.exception))

    def test_zero_retries_disables_the_ladder(self):
        site = _bleed_site(retry_on_verify_fail=0, fallback_model="")
        with self.assertRaises(RuntimeError):
            _run_recovery(site, [(BLED, [BLEED_PROBLEM])])

    def test_non_glossary_failure_is_not_retried(self):
        """A code-span or link-prefix delta is deterministic: retrying it burns
        rig time and cannot succeed."""
        site = _bleed_site()
        with self.assertRaises(RuntimeError) as cm:
            _run_recovery(site, [(CLEAN, ["inline code: 3 -> 5"])])
        self.assertIn("inline code", str(cm.exception))
        self.assertNotIn("after recovery", str(cm.exception))

    def test_recovery_never_mutates_the_configured_site(self):
        site = _bleed_site()
        _run_recovery(site, [(BLED, [BLEED_PROBLEM]), (CLEAN, [])])
        self.assertEqual(site.temperature, 0.7)
        self.assertEqual(site.no_translate, ["DeepSeek", "OpenClaw"])
        self.assertNotIn("seed", site.extra_params)

    def test_glossary_problems_filter(self):
        self.assertEqual(tf.glossary_problems([BLEED_PROBLEM, "links: 1 -> 2"]),
                         [BLEED_PROBLEM])


class TestExcludeTermsInPrompt(unittest.TestCase):
    """The exclude list must actually reach the prompt builder."""

    def test_excluded_term_is_not_offered_to_the_model(self):
        site = _bleed_site()
        tr = tf.Translator(site, None)
        text = "<p>DeepSeek and OpenClaw.</p>"
        self.assertIn("DeepSeek", tr._terms_for(text))
        tr.exclude_terms = ("DeepSeek",)
        self.assertNotIn("DeepSeek", tr._terms_for(text))
        self.assertIn("OpenClaw", tr._terms_for(text))

    def test_default_exclude_is_empty(self):
        self.assertEqual(tf.Translator(_bleed_site(), None).exclude_terms, ())


class TestPinSource(unittest.TestCase):
    """The management PIN comes from $STUDIOFORGE_MCP_PIN or from the file
    named by $TRANSFORGE_PIN_ENV_FILE -- never from a guessed location."""

    def _rig(self):
        site = mock.Mock(endpoint="http://127.0.0.1:1/")
        return tf.Rig(site)

    def test_env_var_wins(self):
        with mock.patch.dict(os.environ, {"STUDIOFORGE_MCP_PIN": "11112222"}), \
             mock.patch.object(tf, "PIN_ENV_FILE", "/nonexistent"):
            self.assertEqual(self._rig().pin(), "11112222")

    def test_no_file_configured_reads_nothing(self):
        env = {k: v for k, v in os.environ.items() if k != "STUDIOFORGE_MCP_PIN"}
        with mock.patch.dict(os.environ, env, clear=True), \
             mock.patch.object(tf, "PIN_ENV_FILE", ""), \
             mock.patch.object(tf, "read_text") as rt:
            self.assertEqual(self._rig().pin(), "")
            rt.assert_not_called()

    def test_opt_in_file_is_read(self):
        env = {k: v for k, v in os.environ.items() if k != "STUDIOFORGE_MCP_PIN"}
        with tempfile.TemporaryDirectory() as d:
            f = os.path.join(d, "pin.env")
            with open(f, "w", encoding="utf-8") as fh:
                fh.write('OTHER=1\nSTUDIOFORGE_MCP_PIN="33334444"\n')
            with mock.patch.dict(os.environ, env, clear=True), \
                 mock.patch.object(tf, "PIN_ENV_FILE", f):
                self.assertEqual(self._rig().pin(), "33334444")

    def test_module_default_is_opt_in(self):
        env = {k: v for k, v in os.environ.items() if k != "TRANSFORGE_PIN_ENV_FILE"}
        with mock.patch.dict(os.environ, env, clear=True):
            spec = importlib.util.spec_from_file_location("tf_fresh", tf.__file__)
            fresh = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(fresh)
        self.assertEqual(fresh.PIN_ENV_FILE, "")
