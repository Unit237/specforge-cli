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
then drops `spec` on your `PATH` in an isolated environment, and runs
`spec shell install` (a `git` wrapper: `git init` → `spec init`, post-clone
`spec bundle doctor` when the clone contains `spec.yaml`, plus `spec live`
autostart — see the hosted docs). Full instructions
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

The daily model is intentionally small:

```bash
spec discover       # once: find/register repos across saved workspaces
spec on             # from anywhere: connect + start every watcher
spec activity       # all teammates and agents; add --show-tool-runs for tools
spec prs            # all of your open PRs under Spec watch
spec review         # choose your Compress cloud agent or a Spec teammate
```

`spec discover` is system-wide, not cwd-relative: an explicit workspace root is
remembered, rootless runs rescan every remembered workspace, and older installs
derive the workspace from their registered bundle paths. On a true first run it
scans your home folder. Existing Spec repos are registered as well as newly
initialized repos, fixing the old "selected but unbound" hole.
Disposable Codex task checkouts under `.codex-worktrees/` are excluded from the
registry so they cannot start duplicate watchers for the same repository.

`spec on` creates or resolves the Cloud bundle identity needed by each watcher,
then starts all registered watchers idempotently. It does **not** upload bundle
files. Content transfer is the separate, explicit `spec publish` command;
`spec push` remains a compatibility alias for hooks and older scripts.
On a watcher's first start, existing local transcript history becomes its live
baseline rather than being uploaded as a backlog. Only subsequent turns stream;
use `spec prompts capture` when you deliberately want historical sessions.

To request review for the current PR (or choose from all open watched PRs):

```bash
spec review --with cloud          # your Actionairy-authorized Compress agent
spec review --with @alice         # native GitHub request to a Spec teammate
spec review --pr owner/repo#123   # explicit PR from any watched project
```

That command adds the explicit `agent-review` label. GitHub delivers the event,
Actionairy selects the authorized reviewer profile and memory scope, and
Compress runs a read-only review tied to the current head SHA. The durable
GitHub review includes a 1.0–10.0 score; only 9.0+ with no blocking finding
passes. A later push triggers a fresh review for the new SHA while the label
remains present.

For a single new project:

