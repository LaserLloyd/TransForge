"""Offline tests for review.py — the API client is always mocked, no network.

Run from the repo root:  python3 -m unittest discover -s tests
"""
import importlib.util
import json
import os
import sys
import tempfile
import types
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

_spec = importlib.util.spec_from_file_location(
    "transforge", os.path.join(REPO, "transforge.py"))
tf = importlib.util.module_from_spec(_spec)
sys.modules["transforge"] = tf
_spec.loader.exec_module(tf)

_rspec = importlib.util.spec_from_file_location(
    "review_mod", os.path.join(REPO, "review.py"))
rv = importlib.util.module_from_spec(_rspec)
sys.modules["review_mod"] = rv
_rspec.loader.exec_module(rv)


SRC_DOC = """---
title: "Widget: A Thing"
excerpt: "Short and sharp."
plain: >-
  A friendly explanation with no query log anywhere.
you_setup:
  - "One machine"
  - "Two disks"
---

Intro paragraph naming the article it read that in.

<div class="callout">
<p><strong>tl;dr</strong></p>
<ul>
<li>Point one.</li>
</ul>
</div>

## Deep dive

Body text that is never reviewed.
"""

JA_DOC = """---
title: ウィジェット
excerpt: 短い。
plain: クエリログのある説明。
you_setup:
- 一台のマシン
- 二台のディスク
---

冒頭の段落。

<div class="callout">
<p><strong>要約</strong></p>
<ul>
<li>第一のポイント。</li>
</ul>
</div>

## Deep dive

決してレビューされない本文。
"""


class FakeClient:
    """Stands in for FlashClient; returns a canned reply, counts calls."""
    instances = []

    def __init__(self, rc):
        self.rc = rc
        self.reply = getattr(FakeClient, "next_reply", "{}")
        self.calls = 0
        self.usage = {"prompt_tokens": 1, "completion_tokens": 1, "calls": 0,
                      "cache_hit_tokens": 0, "cache_miss_tokens": 0}
        FakeClient.instances.append(self)

    def chat(self, system, user):
        self.calls += 1
        self.usage["calls"] += 1
        FakeClient.last_user = user
        return self.reply


class ExplodingClient:
    def __init__(self, rc):
        raise AssertionError("client constructed although review is disabled")


def ns(**kw):
    base = dict(site=None, langs=None, files=None, dry_run=False,
                include_edited=False, limit=None)
    base.update(kw)
    return types.SimpleNamespace(**base)


