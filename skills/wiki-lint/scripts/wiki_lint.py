#!/usr/bin/env python3
"""Check the health of a markdown wiki kept by agents.

Standard library only, Python 3.8+. Point it at the wiki's root folder:

    python3 wiki_lint.py path/to/wiki                 # report
    python3 wiki_lint.py path/to/wiki --json          # machine-readable report
    python3 wiki_lint.py path/to/wiki --catalog       # every page with its description
    python3 wiki_lint.py path/to/wiki --links-to concepts/llm-wiki

What it finds:

  broken        links to pages that do not exist (the only true defect)
  wanted        broken-link targets ranked by how many pages want them
  unreferenced  pages nothing links to (hard to find by browsing)
  orphaned      pages with no links in or out
  frontmatter   pages missing frontmatter, a title, a description or a valid date
  stale         pages past their `stale_after` date, or unchanged for --stale-days
                while pages they link to have changed since
  long          pages over --max-lines lines (candidates to split)
  duplicates    titles used by more than one page

Links: [[target]], [[target|label]], [[target#heading]] and relative markdown
links such as [text](../concepts/page.md). A target resolves to the exact path
from the wiki root, then to the path relative to the linking page, then to the
one page with that file name (Obsidian's shortest-path style). Links inside
code, HTML comments, to URLs and to files such as images or PDFs are ignored.

Exit status is 1 when a link is broken (or, with --strict, when any page has a
frontmatter problem), so the script can gate a commit or a CI job.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

WIKILINK = re.compile(r"\[\[([^\]]+)\]\]")
MDLINK = re.compile(r"\[[^\]]*\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
COMMENT = re.compile(r"<!--.*?-->", re.S)
FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})")
INLINE_CODE = re.compile(r"(`+)(.+?)\1", re.S)
H1 = re.compile(r"^#\s+(.+?)\s*#*\s*$", re.M)
DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
SCHEME = re.compile(r"^(?:[A-Za-z][A-Za-z0-9+.-]*:/|mailto:|tel:)")
ROW_LIMIT = 25

SKIP_DIRS = {".git", ".obsidian", ".trash", "node_modules", ".venv", "venv", "__pycache__"}
# Entry points and generated catalogs, at any depth: nothing has to link to them,
# and they need no frontmatter.
ENTRY_PAGES = {"readme", "schema", "index", "log", "agents", "claude", "home"}
# Files with a .md extension that are not pages (Obsidian plugin data).
NOT_PAGES = (".excalidraw.md",)
# Folders whose pages follow their own rules: raw sources are immutable copies
# with their own frontmatter, and archived pages are meant to be out of the way.
RAW_DIRS = ("raw/",)
ARCHIVE_DIRS = ("_archive/", "archive/")

FILE_EXTS = frozenset("""
png jpg jpeg gif webp svg bmp tif tiff ico heic avif pdf doc docx ppt pptx key xls xlsx
numbers pages odt ods odp rtf csv tsv txt json jsonl yaml yml toml xml html htm css js mjs
ts tsx jsx py rb go rs java kt swift c h cpp hpp cs php sh bash zsh sql ini cfg conf log
ipynb zip gz tgz tar 7z rar mp3 wav m4a ogg flac mp4 mov webm avi mkv drawio excalidraw
canvas base fig sketch psd ai eps ttf otf woff woff2 wasm bin exe dmg pkg apk msi
""".split())


# ---- reading pages -------------------------------------------------------------

def mask_code(text: str) -> str:
    """Blank out HTML comments, fenced code and inline code, keeping every offset
    and line break, so links written as examples are not counted as links."""
    text = COMMENT.sub(lambda m: re.sub(r"[^\n]", " ", m.group(0)), text)
    out, fence = [], None
    for line in text.splitlines(keepends=True):
        m = FENCE.match(line)
        if fence is None and m:
            fence = m.group(1)
            out.append(re.sub(r"[^\n]", " ", line))
        elif fence is not None:
            if m and m.group(1)[0] == fence[0] and len(m.group(1)) >= len(fence) \
                    and not line.strip().strip(fence[0]):
                fence = None
            out.append(re.sub(r"[^\n]", " ", line))
        else:
            out.append(line)
    body = "".join(out)
    return INLINE_CODE.sub(lambda m: re.sub(r"[^\n]", " ", m.group(0)), body)


def split_frontmatter(text: str):
    """(fields, body, has_frontmatter). Reads the flat `key: value` subset of YAML
    that wiki frontmatter uses; lists stay as their raw string."""
    if not text.startswith("---"):
        return {}, text, False
    lines = text.splitlines(keepends=True)
    if lines[0].strip() != "---":
        return {}, text, False
    for i in range(1, len(lines)):
        if lines[i].strip() in ("---", "..."):
            fields = {}
            for raw in lines[1:i]:
                m = re.match(r"^([A-Za-z_][\w-]*)\s*:\s*(.*?)\s*$", raw)
                if m:
                    val = m.group(2)
                    if len(val) >= 2 and val[0] == val[-1] and val[0] in "'\"":
                        val = val[1:-1]
                    fields[m.group(1).lower()] = val
            return fields, "".join(lines[i + 1:]), True
    return {}, text, False


def is_file_link(target: str) -> bool:
    if SCHEME.match(target.strip()):
        return True                                 # obsidian://, mailto:, https:
    name = target.split("#", 1)[0].split("|", 1)[0].strip().rsplit("/", 1)[-1]
    return "." in name and name.rsplit(".", 1)[1].lower() in FILE_EXTS


def normalise(target: str) -> str:
    target = target.split("|", 1)[0].split("#", 1)[0].strip()
    target = target.replace("%20", " ").strip("/")
    if target.startswith("./"):
        target = target[2:]
    if target.lower().endswith(".md"):
        target = target[:-3]
    return target


def extract_links(text: str) -> list:
    body = mask_code(text)
    out = []
    for m in WIKILINK.finditer(body):
        raw = m.group(1)
        if body[max(m.start() - 1, 0)] == "!" and is_file_link(raw):
            continue                                # ![[image.png]] embed
        if not is_file_link(raw):
            out.append(normalise(raw))
    for m in MDLINK.finditer(body):
        raw = m.group(1)
        if "://" in raw or raw.startswith(("#", "mailto:", "tel:")) or is_file_link(raw):
            continue
        base = raw.split("#", 1)[0]
        if base.lower().endswith(".md") or "/" in base:
            out.append(normalise(raw))
    return [t for t in out if t]


def join(parent: str, target: str) -> str:
    parts = []
    for seg in (parent + "/" + target if parent else target).split("/"):
        if seg == "..":
            if parts:
                parts.pop()
        elif seg not in ("", "."):
            parts.append(seg)
    return "/".join(parts)


class Wiki:
    def __init__(self, root: Path):
        self.root = root
        self.pages = {}                             # path without .md -> info
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS
                                 and not d.startswith("."))
            for name in sorted(filenames):
                if not name.lower().endswith(".md") or name.lower().endswith(NOT_PAGES):
                    continue
                full = Path(dirpath, name)
                rel = full.relative_to(root).as_posix()[:-3]
                text = full.read_text(encoding="utf-8", errors="replace")
                fields, body, has_fm = split_frontmatter(text)
                h1 = H1.search(mask_code(body))
                self.pages[rel] = {
                    "path": rel, "text": text, "fields": fields, "has_fm": has_fm,
                    "title": fields.get("title") or (h1.group(1).strip() if h1 else ""),
                    "lines": text.count("\n") + 1,
                    "mtime": full.stat().st_mtime,
                }
        self.by_name = {}
        self.by_name_ci = {}
        for p in self.pages:
            base = p.rsplit("/", 1)[-1]
            self.by_name.setdefault(base, []).append(p)
            self.by_name_ci.setdefault(base.lower(), []).append(p)
        self.lower = {p.lower(): p for p in self.pages}
        self.edges, self.broken = [], []
        seen_broken = set()
        for src, info in self.pages.items():
            parent = src.rsplit("/", 1)[0] if "/" in src else ""
            for target in extract_links(info["text"]):
                dst = self.resolve(target, parent)
                if dst is None:
                    if (src, target) not in seen_broken:
                        seen_broken.add((src, target))
                        self.broken.append((src, target))
                elif dst != src:
                    self.edges.append((src, dst))

    def resolve(self, target: str, parent: str):
        rel = join(parent, target)
        for cand in (target, rel):
            if cand in self.pages:
                return cand
        # A link to a folder lands on its README or index page, as on GitHub.
        for cand in (target, rel):
            for entry in ("README", "readme", "index", "INDEX"):
                if cand + "/" + entry in self.pages:
                    return cand + "/" + entry
        base = target.rsplit("/", 1)[-1]
        if len(self.by_name.get(base, ())) == 1:
            return self.by_name[base][0]
        # Obsidian and most hosts match names case-insensitively.
        for key in (target.lower(), rel.lower()):
            if key in self.lower:
                return self.lower[key]
        if len(self.by_name_ci.get(base.lower(), ())) == 1:
            return self.by_name_ci[base.lower()][0]
        return None


# ---- checks ----------------------------------------------------------------------

def parse_date(value: str):
    value = (value or "").split("#", 1)[0].strip().strip("'\"")
    if not value:
        return None
    try:
        if DATE.match(value):
            return dt.datetime.strptime(value, "%Y-%m-%d").replace(
                tzinfo=dt.timezone.utc).timestamp()
        when = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        if when.tzinfo is None:
            when = when.replace(tzinfo=dt.timezone.utc)
        return when.timestamp()
    except ValueError:
        return None


def git_times(root: Path) -> dict:
    """Last commit time per page when the wiki is a git checkout. File mtimes are
    reset by every clone, so they are a poor record of when a page changed."""
    try:
        top = subprocess.run(["git", "-C", str(root), "rev-parse", "--show-toplevel"],
                             capture_output=True, text=True, timeout=20)
        if top.returncode:
            return {}
        log = subprocess.run(["git", "-C", str(root), "log", "--format=@%ct",
                              "--name-only", "--", "."],
                             capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return {}
    toplevel = Path(top.stdout.strip())
    out, when = {}, None
    for line in log.stdout.splitlines():
        if line.startswith("@"):
            when = float(line[1:])
        elif line.endswith(".md") and when is not None:
            try:
                rel = (toplevel / line).resolve().relative_to(root.resolve()).as_posix()[:-3]
            except ValueError:
                continue
            out.setdefault(rel, when)
    return out


def under(path: str, prefixes) -> bool:
    return any(path.startswith(p) for p in prefixes)


def is_entry(path: str) -> bool:
    return path.rsplit("/", 1)[-1].lower() in ENTRY_PAGES


def check(wiki: Wiki, stale_days: int, max_lines: int, now: float) -> dict:
    pages = wiki.pages
    inbound = {p: set() for p in pages}
    outbound = {p: set() for p in pages}
    for src, dst in wiki.edges:
        inbound[dst].add(src)
        outbound[src].add(dst)

    wanted = {}
    for src, target in wiki.broken:
        wanted.setdefault(target, [])
        if src not in wanted[target]:
            wanted[target].append(src)

    curated = [p for p in pages if not under(p, RAW_DIRS + ARCHIVE_DIRS) and not is_entry(p)]
    unreferenced = sorted(p for p in curated if not inbound[p])
    orphaned = sorted(p for p in unreferenced if not outbound[p])

    frontmatter = []
    for p in curated:
        info, problems = pages[p], []
        f = info["fields"]
        if not info["has_fm"]:
            problems.append("no frontmatter")
        else:
            if not info["title"]:
                problems.append("no title")
            if not f.get("description"):
                problems.append("no description")
            if not f.get("updated"):
                problems.append("no updated date")
            elif parse_date(f["updated"]) is None:
                problems.append("updated is not a date")
        if problems:
            frontmatter.append({"path": p, "problems": problems})

    gt = git_times(wiki.root)
    changed = {}
    for p, info in pages.items():
        changed[p] = parse_date(info["fields"].get("updated", "")) or gt.get(p) or info["mtime"]
    stale, cutoff = [], now - stale_days * 86400
    for p in curated:
        info = pages[p]
        after = parse_date(info["fields"].get("stale_after", ""))
        if after is not None and now >= after:
            stale.append({"path": p, "reason": "past its stale_after date",
                          "stale_after": day(after)})
        elif changed[p] <= cutoff:
            newer = sorted((d for d in outbound[p] if changed[d] > changed[p]),
                           key=lambda d: -changed[d])
            if newer:
                stale.append({"path": p, "updated": day(changed[p]),
                              "reason": "unchanged %d days; pages it links to changed since"
                                        % ((now - changed[p]) // 86400),
                              "changed_since": newer[:3]})

    long_pages = sorted(({"path": p, "lines": pages[p]["lines"]} for p in curated
                         if pages[p]["lines"] > max_lines), key=lambda x: -x["lines"])

    titles = {}
    for p in curated:
        t = pages[p]["title"].strip().lower()
        if t:
            titles.setdefault(t, []).append(p)
    duplicates = [{"title": pages[ps[0]]["title"], "paths": sorted(ps)}
                  for t, ps in sorted(titles.items()) if len(ps) > 1]

    return {
        "root": str(wiki.root),
        "checked": dt.datetime.fromtimestamp(now, dt.timezone.utc).strftime("%Y-%m-%d"),
        "stats": {"pages": len(pages), "links": len(wiki.edges),
                  "broken": len(wiki.broken), "unreferenced": len(unreferenced),
                  "orphaned": len(orphaned)},
        "broken": [{"source": s, "target": t} for s, t in wiki.broken],
        "wanted": [{"target": t, "linked_from": len(srcs), "for_example": srcs[:3]}
                   for t, srcs in sorted(wanted.items(), key=lambda kv: (-len(kv[1]), kv[0]))],
        "unreferenced": unreferenced,
        "orphaned": orphaned,
        "frontmatter": frontmatter,
        "stale": stale,
        "long": long_pages,
        "duplicates": duplicates,
    }


def day(ts: float) -> str:
    return dt.datetime.fromtimestamp(ts, dt.timezone.utc).strftime("%Y-%m-%d")


# ---- output ----------------------------------------------------------------------

def print_report(r: dict, max_lines: int, stale_days: int) -> None:
    s = r["stats"]
    print("%s: %d pages, %d links, %d broken" % (r["root"], s["pages"], s["links"], s["broken"]))

    def section(title, rows):
        if rows:
            print("\n%s (%d)" % (title, len(rows)))
            for row in rows[:ROW_LIMIT]:
                print("  " + row)
            if len(rows) > ROW_LIMIT:
                print("  ... and %d more (--json lists all)" % (len(rows) - ROW_LIMIT))

    section("Broken links: fix these", ["%s -> [[%s]]" % (b["source"], b["target"])
                                         for b in r["broken"]])
    section("Wanted pages, most linked first",
            ["%s (from %d: %s)" % (w["target"], w["linked_from"], ", ".join(w["for_example"]))
             for w in r["wanted"] if w["linked_from"] > 1])
    section("Orphaned: no links in or out", r["orphaned"])
    section("Unreferenced: nothing links here",
            [p for p in r["unreferenced"] if p not in set(r["orphaned"])])
    section("Frontmatter", ["%s: %s" % (f["path"], ", ".join(f["problems"]))
                            for f in r["frontmatter"]])
    section("Possibly stale (%d days)" % stale_days,
            ["%s: %s%s" % (x["path"], x["reason"],
                           " (" + ", ".join(x["changed_since"]) + ")" if x.get("changed_since") else "")
             for x in r["stale"]])
    section("Long pages, over %d lines" % max_lines,
            ["%s: %d lines" % (x["path"], x["lines"]) for x in r["long"]])
    section("Duplicate titles", ["%s: %s" % (d["title"], ", ".join(d["paths"]))
                                 for d in r["duplicates"]])
    if not s["broken"]:
        print("\nNo broken links.")


def catalog(wiki: Wiki) -> None:
    folder = None
    for p in sorted(wiki.pages):
        if under(p, RAW_DIRS + ARCHIVE_DIRS):
            continue
        parent = p.rsplit("/", 1)[0] if "/" in p else ""
        if parent != folder:
            folder = parent
            print("\n## %s" % (parent or "(root)"))
        info = wiki.pages[p]
        desc = info["fields"].get("description", "")
        print("- [[%s]] %s%s" % (p, info["title"] or p.rsplit("/", 1)[-1],
                                 ": " + desc if desc else ""))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("root", nargs="?", default=".", help="the wiki's root folder")
    ap.add_argument("--json", action="store_true", help="print the report as JSON")
    ap.add_argument("--catalog", action="store_true",
                    help="print every page with its title and description, by folder")
    ap.add_argument("--links-to", metavar="PAGE", help="list the pages that link to PAGE")
    ap.add_argument("--stale-days", type=int, default=90)
    ap.add_argument("--max-lines", type=int, default=200)
    ap.add_argument("--strict", action="store_true",
                    help="also exit 1 on frontmatter problems")
    args = ap.parse_args(argv)

    root = Path(args.root).expanduser()
    if not root.is_dir():
        print("not a folder: %s" % root, file=sys.stderr)
        return 2
    wiki = Wiki(root)

    if args.catalog:
        catalog(wiki)
        return 0
    if args.links_to:
        target = normalise(args.links_to)
        hits = sorted({s for s, d in wiki.edges if d == target}
                      | {s for s, t in wiki.broken if t == target})
        print("\n".join(hits) if hits else "no page links to %s" % target)
        return 0

    report = check(wiki, args.stale_days, args.max_lines, time.time())
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print_report(report, args.max_lines, args.stale_days)
    if report["broken"] or (args.strict and report["frontmatter"]):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
