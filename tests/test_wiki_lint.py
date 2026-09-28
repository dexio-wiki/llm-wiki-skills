"""Tests for skills/wiki-lint/scripts/wiki_lint.py. Standard library only:

    python3 -m unittest discover -s tests
"""
import importlib.util
import io
import json
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPT = HERE.parent / "skills" / "wiki-lint" / "scripts" / "wiki_lint.py"
spec = importlib.util.spec_from_file_location("wiki_lint", SCRIPT)
wl = importlib.util.module_from_spec(spec)
spec.loader.exec_module(wl)

FM = """---
title: {title}
description: {desc}
updated: {updated}
---

"""


def page(title, body, desc="A page.", updated="2026-09-01"):
    return FM.format(title=title, desc=desc, updated=updated) + body


class WikiCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, rel, text):
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")

    def report(self, now=None, **kw):
        wiki = wl.Wiki(self.root)
        return wl.check(wiki, kw.get("stale_days", 90), kw.get("max_lines", 200),
                        now or time.time())

    def run_main(self, *args):
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = wl.main([str(self.root), *args])
        return code, buf.getvalue()


class TestLinks(WikiCase):
    def test_resolution_exact_relative_and_name(self):
        self.write("concepts/a.md", page("A", "[[concepts/b]] [[b]] [[./b]] [see](b.md) [[c|C]]"))
        self.write("concepts/b.md", page("B", "[[a#Heading]] [up](../SCHEMA.md)"))
        self.write("other/c.md", page("C", "[[concepts/a]]"))
        self.write("SCHEMA.md", "# Schema\n")
        r = self.report()
        self.assertEqual(r["broken"], [])
        self.assertEqual(r["stats"]["links"], 8)

    def test_broken_links_and_wanted_ranking(self):
        self.write("a.md", page("A", "[[missing]] [[gone]]"))
        self.write("b.md", page("B", "[[missing]] [[a]]"))
        r = self.report()
        self.assertEqual(len(r["broken"]), 3)
        self.assertEqual(r["wanted"][0], {"target": "missing", "linked_from": 2,
                                          "for_example": ["a", "b"]})

    def test_ambiguous_name_is_broken(self):
        self.write("x/note.md", page("Note X", ""))
        self.write("y/note.md", page("Note Y", ""))
        self.write("a.md", page("A", "[[note]]"))
        self.assertEqual(self.report()["broken"], [{"source": "a", "target": "note"}])

    def test_code_comments_urls_and_files_are_not_links(self):
        body = ("`[[inline]]`\n\n```\n[[fenced]]\n```\n\n<!-- [[hidden]] -->\n"
                "[site](https://example.com/x.md) ![img](pic.png) ![[diagram.svg]] "
                "[[deck.pdf]] [anchor](#part)\n")
        self.write("a.md", page("A", body))
        r = self.report()
        self.assertEqual(r["broken"], [])
        self.assertEqual(r["stats"]["links"], 0)

    def test_unclosed_fence_hides_rest_of_page(self):
        self.write("a.md", page("A", "```\n[[nope]]\n"))
        self.assertEqual(self.report()["broken"], [])

    def test_uri_links_folder_links_and_repeats(self):
        self.write("tools/README.md", "# Tools\n")
        self.write("docs/a.md", page("A", "[[obsidian://open?vault=x]] [[obsidian:/Vault/x]] "
                                          "[[Meeting: Q3]] [[../tools]] [t](../tools/) "
                                          "[[gone]] [[gone]]"))
        self.write("draw.excalidraw.md", "---\nexcalidraw-plugin: parsed\n---\n")
        r = self.report()
        self.assertEqual(r["broken"], [{"source": "docs/a", "target": "Meeting: Q3"},
                                       {"source": "docs/a", "target": "gone"}])
        self.assertEqual(r["stats"]["pages"], 2)

    def test_case_insensitive_fallback(self):
        self.write("concepts/LLM-Wiki.md", page("LLM wiki", ""))
        self.write("a.md", page("A", "[[llm-wiki]]"))
        self.assertEqual(self.report()["broken"], [])


class TestPages(WikiCase):
    def test_orphans_unreferenced_and_exemptions(self):
        self.write("README.md", "# Readme\n[[a]]\n")
        self.write("a.md", page("A", "[[b]]"))
        self.write("b.md", page("B", ""))
        self.write("c.md", page("C", "[[a]]"))
        self.write("lonely.md", page("Lonely", "no links"))
        self.write("raw/source.md", "---\nsource_url: https://x\n---\ntext\n")
        self.write("_archive/old.md", page("Old", ""))
        self.write("concepts/index.md", "# Concepts\n" + "- x\n" * 300)
        r = self.report()
        self.assertEqual(r["long"], [])
        self.assertEqual(r["unreferenced"], ["c", "lonely"])
        self.assertEqual(r["orphaned"], ["lonely"])
        self.assertEqual(r["frontmatter"], [])

    def test_frontmatter_problems(self):
        self.write("none.md", "# None\ntext\n")
        self.write("partial.md", "---\ntitle: Partial\nupdated: soon\n---\n")
        self.write("h1.md", "---\ndescription: Title from heading.\nupdated: 2026-09-01\n---\n# From H1\n")
        r = {f["path"]: f["problems"] for f in self.report()["frontmatter"]}
        self.assertEqual(r["none"], ["no frontmatter"])
        self.assertEqual(r["partial"], ["no description", "updated is not a date"])
        self.assertNotIn("h1", r)

    def test_stale_by_date_and_by_changed_neighbours(self):
        now = wl.parse_date("2026-09-28")
        self.write("old.md", page("Old", "[[fresh]]", updated="2026-01-01"))
        self.write("fresh.md", page("Fresh", "", updated="2026-09-20"))
        self.write("quiet.md", page("Quiet", "[[old]]", updated="2026-01-01"))
        self.write("expiring.md", "---\ntitle: Expiring\ndescription: d\nupdated: 2026-09-27\n"
                                  "stale_after: 2026-09-15\n---\n")
        stale = {s["path"]: s for s in self.report(now=now)["stale"]}
        self.assertEqual(stale["old"]["changed_since"], ["fresh"])
        self.assertIn("stale_after", stale["expiring"]["reason"])
        self.assertNotIn("quiet", stale)

    def test_long_pages_and_duplicate_titles(self):
        self.write("big.md", page("Big", "line\n" * 250))
        self.write("a.md", page("Same", ""))
        self.write("b.md", page("same", ""))
        r = self.report()
        self.assertEqual(r["long"][0]["path"], "big")
        self.assertEqual(r["duplicates"][0]["paths"], ["a", "b"])


class TestCli(WikiCase):
    def test_exit_codes_json_catalog_and_links_to(self):
        self.write("a.md", page("A", "[[b]]", desc="Holds A."))
        self.write("b.md", page("B", ""))
        code, out = self.run_main("--json")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["stats"]["links"], 1)

        code, out = self.run_main("--catalog")
        self.assertIn("- [[a]] A: Holds A.", out)

        code, out = self.run_main("--links-to", "b.md")
        self.assertEqual(out.strip(), "a")

        self.write("c.md", page("C", "[[nowhere]]"))
        code, out = self.run_main()
        self.assertEqual(code, 1)
        self.assertIn("c -> [[nowhere]]", out)

    def test_strict_fails_on_frontmatter(self):
        self.write("a.md", "# A\n")
        self.assertEqual(self.run_main()[0], 0)
        self.assertEqual(self.run_main("--strict")[0], 1)


if __name__ == "__main__":
    unittest.main()
