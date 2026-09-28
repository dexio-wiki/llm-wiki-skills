# LLM wiki skills

Agent skills for keeping an LLM wiki correct as it grows.

Karpathy's [LLM wiki pattern](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f)
has an agent compile sources into a folder of linked markdown pages. Building one takes an
afternoon. Keeping it right is the hard part: new facts land on duplicate pages, claims lose
their sources, a newer blog post quietly overwrites a better-sourced number, links break on
every rename, and a second agent's rewrite wipes out the first one's work.

These nine skills are the maintenance rules. They use the open
[Agent Skills](https://agentskills.io) format, so they work in Claude Code, Codex, Cursor,
OpenCode, Hermes Agent, OpenClaw and other agents that read `SKILL.md`. One of them ships a
zero-dependency checker for broken links and page health.

## Skills

- **wiki-setup**: write the schema page that tells every agent how the wiki is organized, and
  point each agent at it.
- **wiki-orient**: read the schema, catalog, recent changes and the right page before working.
- **wiki-record**: file a decision or finding on the page that owns it, with a small edit,
  dates, sources and a change note.
- **wiki-ingest**: compile a source into every page it touches, keep an immutable hashed raw
  copy, and label how far each claim can be trusted.
- **wiki-query**: answer from the wiki with citations, check live facts at the source, and file
  substantial answers back.
- **wiki-lint**: find broken links, orphans, missing descriptions, stale and oversized pages,
  and fix them in the right order.
- **wiki-conflicts**: handle contradictions and outdated claims without silent overwrites.
- **wiki-refactor**: split, merge, rename and archive pages without breaking links.
- **wiki-shared**: rules for a wiki that several agents, machines or people write to.

They assume a folder of markdown (plain files, an Obsidian vault, a git repo) or a hosted wiki
the agent reaches over MCP. A wiki's own `SCHEMA.md` always overrides the defaults here.

## Install

Any agent, with the [skills CLI](https://github.com/vercel-labs/skills):

```bash
npx skills add dexio-wiki/llm-wiki-skills
```

Claude Code:

```
/plugin marketplace add dexio-wiki/llm-wiki-skills
/plugin install llm-wiki-skills@dexio
```

Hermes Agent, one skill at a time:

```bash
hermes skills install dexio-wiki/llm-wiki-skills/skills/wiki-lint
```

OpenClaw, from [ClawHub](https://clawhub.ai/dexio), where each skill is prefixed `dexio-`:

```bash
clawhub install dexio-wiki-lint
```

Or copy the folders under `skills/` into your agent's skills directory.

Then tell the agent where the wiki is, for example in `AGENTS.md` or `CLAUDE.md`:

```markdown
## Wiki
The team wiki is at ./wiki. Before substantial work, read wiki/SCHEMA.md and search the wiki
for the topic. Record durable decisions and findings on the page that covers them, following
the wiki-* skills. Never put secrets in it.
```

## The checker

`skills/wiki-lint/scripts/wiki_lint.py` needs Python 3.8 or later and nothing else.

```bash
python3 skills/wiki-lint/scripts/wiki_lint.py path/to/wiki
python3 skills/wiki-lint/scripts/wiki_lint.py path/to/wiki --json
python3 skills/wiki-lint/scripts/wiki_lint.py path/to/wiki --catalog
python3 skills/wiki-lint/scripts/wiki_lint.py path/to/wiki --links-to concepts/some-page
```

It reads `[[wikilinks]]` (with `|labels` and `#headings`) and relative markdown links, skips
code, comments, URLs and file links, and resolves a target the way Obsidian does: exact path,
then relative to the linking page, then the one page with that name. It reports broken links,
the missing pages most linked to, orphaned and unreferenced pages, frontmatter gaps, stale
pages, long pages and duplicate titles. It exits 1 on a broken link, so it can gate a commit
or a CI job.

## Where these come from

We run a wiki that a small fleet of agents maintains together, and these are the rules that
held up. The schema template is a cleaned-up copy of ours.

If your agents work on more than one machine, or with other people, [Dexio](https://dexio.wiki)
hosts the wiki over MCP with search, conditional writes, per-change history with the writing
agent's name, and the same link checks. The skills work the same with or without it.

## Contributing

Issues and pull requests are welcome. Keep skills short (under about 150 lines), plain, and
agent-neutral. Run the tests before a pull request:

```bash
python3 -m unittest discover -s tests
```

## License

MIT
