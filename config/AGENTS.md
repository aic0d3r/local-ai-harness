# Agent notes (installed by the harness setup.sh; edit freely)

## Codemode tools

Serena, semble, `subagent`, `triage` and `dedup_scan` are codemode-only (their declarations stay out of
the prompt, which every cold turn reads again): call them from a codemode script, e.g.
`await tools.mcp__serena__find_symbol({ name_path_pattern: "x" })`; `describeTool(name)` gives the exact arguments.

## Code search — semble

Semble first for code search (the rule is in APPEND_SYSTEM.md). `search` and `find_related` require
`repo`: always the absolute project root (the cwd), never a file path. `content: "docs"|"config"|"all"`
for non-code.

## Serena — symbol-level work

Call `activate_project` once per project, then prefer Serena for symbols:
`get_symbols_overview` (file structure), `find_symbol` (definitions), `find_referencing_symbols`
(callers/impact), `replace_symbol_body` / `insert_after_symbol` (edits), `rename_symbol`
(project-wide rename), `get_diagnostics_for_file` (type/syntax errors). Use grep only for
exact-string sweeps, and native read/edit for docs and config files.

Don't read a whole source file to look at one symbol: a function body via `find_symbol` costs
~0.8k chars against ~14k for the file, and an outline via `get_symbols_overview` ~0.4k. Read a
file whole only when you need most of it; otherwise use `offset`/`limit`. Serena needs a
language server for the file's language (`.serena/project.yml` `language_servers`); if a tool
reports none, fall back to semble + `read` with `offset`/`limit`.

## Web search — DuckDuckGo

If an answer depends on knowledge likely newer than your training (recent packages/tools,
releases, maintenance status, current docs, "best X" questions), search with the DuckDuckGo MCP
server first (`mcp__ddg__search`), then read the sources with `mcp__ddg__fetch_content`. It ships
enabled, needs no API key, and returns Markdown (`max_length` / `start_index` paginate long pages).
Long URLs may come back as `ref://` tokens; `fetch_content` accepts them directly, and
`mcp__ddg__expand_link` resolves them.

Jina ships disabled: enable it with `/mcp` when you need academic search (arXiv/SSRN), images, or
reranking. Check `~/.pi/agent/mcp.json` (`enabled: false` = off) before telling the user a tool is
unavailable.
