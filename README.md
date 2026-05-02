# spec-cli

> `git`-style CLI for Spec — governed bundles of plain-English source code.

Spec bundles are versioned, signable units of intent. You author them on
your laptop with this CLI, sync them with
[Spec Cloud](https://spec.lightreach.io), and compile them — by default
through your existing Claude Code session.

**AI inference never happens inside this CLI or inside Cloud.** Either your
Claude Code session does it (the default), or you opt in to calling Anthropic
directly via `spec compile --via api`. Either way, inference happens on
your machine, under your model / billing relationship.

---

## Install

**This project is not published on PyPI under a unique name** — the `spec-cli`
name on PyPI is taken. End users should never run `pip install spec-cli` to get
this tool (that installs a different project).

The one-liner (macOS, Linux, WSL):

```bash
curl -LsSf https://spec.lightreach.io/install.sh | sh
```

That installs [`uv`](https://docs.astral.sh/uv) if you don't already have it,
then drops `spec` on your `PATH` in an isolated environment. Full instructions
— including a manual path that doesn't pipe into `sh`, and a contributor path
that installs from a local clone — live at
[spec.lightreach.io/install](https://spec.lightreach.io/install).

Requires Python 3.9+ (`uv` will fetch one if your system Python is older).

> **Don't `pip install spec-cli`.** The PyPI name `spec-cli` is owned by an
> unrelated project; `pip install spec-cli` will install someone else's tool,
> not this one. Install via `uv tool` (above) or the contributor path in the
> install docs.

Recommended: install [Claude Code](https://claude.ai/code) too. The CLI's
`compile` flow expects it by default.

## Quick start

```bash
# Scaffold a new bundle
mkdir billing-service-rewrite && cd billing-service-rewrite
spec init

# Capture prior prompt history from Claude Code (optional; captures what
# you've already said in this project's Claude Code sessions).
spec prompts sync

# Stage + push to Spec Cloud
spec login
spec add .
spec push

# Compile. Writes .spec/compile-prompt.md; your running Claude Code
# session reads that file (via AGENTS.md) when you say "compile".
spec compile
```

## Git hooks

`spec init` inside a git repo installs **pre-commit** (mirrors `git add` into
`spec add` / `spec unstage` for bundle paths, including **renames**),
**commit-msg** (`spec git-hooks commit-msg` — runs capture and stages `.prompts`
into the **same** commit), and **pre-push** (`spec push` when you `git push` a
branch). Refresh with `spec git-hooks install`. Remove Spec hook blocks from
`.git/hooks/` with `spec git-hooks uninstall` (non-Spec hook content in the same
files is preserved). Skip Spec upload on push with `SKIP_SPEC_PUSH=1`, or skip
all hooks with `git push --no-verify`. Multi-bundle monorepos: set
`SPEC_BUNDLE_ROOT` to the bundle directory.

Semantics (capture timing, disk vs. last `spec add`, auxiliary Markdown): see the
Spec docs bundle (`docs/index.html`, Review section — Git hooks).

## What's a bundle?

A directory with exactly one `spec.yaml` at the root and at least one
`.md` file anywhere in the tree. A bundle is three tiers of files:

```
my-bundle/
├── spec.yaml              # settings · required · exactly 1
├── AGENTS.md                   # instructions for Claude Code
├── docs/
│   └── product.md              # intent (md) · at least 1 somewhere in tree
└── prompts/
    ├── scaffold.md             # prompt template · optional
    └── sessions/
        └── 2026-03-10T11-47-12Z_claude_code_d1714569.prompt
                                # captured conversational history · optional
```

### File tiers

| Tier          | Extensions         | Role                                                    |
|---------------|--------------------|---------------------------------------------------------|
| **Intent**    | `.md`, `.markdown` | What to build, in plain English                         |
| **History**   | `.prompt`          | Captured or hand-authored conversational sessions (TOML) |
| **Config**    | `spec.yaml`   | Manifest, model routing, output target                  |

`.prompt` files are **build inputs**, not telemetry. Edit them to change what
the compiler sees on the next run — see
[`docs/prompt-format.md`](./docs/prompt-format.md).

### Hard rules

- **At least one `.md`** anywhere in the tree.
- **Exactly one `spec.yaml`** at the bundle root.
- **Only these extensions** are accepted: `.md`, `.markdown`, `.prompt`, and
  the literal filename `spec.yaml`. Everything else is rejected at `add`
  time — the CLI never silently drops a file you asked for.

## Commands

### Everyday flow

| Command | Purpose |
|---|---|
| `spec init` | Scaffold `spec.yaml`, `docs/product.md`, `prompts/scaffold.md`, `prompts/sessions/`, and `AGENTS.md`. |
| `spec status` | Show staged / modified / untracked / ignored files. |
| `spec add <paths…>` | Stage files. Rejects non-spec extensions explicitly. |
| `spec push [URL]` | Upload the staged snapshot to Cloud, in 10-file batches. Accepts a `git`-style URL (see below). |
| `spec pull [URL]` | Pull the latest bundle state into the working tree. `--force` to overwrite local changes. Accepts the same URL form as `push`. |
| `spec compile` | Assemble a compile prompt for Claude Code (default) or call an API directly (`--via api`). |
| `spec log` | Print recent pushes and runs for this bundle. |

### Prompt capture (`spec prompts …`)

| Command | Purpose |
|---|---|
| `spec prompts sync` | Pull sessions from `~/.claude/projects/<encoded-cwd>/` into `prompts/sessions/`. Deterministic; re-running doesn't rewrite existing files unless `--force`. |
| `spec prompts validate` | Check every `.prompt` file against the schema. Exit 1 on error. |
| `spec prompts simulate` | (Contract-only in v0.1) Replay a session through the compiler in a read-only sandbox. |

### Auth

| Command | Purpose |
|---|---|
| `spec login` | Google OAuth device flow; stores credentials at `~/.spec/credentials` (mode `0600`). |
| `spec logout` | Forget the stored credentials. |

### Push / pull by URL (git-style)

`push` and `pull` accept an optional URL, so you don't have to edit
`spec.yaml` or carry a `--project` flag around just to point at a
different Cloud or bundle:

```bash
# Push to a specific bundle on the default Cloud
spec push https://spec.lightreach.io/billing.git

# Namespaced paths are preserved verbatim as the slug
spec push https://spec.lightreach.io/acme/billing.git

# Point at a self-hosted or local Cloud for this command only
spec push http://localhost:8000/dev-bundle
spec pull  http://localhost:8000/dev-bundle
```

Rules the parser enforces:

- **`http` or `https` only.** Device-flow tokens never travel over other schemes.
- **Host** becomes the Cloud API base for this invocation (overrides
  `SPEC_API` and the `api_base` in saved credentials — but only for
  this command).
- **Path** is the slug. Any trailing `.git` is stripped; multi-segment paths
  (`acme/billing`) are preserved for forward-compatibility with namespacing.
- **Query / fragment** are rejected. They have no meaning here and silently
  dropping them would be a footgun.

If the URL's host differs from the one in `~/.spec/credentials`, the
CLI warns (in amber) but still tries — the server's 401 is the source of
truth. If it does reject, sign in against the other host first:

```bash
SPEC_API=https://api.staging.spec.lightreach.io spec login
```

## `spec compile` in detail

The default flow is **Claude-Code-first**:

```
$ spec compile
compile · 5 spec file(s), 1 prompt template(s), 3 session(s)
✓ compile prompt ready · .spec/compile-prompt.md
next  open Claude Code in this directory and say "compile"
```

What happened:

1. The CLI walked your bundle, collected every spec doc, every prompt
   template, and every `.prompt` session file.
2. It rendered a single self-contained compile prompt — deterministically,
   so two runs on the same inputs produce byte-identical files.
3. It wrote that prompt to `.spec/compile-prompt.md` (gitignored).
4. Your project's `AGENTS.md` (scaffolded by `spec init`) tells Claude
   Code to read that file when you ask it to compile.

Alternative modes:

```bash
spec compile --stdout          # print prompt to stdout, don't write
spec compile --via api         # call Anthropic directly
                                    # (needs `spec-compiler` + ANTHROPIC_API_KEY)
spec compile --via api --dry-run --model claude-opus-4 --out ./out-opus
```

## `.prompt` files

Conversational history captured into TOML. See
[`docs/prompt-format.md`](./docs/prompt-format.md) for the full spec.

```toml
schema = "spec.prompt/v0.1"

[session]
id          = "d1714569-2799-464b-9a0e-360aced5767c"
source      = "claude_code"
started_at  = 2026-03-10T11:47:12Z
branch      = "main"

[[turns]]
role = "user"
at   = 2026-03-10T11:47:12Z
text = '''
Refactor billing.py to extract the tax logic into its own module.
Keep the public interface identical.
'''

[[turns]]
role    = "assistant"
at      = 2026-03-10T11:47:35Z
summary = "Mapping tax call sites before extraction."

  [[turns.tool_calls]]
  name = "Grep"
  args = { path = "billing/", pattern = "calculate_tax" }
```

User turns store raw text. Assistant turns store a bounded `summary` (and,
for regenerated responses, the sandbox output of `spec prompts
simulate`). Tool calls are sanitized through a shared allowlist — names and
args only, no file contents or command output.

## Environment

| Variable | Purpose |
|---|---|
| `SPEC_API` | Cloud API base URL. Default `https://spec.lightreach.io`. |
| `SPEC_HOME` | Override the credentials directory (default `~/.spec`). |
| `SPEC_OAUTH_CLIENT_ID` | Override the embedded Google OAuth client ID. |
| `CLAUDE_HOME` | Override the Claude Code project store location (default `~/.claude`). |

## Design notes

- **One local index, not a full DAG.** `.spec/index.json` records the
  sha256 of each file at `add` time and at last successful `push`. That's
  enough for a sensible `status` without being git.
- **Shared extension allow-list.** The same `SPEC_EXTENSIONS` + filename live
  here (`spec_cli/constants.py`), in the compiler, and on Cloud. Server
  is still the source of truth — this is a fast-fail.
- **Prompt capture is read-only.** The Claude Code adapter reads JSONL from
  `~/.claude/projects/…` and never writes there. Sync is safe to re-run.
- **Tool-call args are summaries, not payloads.** We never capture file
  contents, shell output, or diffs. The format only stores what an auditor
  needs to reason about why the model did what it did.
- **Compilation is pluggable.** The default `--via claude-code` path has
  zero LLM dependencies; `--via api` shells out to
  [`spec-compiler`](https://github.com/Unit237/specforge-compiler),
  which owns the SDK-weight parts.

## License

MIT. See [LICENSE](./LICENSE).
