"""Check every skill against the Agent Skills spec (agentskills.io/specification) and
that the Claude Code marketplace lists them all. Standard library only.

    python3 tests/check_skills.py
"""
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
NAME = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")


def frontmatter(text):
    if not text.startswith("---\n"):
        return None
    end = text.find("\n---\n", 4)
    if end < 0:
        return None
    fields = {}
    for line in text[4:end].splitlines():
        m = re.match(r"^([a-z-]+):\s*(.*)$", line)
        if m:
            val = m.group(2).strip()
            if len(val) >= 2 and val[0] == val[-1] == '"':
                val = json.loads(val)
            fields[m.group(1)] = val
    return fields


def main():
    errors = []
    skills = sorted(p.parent for p in ROOT.glob("skills/*/SKILL.md"))
    for d in skills:
        text = (d / "SKILL.md").read_text(encoding="utf-8")
        fm = frontmatter(text)
        where = d.relative_to(ROOT)
        if fm is None:
            errors.append("%s: no frontmatter" % where)
            continue
        name, desc = fm.get("name", ""), fm.get("description", "")
        if not NAME.match(name) or len(name) > 64:
            errors.append("%s: invalid name %r" % (where, name))
        if name != d.name:
            errors.append("%s: name %r does not match folder" % (where, name))
        if not desc or len(desc) > 1024:
            errors.append("%s: description length %d" % (where, len(desc)))
        if ": " in desc and not re.search(r'^description: "', text, re.M):
            errors.append("%s: description with ': ' must be quoted" % where)
        if len(text.splitlines()) > 500:
            errors.append("%s: SKILL.md over 500 lines" % where)
        if "—" in text:
            errors.append("%s: contains an em dash" % where)

    market = json.loads((ROOT / ".claude-plugin" / "marketplace.json").read_text())
    listed = {s for p in market["plugins"] for s in p.get("skills", [])}
    for d in skills:
        if "./" + d.relative_to(ROOT).as_posix() not in listed:
            errors.append("%s: missing from marketplace.json" % d.relative_to(ROOT))

    for e in errors:
        print(e)
    print("%d skills checked, %d problems" % (len(skills), len(errors)))
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