```bash
# Scaffold a new bundle
mkdir billing-service-rewrite && cd billing-service-rewrite
git init
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

`spec init` only initializes a valid Git working tree. It resolves
`git rev-parse --show-toplevel`, so invoking it in a subdirectory initializes
the repository root; outside Git it exits before writing anything. This also
supports Git worktrees, whose `.git` is a file rather than a directory.

`spec init` installs **pre-commit** (captures sessions and
mirrors `git add` into `spec add` / `spec unstage` for bundle paths, including
**renames**), a compatibility-only **commit-msg** hook, **pre-push**
(`spec push` when you `git push` a
branch), and **post-merge** (prompt rollup on trunk plus a fast **local-only**
bundle alignment check — stderr hints only when something is off, no Cloud
round-trip). Refresh with `spec git-hooks install`. Remove Spec hook blocks from
`.git/hooks/` with `spec git-hooks uninstall` (non-Spec hook content in the same
files is preserved). Skip Spec upload on push with `SKIP_SPEC_PUSH=1`, or skip
all hooks with `git push --no-verify`. Existing multi-bundle monorepos can set
`SPEC_BUNDLE_ROOT` to select a legacy nested bundle, but new initialization is
one canonical Spec root per Git worktree.

On first push to your own handle, `spec push` creates the Cloud bundle if it
does not exist yet (the server may suffix the slug — `my-app-2`, etc. — and
the CLI updates `cloud.project` in `spec.yaml` when that happens).

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
| `spec discover [ROOT]` | System-wide repository inventory. ROOT is remembered; no ROOT scans all saved workspaces. Registers existing bundles and optionally initializes new ones. |
| `spec on` / `spec off` | Connect and start every registered watcher, or stop them all. Connecting uploads no repository content. |
| `spec activity [--show-tool-runs]` | Workspace-wide live feed for Codex, Claude Code, Cursor, and Compress. Tool calls stay hidden unless requested, then render as a grouped digest. |
| `spec prs [--all-authors]` | Open GitHub PRs across repositories registered with Spec watch. Defaults to your PRs. |
| `spec review [--pr OWNER/REPO#N] [--with cloud|@TEAMMATE]` | Request a SHA-bound Compress cloud review or a native GitHub teammate review. |
| `spec status` | Git-like sections: staged for push, modified (out-of-date snapshot vs not staged), untracked, etc. |
| `spec add <paths…>` | Stage files. Rejects non-spec extensions explicitly. |
| `spec publish [URL]` | Explicitly upload the staged snapshot to Cloud. `--all` handles every registered bundle. `spec push` is a backwards-compatible alias. |
| `spec pull [URL]` | Pull the latest bundle state into the working tree. `--force` to overwrite local changes. Accepts the same URL form as `push`. |
| `spec compile` | Assemble a compile prompt for Claude Code (default) or call an API directly (`--via api`). |
| `spec log` | Print recent pushes and runs for this bundle. |

### Prompt capture (`spec prompts …`)

| Command | Purpose |
|---|---|
| `spec prompts capture --source claude_code\|cursor\|codex\|compress\|all` | Append new local agent sessions to `prompts/<branch>.prompts`. Deterministic; re-running skips sessions already captured at the same turn count. |
| `spec prompts validate` | Check every `.prompts` file against the schema. Exit 1 on error. |
| `spec prompts simulate` | (Contract-only in v0.1) Replay a session through the compiler in a read-only sandbox. |

### Codex capture

```bash
# Show recent Codex chats for this bundle, pick one by number, and import it.
spec codex capture

# Non-interactive: import the first recent chat.
spec codex capture --index 1

# Preview without writing.
spec codex capture --dry-run
```

`spec codex capture` reads Codex Desktop's local thread index
(`~/.codex/state_5.sqlite`) and rollout logs (`~/.codex/sessions/...`), filters
to chats whose working directory matches this bundle, then appends the selected
chat as one `source = "codex"` session in `prompts/<branch>.prompts`. Common
tokens and Authorization headers are redacted before serialization.

For the normal git-like workflow, you usually do not need the Codex-specific
command: `spec add .`, `git commit` with Spec hooks, and
`spec prompts capture --source all` scan the local Claude Code, Cursor,
Codex Desktop, and Compress stores for sessions that belong to the current
bundle.

### Spec Live — real-time team feed

Spec Live broadcasts every new prompt you write in Cursor / Claude Code / Codex / Compress
to the rest of your team within a few seconds, surfaces theirs back in your
terminal, and streams a live "who is editing what" presence layer that AI IDEs
can read before making file edits. **All of it is on by default the moment you
install the CLI.**

Use the machine-wide workday switch instead of managing bundle daemons one by
one:

```bash
spec on                             # connect + start every registered project
spec status                         # plain Watching / Stopped / Needs connection states
spec activity                       # every teammate and agent across the workspace
spec activity --show-tool-runs      # grouped tool digest plus code blocks
spec off                            # stop all watchers and release local locks
```

`spec on` learns the current bundle, remembers every bundle seen through
`spec init` / `spec discover` / `spec watch` / `spec live start`, and restarts
those known bundle watchers idempotently. `spec off` mutes this machine before
stopping them, so a shell hook cannot race the shutdown. It controls local
coordination only; cloud-side PR jobs are configured and run separately.

You can launch an agent from a non-git folder that contains several registered
repositories. Spec does not initialize that folder or invent a repository for
it. A conversation that touches one child repository is routed there; a
conversation that spans multiple children (or none) is emitted once as
**workspace** activity and is visible in `spec activity` from any folder.

Fresh bundles no longer need a content push before they can be watched.
`spec on` resolves or creates the Cloud identity and stamps the immutable
`cloud.bundle_id`, without uploading staged files. If connection fails,
`spec status` says **Needs connection** and prints the actionable error; it no
longer communicates state through filled and hollow circles.

It also materializes active agent rounds into
`.spec/team-coordination.json` and `.spec/team-coordination.md`. The brief
shows objectives, latest progress, claimed paths, and recent handoffs. Agents
read it before planning; when the last round finishes or expires, both files
are removed automatically.

After upgrading an existing bundle, run `spec init --upgrade-rules` once.
It refreshes Cursor and Claude integrations and appends an idempotent
Spec-managed coordination block to `AGENTS.md` for Codex and other agents,
without replacing the repository's own instructions.

```bash
# Start every known local watcher for the workday.
spec on

# One-shot snapshot — the last 20 prompt events, no daemon required.
spec team

# Who's editing what RIGHT NOW (file + line counts per teammate)
spec presence show

# Programmatic conflict probe (for hooks / scripts):
spec presence check path/to/file.py
#   exit 0 → clear · exit 2 → a teammate is editing it (warning printed)

# See the machine switch, all watchers, and current bundle state
spec status

# Advanced per-bundle / per-machine controls
spec live off       # disable broadcasting for this bundle (writes spec.yaml)
spec live mute      # silence broadcasting on this machine for every bundle
spec live unmute    # remove the per-machine mute
spec live on        # re-enable for this bundle (with --verbose for full assistant text)
```

| Command | Purpose |
|---|---|
| `spec on` | Start-of-workday switch: unmute sharing, enable autostart, prune missing registrations, and start every known local bundle watcher. Safe to repeat. |
| `spec off` | End-of-workday switch: disable new automatic starts, gracefully stop every known watcher, and release leftover local edit locks. Cloud-side jobs are configured separately. |
| `spec status` | Machine-wide ON/OFF + watcher summary, followed by the current bundle's staging status when run inside a bundle. Works outside a bundle. |
| `spec watch` | Long-running daemon. Broadcasts your prompts + dirty files and tails the workspace stream from the moment you join: all of your own conversations plus every conversation authored by an accepted teammate. Pass `--bootstrap` only when you want recent Cloud history first. Every row names its chat; Codex rows use textual `USER`, `UPDATE`, and `ANSWER` badges so prompts, progress commentary, and conclusions remain distinct without relying on color. Internal reasoning and completion sentinels stay hidden. `--mirror` also writes incoming peer events to `prompts/captured/peers/<handle>/`. |
| `spec team` | Snapshot of recent prompt activity (no SSE). |
| `spec team watch` | Live workspace-wide SSE tail (`GET /api/me/prompt-stream`). **Default (non-compact) layout:** user bodies up to the schema wire cap; **assistant prose in each finished turn** is the merged stored body (same cap as `spec watch`), with fenced code collapsed to `[code: lang ~N lines]` unless you pass **`--show-tool-runs`**, which also lists structured tool calls and preserves code blocks. A **`● turn complete`** footer still points to **`/turn`** / **`/full`** for pager drill-in (whole thread, structured tools, or re-fetch from Cloud — see [docs/team-watch-slash-commands.md](docs/team-watch-slash-commands.md)). Headers: USER / UPDATE / ANSWER / AI / ERROR, source, branch, bundle, time; chip row: chat title, `cwd`, `touched`, color **`session`** id. Flags include `--compact`, `--no-verbose`, `--no-critic`, `--no-commands`, `--no-heartbeat`, `--show-tool-runs`, `--notify`. |
| `spec team flag <event_id>` | Flag a teammate's prompt event (`warning` / `question` / `block` / `ack`) in near real time. The flag fans out over the same SSE channel so every connected watcher sees it within an RTT. |
| `spec presence show` | Show every teammate's current dirty-file list with `+/-` line counts. |
| `spec presence check <path>` | Exit code is the contract: 0 = clear, 2 = a teammate is editing the path. |
| `spec locks check <path>` | Unified pre-edit contract: task claims, dirty-tree presence, and same-machine active edits. Exit `0` = clear, `2` = conflict, `3` = coordination unknown. `--json` returns the same `state` plus holders and `pull_alerts`. Existing generated mirrors can be used even when the repository is not a compile bundle. |
| `spec locks pull-status` | Exit `0` when no teammate is ahead of your branch, `2` when at least one same-branch peer has a different `head_commit` — i.e. they pushed and you should `git pull`. `--json` for hooks. |
| `spec locks acquire <path>` | Take a per-machine **active-edit** lock for a single AI agent. Use `--agent claude_code\|cursor\|codex\|compress\|...` plus `--session <id>` so the same agent renewing doesn't conflict with itself. `--block` exits `2` on cross-agent overlap. Locks have a TTL (default 5 min, cap 60 min) so a crashed agent never deadlocks. |
| `spec locks release <lock_id>` | Drop a previously-acquired active-edit lock. Unknown ids exit `0` (no-op) — PostToolUse hooks fire unconditionally and must never break. |
| `spec locks list` | Show active edit locks in this bundle. Add `--all` for every repo in the machine-wide registry; filter with `--agent` / `--session`, and use `--include-expired` for stale rows. |
| `spec locks prune` | Physically remove expired active-edit locks. Reads already filter them; this is housekeeping. |
| `spec hooks install-claude` | Wire the Spec Live `PreToolUse` *and* `PostToolUse` hooks into Claude Code (`spec init` does this for you on first run). PreToolUse warns on teammate conflicts and auto-acquires an active-edit lock; PostToolUse releases it. Add `--block` to refuse edits on conflict instead of just warning. |
| `spec live status` | Resolved broadcasting state — bundle setting, machine mute, and the final answer. |
| `spec live on` / `off` | Per-bundle: writes `cloud.prompt_stream` to `spec.yaml`. Commit it so teammates inherit. |
| `spec live mute` / `unmute` | Per-machine: lives in `~/.spec/preferences.json`. Receivers always work; this only stops your outgoing share. |

#### `spec team watch` — stdin slash commands

Type `/help` while the stream is running for the short in-app list. From a **bundle cwd**, `/push@handle` (or `/push handle …`) appends a git-push handoff to `.spec/team-push-requests.yaml`, merged into `team-presence.json` / `team-editing-brief.md` by `spec watch`. **`/turn`** and **`/full`** open your system **`less`** (or **`PAGER`** / **`SPEC_TEAM_WATCH_PAGER`**) in a full-screen pager: press **`q`** to return to the live feed (live lines are paused while the pager runs). Full reference: **[docs/team-watch-slash-commands.md](docs/team-watch-slash-commands.md)**.

#### How AI IDEs see teammate presence

`spec watch` keeps `.spec/team-presence.json` fresh whenever a teammate's
state changes. The file has a stable, documented shape (`schema: 1`, plus
`members[]` and a pre-built `files_index` for O(1) lookup). Three integration
vectors today:

- **Claude Code** — `spec init` writes `.claude/settings.json` with a
  `PreToolUse` hook that runs `spec hooks claude-pre-tool-use` before every
  `Edit` / `Write` / `MultiEdit` / `NotebookEdit`. If a teammate is editing
  the target file, Claude shows the warning inline. Add `--block` mode to make
  Claude refuse the call until you intervene.
- **Cursor** — `spec init` writes `.cursor/rules/spec-team-presence.mdc`
  with `alwaysApply: true`. Cursor's model voluntarily runs `spec presence
  check` before suggesting edits.
- **Any AI agent** — `AGENTS.md` (also written by `spec init`) instructs any
  model-driven agent to call `spec locks check <path>` before making
  destructive edits and surface the warning to the user.

**Single-user, multi-agent locks.** `team-presence.json` answers "is a
teammate dirty here?" — but a single dev commonly has Claude Code, Cursor, and
Codex all editing the same working tree in parallel. Git can't tell those
agents apart; `team-presence.json` lumps them under one `self` block. To
coordinate inside one machine we maintain one shared registry:

`~/.spec/active-edits.json` (or `$SPEC_HOME/active-edits.json`) — a list of
short-lived **active-edit locks** keyed by
`(bundle_root, agent, session_id, paths)`. Every local Spec repo uses this same
physical file, while `bundle_root` keeps identical relative paths in unrelated
repos from conflicting. Existing per-repo `.spec/active-edits.json` files are
imported once and left untouched. Each lock has a TTL (default 5 minutes,
capped at 60). The flow:

1. Before a write tool call, the agent's PreToolUse hook calls
   `spec locks acquire <path> --agent <name> --session <id>`. Same agent +
   session re-acquires is a **renewal** (no conflict); cross-agent or
   cross-session overlap surfaces as a `conflicts` entry in the JSON output.
2. After the write tool call, PostToolUse calls `spec locks release <id>`
   (or the matching `claude-post-tool-use` hook does it automatically).
3. Any caller — `spec locks check <path>`, Cursor's rule, the brief
   renderer — sees both teammate locks **and** your-own-agent locks merged
   into one `holders[]` array (with `kind: "active_edit"` on the same-
   machine rows so renderers can disambiguate).

`spec hooks install-claude` writes both PreToolUse and PostToolUse blocks so
Claude Code participates in this layer out of the box. Cursor and Codex
integrations can do the same via their respective rule / config systems —
the contract is just "call `spec locks acquire` before write, `release`
after". A crashed agent never holds a lock past the TTL; `spec locks prune`
is a manual cleanup if you ever need it.

Verify the shared registry at any time—even outside a repo—with
`spec locks list --all --json`. The `bundle_root` field on every row shows
which repository owns the lock.

**Post-push pull hint.** When you run `spec push`, the CLI fires one extra
presence event after the upload succeeds with the new `head_commit` baked in.
Teammates' watchers receive it over the SSE channel within an RTT, their
`.spec/team-presence.json` is refreshed, and `.spec/team-editing-brief.md`
grows a `## Pull needed` section listing every same-branch peer whose
`head_commit` differs from theirs. `spec locks pull-status` is the dedicated
exit-code probe that hooks call before write tools (`Edit` / `Write` /
`MultiEdit`); `spec locks check <path> --json` carries the same `pull_alerts`
array alongside the per-path holders. Cross-branch divergence is intentionally
ignored — only same-branch ahead-of-you is treated as a "git pull first" signal.

**Workspace-wide live tail:** `spec watch`, `spec activity`, and
`spec team watch` open `GET /api/me/prompt-stream`. The feed includes every
conversation you authored plus every conversation authored by an accepted
teammate; unrelated users remain limited to bundles they can already read.
Each project-bound event carries `bundle_label`, while repository-neutral
events carry the `workspace` label. The repo-local watcher still routes and
broadcasts local turns for its registered bundle.

When `spec watch` is not healthy, `spec locks check` returns **unknown**
(exit 3). Warn-only hooks still preserve local edit availability, but no agent
may describe missing telemetry as proof that a path is clear.

What's still on the roadmap (deferred until they earn the cost):

- **Live cursor position** (which line / column a teammate is on) — needs a
  per-editor extension or LSP server. The SSE channel is the substrate.
- **Sub-file hunk granularity** — `git diff -U0` parsing to ship `(start_line,
  end_line)` ranges in addition to the per-file `+/-` counts.

Privacy posture, by default:

- Secrets are redacted from every outbound payload (same `_SECRET_PATTERNS` as `.prompts` files on disk).
- Assistant turns are summary-only — bodies stay local unless you set `verbose: true` in `spec.yaml` or pass `--verbose-out`.
- The author block is server-stamped from your bearer token; nobody can spoof your handle on the feed.
- Only project members can read or post; outsiders get 400.
- Presence events follow the same opt-out path as prompt events: `spec live off` (per bundle) or `spec live mute` (per machine).

Architecture and full design: [`spec/PROMPT-LIVE-PLAN.md`](../spec/PROMPT-LIVE-PLAN.md).

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

## Development

Tests and linters expect this repo’s dependencies (including `rich` and
`pytest`). Running `python -m pytest` with the bare system interpreter
(Apple Command Line Tools Python, etc.) will fail at import time with
`ModuleNotFoundError` — use a project environment first.

From the `spec-cli` directory:

```bash
# Recommended (uses pyproject.toml; pulls dev extras including pytest)
uv run pytest

# Or: editable install into a venv, then pytest on PATH
python3 -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
pytest
```

## Environment

| Variable | Purpose |
|---|---|
| `SPEC_API` | Cloud API base URL. Default `https://spec.lightreach.io`. |
| `SPEC_HOME` | Override the credentials directory (default `~/.spec`). |
| `SPEC_OAUTH_CLIENT_ID` | Override the embedded Google OAuth client ID. |
| `CLAUDE_HOME` | Override the Claude Code project store location (default `~/.claude`). |
| `CODEX_CLI_HOME` | Override the Codex Desktop home used by `spec codex capture` (default `~/.codex`). |

## Design notes

- **One local index, not a full DAG.** `.spec/index.json` records the
  sha256 of each file at `add` time and at last successful `push`. That's
  enough for a sensible `status` without being git.
- **Shared extension allow-list.** The same `SPEC_EXTENSIONS` + filename live
  here (`spec_cli/constants.py`), in the compiler, and on Cloud. Server
  is still the source of truth — this is a fast-fail.
- **Prompt capture is read-only.** The Claude Code and Codex adapters read
  local JSONL / SQLite stores and never write there. Capture is safe to re-run.
- **Oversized captures stay local.** The pre-commit hook does not silently
  stage a branch `.prompts` snapshot larger than 5 MiB; curate or split it
  before sharing so raw session volume does not inflate Git history.
- **Tool-call args are summaries, not payloads.** We never capture file
  contents, shell output, or diffs. The format only stores what an auditor
  needs to reason about why the model did what it did.
- **Compilation is pluggable.** The default `--via claude-code` path has
  zero LLM dependencies; `--via api` shells out to
  [`spec-compiler`](https://github.com/Unit237/specforge-compiler),
  which owns the SDK-weight parts.

## License

MIT. See [LICENSE](./LICENSE).