class ReviewHarness(unittest.TestCase):
    """Temp site + config + state; each test gets a clean world."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = self.tmp.name
        self.root = root
        os.makedirs(os.path.join(root, "content"))
        self.src_rel = os.path.join("content", "x.md")
        self.out_rel = os.path.join("content", "x.ja.md")
        self.write(self.src_rel, SRC_DOC)
        self.write(self.out_rel, JA_DOC)

        self.site = tf.SiteConfig("t", {}, {
            "root": root, "content_dirs": ["content"], "languages": ["ja"],
            "no_translate": ["OpenClaw"], "translate_fields": [],
        })
        self.sites = {"t": self.site}

        self._old_state = tf.STATE_DIR
        tf.STATE_DIR = os.path.join(root, "state")
        self._old_cfgpath = tf.CONFIG_PATH
        tf.CONFIG_PATH = os.path.join(root, "config.toml")
        self.write_config(enabled=True)

        self._old_client = rv.FlashClient
        rv.FlashClient = FakeClient
        FakeClient.instances = []
        FakeClient.next_reply = "{}"
        os.environ["TEST_REVIEW_KEY"] = "k"

        # track the sibling so it classifies CURRENT
        m = tf.Manifest("t")
        m.set(self.src_rel, "ja", {
            "src_sha": tf.sha256_text(SRC_DOC),
            "out_sha": tf.sha256_text(JA_DOC),
            "cfg": self.site.cfg_hash(), "model": "local", "out": self.out_rel,
            "translated_at": tf.now_iso(), "duration_s": 0})
        m.save()

    def tearDown(self):
        tf.STATE_DIR = self._old_state
        tf.CONFIG_PATH = self._old_cfgpath
        rv.FlashClient = self._old_client
        self.tmp.cleanup()

    def write(self, rel, text):
        p = os.path.join(self.root, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            f.write(text)

    def read(self, rel):
        with open(os.path.join(self.root, rel), encoding="utf-8") as f:
            return f.read()

    def write_config(self, enabled=True, **extra):
        lines = ["[review]", f"enabled = {'true' if enabled else 'false'}",
                 'base_url = "https://reviewer.invalid"',
                 'model = "test-flash"',
                 'api_key_env = "TEST_REVIEW_KEY"']
        for k, v in extra.items():
            lines.append(f"{k} = {json.dumps(v)}")
        with open(tf.CONFIG_PATH, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")

    # ---- helpers over the module under test
    def run_review(self, **kw):
        return rv.cmd_review(ns(**kw), self.sites)


class TestExtraction(unittest.TestCase):
    def test_intro_span_through_callout(self):
        _, body = tf.split_frontmatter(SRC_DOC)
        a, b = rv.intro_span(body)
        seg = body[a:b]
        self.assertIn("Intro paragraph", seg)
        self.assertTrue(seg.rstrip().endswith("</div>"))
        self.assertNotIn("Deep dive", seg)

    def test_intro_span_nested_divs(self):
        body = ('lead\n<div class="callout"><div class="inner">x</div>'
                "tail</div>\n\n## H\nrest")
        a, b = rv.intro_span(body)
        self.assertTrue(body[a:b].endswith("tail</div>"))

    def test_intro_span_no_callout_falls_back_to_heading(self):
        body = "just an intro\n\n## First\nrest"
        a, b = rv.intro_span(body)
        self.assertEqual(body[a:b].strip(), "just an intro")

    def test_intro_span_unclosed_callout_fails_closed(self):
        self.assertEqual(rv.intro_span('<div class="callout">never closed'),
                         (0, 0))

    def test_intro_span_nothing(self):
        self.assertEqual(rv.intro_span("plain text only"), (0, 0))

    def test_extract_sections_roundtrip(self):
        parts, fm, body = rv.extract_sections(
            SRC_DOC, rv.REVIEW_DEFAULT_SECTIONS)
        self.assertEqual(parts["title"], "Widget: A Thing")
        self.assertEqual(parts["you_setup"], ["One machine", "Two disks"])
        self.assertIn("Intro paragraph", parts["intro_through_tldr"])
        self.assertNotIn("llm_does", parts)      # absent field not invented

    def test_apply_sections_splices_body_and_fm(self):
        fixed = {"plain": "NEW PLAIN", "intro_through_tldr": "NEW INTRO\n"}
        out = rv.apply_sections(JA_DOC, fixed)
        self.assertIn("NEW PLAIN", out)
        self.assertIn("NEW INTRO", out)
        self.assertIn("決してレビューされない本文", out)   # untouched body kept
        self.assertIn("ウィジェット", out)                # untouched fm kept
        self.assertNotIn("冒頭の段落", out)               # old intro replaced


class TestFlashClientOverflow(unittest.TestCase):
    """A reasoning model can spend the whole budget thinking and answer with
    an empty string + finish_reason=length. That must be recovered, not
    mistaken for a refusal."""

    def client(self, replies, **cfg):
        rc = rv.ReviewConfig({**rv.REVIEW_DEFAULTS, "base_url": "https://x.invalid",
                              "model": "m", "api_key_env": "TEST_REVIEW_KEY",
                              **cfg})
        c = rv.FlashClient(rc)
        self.posts = []

        def fake_post(system, user, max_tokens, reasoning_effort):
            self.posts.append((max_tokens, reasoning_effort))
            return replies[len(self.posts) - 1]
        c._post = fake_post
        return c

    def test_overflow_retried_without_reasoning_and_bigger_budget(self):
        c = self.client([("", "length"), ('{"ok":1}', "stop")], max_tokens=100)
        self.assertEqual(c.chat("s", "u"), '{"ok":1}')
        self.assertEqual(self.posts, [(100, ""), (200, "none")])

    def test_persistent_overflow_raises_a_named_error(self):
        c = self.client([("", "length"), ("", "length")], max_tokens=100)
        with self.assertRaises(RuntimeError) as e:
            c.chat("s", "u")
        self.assertIn("reasoning", str(e.exception))

    def test_good_first_reply_makes_one_call_only(self):
        c = self.client([('{"ok":1}', "stop")], reasoning_effort="none")
        self.assertEqual(c.chat("s", "u"), '{"ok":1}')
        self.assertEqual(len(self.posts), 1)
        self.assertEqual(self.posts[0][1], "none")

    def test_empty_reply_that_is_not_an_overflow_is_not_retried(self):
        c = self.client([("", "stop")])
        self.assertEqual(c.chat("s", "u"), "")
        self.assertEqual(len(self.posts), 1)


class TestFrontmatterSplice(unittest.TestCase):
    """Untouched frontmatter must survive byte-for-byte — re-dumping the whole
    mapping through yaml.safe_dump rewrote 242 of 252 live siblings."""

    QUOTED = ('---\n'
              'title: "Widget: A Thing"\n'
              'date: 2024-12-19\n'
              'keywords: ["a b", "c d"]\n'
              'plain: "old plain"\n'
              'toc: true\n'
              '---\n'
              '\nbody\n')

    def test_noop_apply_is_identity(self):
        self.assertEqual(rv.apply_sections(self.QUOTED, {}), self.QUOTED)
        self.assertEqual(rv.apply_sections(JA_DOC, {}), JA_DOC)
        self.assertEqual(rv.apply_sections(SRC_DOC, {}), SRC_DOC)

    def test_only_the_named_field_is_rewritten(self):
        out = rv.apply_sections(self.QUOTED, {"plain": "new plain"})
        self.assertIn("plain: new plain", out)
        self.assertNotIn("old plain", out)
        # every other line untouched, quoting and flow style included
        self.assertIn('title: "Widget: A Thing"', out)
        self.assertIn('keywords: ["a b", "c d"]', out)
        self.assertIn("date: 2024-12-19", out)
        self.assertIn("toc: true", out)

    def test_multiline_block_field_replaced_whole(self):
        doc = ("---\nplain: >-\n  line one\n  line two\ntoc: true\n---\n\nbody\n")
        out = rv.apply_sections(doc, {"plain": "single line"})
        self.assertNotIn("line one", out)
        self.assertIn("toc: true", out)

    def test_list_field_replaced_in_place(self):
        doc = ("---\ntitle: T\nyou_setup:\n  - \"a\"\n  - \"b\"\nzz: keep\n---\n\nb\n")
        out = rv.apply_sections(doc, {"you_setup": ["x", "y"]})
        self.assertIn("- x", out)
        self.assertIn("- y", out)
        self.assertNotIn('- "a"', out)
        self.assertIn("title: T", out)
        self.assertIn("zz: keep", out)

    def test_absent_field_is_appended_not_dropped(self):
        out = rv.apply_sections("---\ntitle: T\n---\n\nb\n", {"excerpt": "E"})
        fm, _ = tf.split_frontmatter(out)
        self.assertIn("title: T", fm)
        self.assertIn("excerpt: E", fm)

    def test_document_without_frontmatter_gets_none_invented(self):
        doc = 'lead\n\n<div class="callout">x</div>\n\n## H\ntail\n'
        out = rv.apply_sections(doc, {"intro_through_tldr": "NEW"})
        self.assertFalse(out.startswith("---"))
        self.assertIn("NEW", out)
        self.assertIn("tail", out)


class TestValidateReply(unittest.TestCase):
    ORIG = {"title": "t", "you_setup": ["a", "b"], "intro_through_tldr": "i"}

    def test_change_detection(self):
        fixed, changed, kept = rv.validate_reply(
            self.ORIG, {"title": "T2", "you_setup": ["a", "b"],
                        "intro_through_tldr": "i"})
        self.assertEqual(fixed["title"], "T2")
        self.assertEqual(changed, ["title"])
        self.assertEqual(kept, [])

    def test_missing_key_keeps_original(self):
        fixed, changed, kept = rv.validate_reply(self.ORIG, {"title": "T2"})
        self.assertEqual(fixed["you_setup"], ["a", "b"])
        self.assertIn("you_setup", kept)
        self.assertIn("intro_through_tldr", kept)

    def test_list_count_mismatch_keeps_original(self):
        fixed, _, kept = rv.validate_reply(
            self.ORIG, {"you_setup": ["only one"]})
        self.assertEqual(fixed["you_setup"], ["a", "b"])
        self.assertIn("you_setup", kept)

    def test_type_mismatch_and_empty_keep_original(self):
        fixed, _, kept = rv.validate_reply(
            self.ORIG, {"title": ["not", "a", "string"],
                        "intro_through_tldr": "   "})
        self.assertEqual(fixed["title"], "t")
        self.assertEqual(fixed["intro_through_tldr"], "i")
        self.assertEqual(sorted(kept),
                         ["intro_through_tldr", "title", "you_setup"])


class TestCmdReview(ReviewHarness):
    def test_disabled_makes_zero_api_calls(self):
        self.write_config(enabled=False)
        rv.FlashClient = ExplodingClient
        self.assertEqual(self.run_review(), 0)

    def test_missing_key_env_exits_7(self):
        del os.environ["TEST_REVIEW_KEY"]
        try:
            self.assertEqual(self.run_review(), rv.EXIT_NO_KEY)
        finally:
            os.environ["TEST_REVIEW_KEY"] = "k"

    def test_misconfigured_without_endpoint_exits_2(self):
        with open(tf.CONFIG_PATH, "w") as f:
            f.write("[review]\nenabled = true\n")
        self.assertEqual(self.run_review(), 2)

    def test_correction_applied_and_manifest_updated(self):
        FakeClient.next_reply = json.dumps(
            {"title": "ウィジェット改", "excerpt": "短い。",
             "plain": "クエリログの「ない」説明。",
             "you_setup": ["一台のマシン", "二台のディスク"],
             "intro_through_tldr":
             "冒頭の段落 — 読んだ記事名も示した。\n\n"
             '<div class="callout">\n<p><strong>要約</strong></p>\n<ul>\n'
             "<li>第一のポイント。</li>\n</ul>\n</div>"},
            ensure_ascii=False)
        self.assertEqual(self.run_review(), 0)
        out = self.read(self.out_rel)
        self.assertIn("ウィジェット改", out)
        self.assertIn("読んだ記事名も示した", out)
        self.assertIn("決してレビューされない本文", out)
        entry = tf.Manifest("t").get(self.src_rel, "ja")
        self.assertEqual(entry["out_sha"], tf.sha256_text(out))
        self.assertIn("title", entry["review"]["changed"])
        # still CURRENT, not EDITED: review output is pipeline output
        state, _ = tf.classify(self.site, tf.Manifest("t"), self.src_rel, "ja")
        self.assertEqual(state, "CURRENT")

    def test_idempotent_second_run_skips(self):
        FakeClient.next_reply = json.dumps(
            {"title": "ウィジェット", "excerpt": "短い。",
             "plain": "クエリログのある説明。",
             "you_setup": ["一台のマシン", "二台のディスク"],
             "intro_through_tldr": rv.extract_sections(
                 JA_DOC, rv.REVIEW_DEFAULT_SECTIONS)[0]["intro_through_tldr"]},
            ensure_ascii=False)
        self.assertEqual(self.run_review(), 0)     # clean review, recorded
        n_calls = sum(c.calls for c in FakeClient.instances)
        self.assertEqual(n_calls, 1)
        self.assertEqual(self.run_review(), 0)     # hash match: no new call
        self.assertEqual(sum(c.calls for c in FakeClient.instances), n_calls)

    def test_unparseable_reply_keeps_original_and_fails(self):
        FakeClient.next_reply = "sorry, here you go: broken"
        before = self.read(self.out_rel)
        self.assertEqual(self.run_review(), 1)
        self.assertEqual(self.read(self.out_rel), before)

    def test_glossary_injection_rejected(self):
        parts, _, _ = rv.extract_sections(JA_DOC, rv.REVIEW_DEFAULT_SECTIONS)
        reply = dict(parts)
        reply["plain"] = "OpenClaw が入った説明。"      # term absent from source
        FakeClient.next_reply = json.dumps(reply, ensure_ascii=False, default=str)
        before = self.read(self.out_rel)
        self.assertEqual(self.run_review(), 1)
        self.assertEqual(self.read(self.out_rel), before)

    def test_structural_damage_rejected(self):
        parts, _, _ = rv.extract_sections(JA_DOC, rv.REVIEW_DEFAULT_SECTIONS)
        reply = dict(parts)
        reply["intro_through_tldr"] = "コールアウトを失った紹介文。"  # dropped div
        FakeClient.next_reply = json.dumps(reply, ensure_ascii=False, default=str)
        before = self.read(self.out_rel)
        self.assertEqual(self.run_review(), 1)
        self.assertEqual(self.read(self.out_rel), before)

    def test_edited_skipped_by_default(self):
        self.write(self.out_rel, JA_DOC.replace("本文。", "本文（手直し）。"))
        FakeClient.next_reply = "{}"
        self.assertEqual(self.run_review(), 0)
        self.assertEqual(sum(c.calls for c in FakeClient.instances), 0)

    def test_edited_no_backup_refused_even_with_flag(self):
        self.write(self.out_rel, JA_DOC.replace("本文。", "本文（手直し）。"))
        self.assertEqual(self.run_review(include_edited=True), 0)
        self.assertEqual(sum(c.calls for c in FakeClient.instances), 0)

    def test_edited_outside_sections_reviewed_with_flag(self):
        # backup = the pipeline output; hand-edit only in the deep body
        bak = os.path.join(tf.STATE_DIR, "backups", "t", "20200101-000000",
                           self.out_rel)
        os.makedirs(os.path.dirname(bak), exist_ok=True)
        with open(bak, "w", encoding="utf-8") as f:
            f.write(JA_DOC)
        self.write(self.out_rel, JA_DOC.replace("本文。", "本文（手直し）。"))
        parts, _, _ = rv.extract_sections(JA_DOC, rv.REVIEW_DEFAULT_SECTIONS)
        reply = dict(parts)
        reply["title"] = "ウィジェット改"
        FakeClient.next_reply = json.dumps(reply, ensure_ascii=False, default=str)
        self.assertEqual(self.run_review(include_edited=True), 0)
        out = self.read(self.out_rel)
        self.assertIn("ウィジェット改", out)
        self.assertIn("手直し", out)               # hand-edit survives

    def test_edited_inside_sections_refused(self):
        bak = os.path.join(tf.STATE_DIR, "backups", "t", "20200101-000000",
                           self.out_rel)
        os.makedirs(os.path.dirname(bak), exist_ok=True)
        with open(bak, "w", encoding="utf-8") as f:
            f.write(JA_DOC)
        self.write(self.out_rel, JA_DOC.replace("冒頭の段落。", "冒頭の段落（手直し）。"))
        self.assertEqual(self.run_review(include_edited=True), 0)
        self.assertEqual(sum(c.calls for c in FakeClient.instances), 0)

    def test_dry_run_makes_no_calls_and_no_writes(self):
        before = self.read(self.out_rel)
        self.assertEqual(self.run_review(dry_run=True), 0)
        self.assertEqual(self.read(self.out_rel), before)
        self.assertEqual(sum(c.calls for c in FakeClient.instances), 0)

    def test_unknown_file_argument_is_an_error_not_silence(self):
        self.assertEqual(self.run_review(files=["content/nope.md"]), 2)
        self.assertEqual(sum(c.calls for c in FakeClient.instances), 0)

    def test_limit_caps_dry_run_too(self):
        self.write(os.path.join("content", "y.md"), SRC_DOC)
        self.write(os.path.join("content", "y.ja.md"), JA_DOC)
        m = tf.Manifest("t")
        m.set(os.path.join("content", "y.md"), "ja", {
            "src_sha": tf.sha256_text(SRC_DOC),
            "out_sha": tf.sha256_text(JA_DOC),
            "cfg": self.site.cfg_hash(), "model": "local",
            "out": os.path.join("content", "y.ja.md"),
            "translated_at": tf.now_iso(), "duration_s": 0})
        m.save()
        self.assertEqual(self.run_review(dry_run=True, limit=1), 0)

    def test_preexisting_verify_problem_does_not_block_a_clean_review(self):
        # sibling already violates a structural check (extra link) before we
        # touch it; the reviewer's own correction must still be applied
        broken = JA_DOC.replace("冒頭の段落。",
                                '冒頭の段落。<a href="/x/">x</a>')
        self.write(self.out_rel, broken)
        m = tf.Manifest("t")
        e = m.get(self.src_rel, "ja")
        e["out_sha"] = tf.sha256_text(broken)
        m.set(self.src_rel, "ja", e)
        m.save()
        parts, _, _ = rv.extract_sections(broken, rv.REVIEW_DEFAULT_SECTIONS)
        reply = dict(parts)
        reply["title"] = "ウィジェット改"
        FakeClient.next_reply = json.dumps(reply, ensure_ascii=False,
                                           default=str)
        self.assertEqual(self.run_review(), 0)
        self.assertIn("ウィジェット改", self.read(self.out_rel))

    def test_stale_sibling_skipped(self):
        self.write(self.src_rel, SRC_DOC + "\nNew EN sentence.\n")
        FakeClient.next_reply = "{}"
        self.assertEqual(self.run_review(), 0)
        self.assertEqual(sum(c.calls for c in FakeClient.instances), 0)


if __name__ == "__main__":
    unittest.main()
