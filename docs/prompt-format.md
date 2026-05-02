# The `.prompts` file format

> **Schema:** `spec.prompts/v0.1` (file shape) · **Layout:** v0.2 (one file per branch)
> **Status:** draft — the contract is not yet stable. Review and argue
> here before code lands.

A `.prompts` file bundles the conversational **sessions** captured on a
git branch into one human-readable TOML artifact. Trunk has one such
file (`prompts/main.prompts`). Each non-trunk branch has its own draft
file under a slugged version of the branch name. `spec prompts capture`
appends new sessions into the current branch's file idempotently.

`.prompts` files are treated as **build inputs**, not telemetry. Editing
one changes what the compiler sees on the next run. That is the whole
point.

Prompts are a **first-class class of source**, not markdown in disguise.
`.md` is for specs. `spec.yaml` is for the compiler contract.
`.prompts` is for the English-language work that produced code.

---

## Activity aggregates (Spec Cloud)

The compiler still treats `.prompts` as **build input**, not telemetry. Separately, **Spec Cloud** may compute **read-only aggregates** for engineer profiles and bundle dashboards (contribution heatmaps, iteration depth, abandonment rate). Those metrics use only what capture already writes: `[[sessions.turns]]` timestamps and `role`, optional `ended_at`, optional `[sessions.commit].commit_sha`, and `operator` for attribution.

Canonical definitions (quiet window, grace days, what “finished for counting” means) are published here:

**https://spec.lightreach.io/docs#activity-metrics**

---

## One file per branch (v0.2)

The central rule. The `.prompts` file for a branch lives at:

```
<bundle-root>/prompts/<branch-slug>.prompts
```

The slug is the branch name, lowercased, with anything outside
`[a-z0-9._-]` (including `/`) collapsed to a single `-`, leading and
trailing `.` / `-` stripped. So:

| Branch                       | File                                  |
| ---------------------------- | ------------------------------------- |
| `main`                       | `prompts/main.prompts`                |
| `feature/billing-rewrite`    | `prompts/feature-billing-rewrite.prompts` |
| `dependabot/npm/foo-1.2.3`   | `prompts/dependabot-npm-foo-1.2.3.prompts` |

The original branch name (with case / `/` / non-ASCII intact) is
preserved in `[commit].branch` and on every session's
`[sessions.commit].branch`, so the slug is just a stable filesystem
handle, not a reversible encoding.

`spec prompts capture` is **append-only**. New sessions go into the
current branch's file in `started_at` order; sessions whose `id` is
already present are dropped. Re-running capture is safe and produces
byte-identical output. The branch's prior captured files (under
`prompts/captured/<timestamp>.prompts` from the v0.1 era) are still
accepted by every parser; new captures land at the root of `prompts/`.

### Why one file per branch

- **One narrative per branch.** A reviewer reading a draft branch sees
  the unmerged thinking in *one* file with a stable name, not a
  forensic crawl through a directory of timestamped envelopes.
- **PR-shaped review by construction.** The diff between a branch's
  prompts file and trunk's is exactly the unmerged delta — same
  shape every engineer already knows from GitHub.
- **Cloud merges by appending.** Approving a branch review promotes
  every session in the branch's file into trunk's file with
  `merged_from` / `merged_at` / `approved_by` stamped on each one.
  The trunk file is the canonical, accumulated record. The branch
  file stays on the branch under its own slug for history.
- **Append-only diffs.** The trunk file only ever grows; merges never
  reorder existing sessions. Two reviews touching the same session id
  collapse cleanly (idempotent dedup).
- **Feed-ready.** `/prompts` (the public feed) walks each bundle's
  trunk prompts file. The "Reviewed only" filter keys off
  `merged_from` to surface just the green-lit thinking.

---

## Bundle layout (v0.2)

```
my-bundle/
├── spec.yaml                          # exactly 1 · manifest
├── docs/
│   ├── product.md                          # specs — English-language source
│   └── auth.md
└── prompts/
    ├── main.prompts                        # trunk · accumulated narrative
    ├── feature-billing-rewrite.prompts     # one .prompts per non-trunk branch
    └── feature-auth.prompts
```

The branch-file convention is the primary path. The legacy
`prompts/captured/` / `prompts/curated/` / `prompts/curated/_pending/`
tiers from v0.1 are still accepted by every parser — bundles that
predate v0.2 keep working untouched — but new captures always go to
the per-branch file at the root. Cloud's branch-merge is the canonical
path from "my draft branch" to "on trunk".

### Legacy tiers (pre-v0.2)

`.prompts` files in v0.1 lived in two populations that deserved very
different review treatment, in two directories:

| Tier        | Directory                           | Written by              | Compiler treats as        |
| ----------- | ----------------------------------- | ----------------------- | ------------------------- |
| `captured`  | `prompts/captured/`                 | `prompts capture` (v0.1) | advisory scrollback        |
| `curated`   | `prompts/curated/`                  | `prompts review` accept | first-class intent         |
| `pending`   | `prompts/curated/_pending/`         | `prompts submit`        | excluded entirely          |
| `legacy`    | `prompts/` (root, no subdirectory)  | pre-tier capture        | treated as curated         |

In v0.2 the local `submit` / `review` / `accept` workflow is no longer
the canonical path — Cloud branch reviews are. The legacy CLI commands
still work for users without Cloud, but they target the older
`prompts/captured/` directory and don't touch the per-branch files at
the root. The two coexist; neither blocks the other.

Three file classes. No overlap:

| Extension           | Kind       | Contains                                |
| ------------------- | ---------- | --------------------------------------- |
| `spec.yaml`    | `settings` | compiler manifest (exactly one)          |
| `.md` / `.markdown` | `md`       | spec / intent prose                      |
| `.prompts`          | `prompts`  | sessions that produced one commit        |

`.md` under `prompts/` is **rejected**. Prompts have their own
extension; the folder is pure convention.

---

## Top-level format

`.prompts` is TOML. One file contains **one or more sessions**.

```toml
schema = "spec.prompts/v0.1"

# ── Commit metadata ────────────────────────────────────────────────
[commit]
branch          = "main"                  # captured git branch
message         = "Extract tax logic into its own module"
committed_at    = 2026-04-21T11:58:03Z    # UTC, Z-suffixed
author_name     = "Alice Chen"             # from `git config user.name`
author_email    = "alice@example.com"      # from `git config user.email`
author_username = "alicec"                 # optional · Cloud handle when linked

# ── One or more conversational sessions ────────────────────────────
[[sessions]]
id          = "d1714569-2799-464b-9a0e-360aced5767c"
source      = "claude_code"                # claude_code | cursor | manual
model       = "claude-sonnet-4-6"
started_at  = 2026-04-21T11:47:12Z
ended_at    = 2026-04-21T11:58:03Z
operator    = "alice@example.com"          # who drove the chat (may ≠ commit author)

title       = "Extract tax logic"
summary     = """
Pulled tax calculation out of billing.py into its own module, keeping
the public interface identical.
"""
lesson      = "Grep call sites first; the refactor was trivial once the graph was in my head."
tags        = ["refactor", "billing", "python"]
outcome     = "shipped"                    # shipped | abandoned | exploratory | failed
visibility  = "public"                     # public | private  (default: public)

forked_from = ""                           # optional · session id of the ancestor

# Files the session's tool calls touched. Derived from tool-call args on
# first write; safe to hand-edit when a path slipped in by exploration.
paths_touched = [
  "billing/billing.py",
  "billing/tax.py",
]

  [[sessions.turns]]
  role = "user"
  at   = 2026-04-21T11:47:12Z
  text = """
  Refactor billing.py to extract the tax logic into its own module.
  Keep the public interface identical.
  """

  [[sessions.turns]]
  role    = "assistant"
  at      = 2026-04-21T11:47:35Z
  model   = "claude-sonnet-4-5"
  summary = "Mapping tax call sites before extraction."

    [[sessions.turns.tool_calls]]
    name   = "Grep"
    args   = { pattern = "calculate_tax", path = "billing/" }

    [[sessions.turns.tool_calls]]
    name   = "Read"
    args   = { path = "billing/billing.py" }

    [[sessions.turns.tool_calls]]
    name   = "Edit"
    args   = { path = "billing/billing.py",
               old_head = "def calculate_tax(",
               new_head = "from .tax import calculate_tax" }

# ── Append-only edit log for the file as a whole ───────────────────
# Every hand-edit must leave a record here. Removing an entry is a
# validator error.
[[edits]]
at       = 2026-04-21T09:00:00Z
by       = "alice@example.com"
sessions = ["d1714569-…"]
turns    = [3, 7]
reason   = "Redacted internal algorithm name in turn 3."
```

---

## Field reference

### `[commit]` — required

| Field             | Required | Notes                                           |
| ----------------- | -------- | ----------------------------------------------- |
| `branch`          | yes      | git branch at commit time                       |
| `message`         | no       | commit message                                  |
| `committed_at`    | no       | UTC RFC-3339, `Z` suffix                         |
| `author_name`     | yes      | `git config user.name`                          |
| `author_email`    | yes      | `git config user.email` — never rendered publicly |
| `author_username` | no       | Cloud handle; set when the email maps to a user  |

Email lives in the file because it lives in every git commit anyway.
Public-facing UI (cards, feed, permalinks) only ever shows
`author_username` when present, else `author_name`, else `anonymous`.

### `[[sessions]]` — one or more required

| Field        | Required | Notes                                                                 |
| ------------ | -------- | --------------------------------------------------------------------- |
| `id`         | yes      | unique UUID-ish string; primary key in Cloud and in permalinks         |
| `source`     | yes      | `claude_code` \| `cursor` \| `manual`                                 |
| `model`      | no       | captured model name (what the engineer actually ran)                   |
| `started_at` | no       | UTC RFC-3339                                                           |
| `ended_at`   | no       | UTC RFC-3339                                                           |
| `operator`   | no       | who drove the chat (email); may differ from `[commit].author_email`    |
| `title`      | no       | author-written headline; drives feed readability                       |
| `summary`    | no       | 1–3 sentence author-written prose                                      |
| `lesson`     | no       | one-sentence "what a colleague should know"                            |
| `tags`       | no       | string array; free-form                                                |
| `outcome`    | no       | `shipped` \| `abandoned` \| `exploratory` \| `failed`                 |
| `visibility` | no       | `public` (default) \| `private`                                       |
| `forked_from`| no       | `id` of an ancestor session, if any                                    |
| `paths_touched` | no    | string array; populated from tool-call `args.path` on write            |
| `verbose`    | no       | boolean; when true, assistant turns may carry `text`                   |
| `merged_from`| no       | branch name a session was merged from (set by Cloud at merge time)     |
| `merged_at`  | no       | UTC RFC-3339; when the session landed on trunk via review              |
| `approved_by`| no       | reviewer handle / email who clicked "Merge into trunk"                 |

The `merged_from` / `merged_at` / `approved_by` trio is the
*review provenance* signal. They're set ONLY by Cloud's branch-merge
endpoint when a reviewed branch's session is appended into trunk's
prompts file — never by `spec prompts capture`. Sessions in trunk's
file with these fields populated render with the green-dot
"reviewed" chip in the feed UI; sessions without them are
direct-to-trunk captures and stay un-flagged. They're idempotent:
re-running merge on a session that's already stamped leaves the
existing values alone, so the original review attribution is never
overwritten.

All author-written fields stay author-written — the Cloud backend
never synthesises `title`, `summary`, `lesson`, or `outcome`. The
review provenance fields are the one exception: they're written by
Cloud, not the author.

### `[sessions.commit]` — optional, per-session

A v0.2 branch file accumulates many commits over time, so each
session can carry its own commit context. The fields mirror the
file-level `[commit]` block plus a `commit_sha`:

| Field             | Notes                                                  |
| ----------------- | ------------------------------------------------------ |
| `branch`          | branch name as known to git at capture time           |
| `commit_sha`      | commit the session was captured against               |
| `committed_at`    | UTC RFC-3339; commit timestamp                        |
| `message`         | commit message subject (optional)                      |
| `author_name`     | `git config user.name` at capture time                |
| `author_email`    | `git config user.email` at capture time               |
| `author_username` | Cloud handle if known                                  |

The block is omitted entirely when every field is `None` (typical on
files written before v0.2 introduced per-session attribution).

### `[[sessions.turns]]`

A session is an ordered array of turns. The compiler reads them in
array order; `at` is informational, not structural.

| role          | Required fields       | Optional fields                                    |
| ------------- | --------------------- | -------------------------------------------------- |
| `user`        | `role`, `text`        | `at`                                               |
| `assistant`   | `role`                | `at`, `model`, `summary`, `tool_calls`             |
| `tool_result` | reserved — disallowed in v0.1                            |

Assistant turns MUST NOT carry `text` unless the session's `verbose` is
true. Use `summary` for short descriptions; regenerate full text with
`spec prompts simulate`. Per-turn `text` (user, or assistant when
`verbose = true`) is capped (512 KiB in the reference implementation;
see `MAX_TURN_TEXT_CHARS` in `spec_cli.prompts.schema`).

Optional per-turn `model` (string, ≤128 chars in the reference validator)
records which LLM answered **that assistant turn**. User turns MUST NOT
include `model`; session-level `sessions.model` remains for backward
compatibility (typically first model seen in the thread).

### Tool calls

Every `[[turns.tool_calls]]` entry:

```toml
name = "ToolName"                      # required; see allowlist below
args = { … }                            # required; per-tool summary fields
status = "ok"                           # optional; ok | error
```

| Tool                  | Captured `args` fields                                   |
| --------------------- | -------------------------------------------------------- |
| `Read`                | `path`, optional `offset`, `limit`                       |
| `Glob`                | `pattern`, optional `target_directory`                   |
| `Grep`                | `pattern`, optional `path`, `glob`, `type`, `output_mode`|
| `Shell`               | `command` (truncated ≤200 chars), optional `cwd`, `exit` |
| `Write`               | `path`, `bytes` (size of content)                        |
| `Edit` / `StrReplace` | `path`, `old_head`, `new_head` (each ≤40 chars)          |
| `Delete`              | `path`                                                   |
| `WebFetch`            | `url`                                                    |
| `WebSearch`           | `search_term`                                            |
| `Task`                | `subagent_type`, `description`, optional `model`         |
| `TodoWrite`           | `todos` (full; already structured and small)             |

Hard rules (from §Determinism + §Privacy):

1. **No payloads.** File contents, stdout, diffs, grep results: never
   stored. The linkage to the repo is by path, not by content.
2. **Truncate aggressively.** Every free-text field is capped. Overflow
   is replaced with `…[truncated N chars]`.
3. **Secret scrub** applies to `command`, `url`, `title`, `summary`,
   `lesson`, `tags`, and any free-text field. Regex-based.
4. **Unknown tools are dropped** with a trailing comment. The validator
   warns; `--strict-unknown` escalates to an error.

### `[[edits]]` — append-only

```toml
[[edits]]
at       = 2026-04-21T09:00:00Z
by       = "alice@example.com"
sessions = ["d1714569-…"]              # which session ids were edited
turns    = [3, 7]                        # per-session turn indices affected
reason   = "Human-readable explanation."
```

Removing an entry is a hard validator error. Adding is free.

---

## Validation rules

A `.prompts` file is valid iff:

1. Parses as TOML.
2. `schema` is either absent or equals `spec.prompts/v0.1`.
3. Exactly one `[commit]` table.
4. `[commit].branch`, `author_name`, `author_email` are present and
   non-empty.
5. `[[sessions]]` contains at least one session.
6. Each session has `id` (non-empty, unique within the file) and
   `source` ∈ `{claude_code, cursor, manual}`.
7. Each session has at least one `[[turns]]` entry.
8. Every turn has a `role` in the allowed set; `user` has `text` (per-turn
   cap, 512 KiB in the reference validator) and no `model`; `assistant`
   has no `text` unless `verbose = true` (same cap on stored `text` when
   verbose). Assistant `model`, when present, is capped (128 chars in the
   reference validator).
9. `visibility` ∈ `{public, private}` if present (default `public`).
10. `outcome` ∈ the enum if present.
11. `tool_calls[].name` is in the allowlist OR dropped with a comment.
12. **Unknown keys anywhere are a hard error.** Typos must fail loud.

The validator lives in `spec_cli.prompts.schema` and is reused by
the compiler.

---

## Determinism

`spec prompts capture` must produce **byte-identical** files when
run twice with identical inputs. Concretely:

- Field order is fixed (writer enforces).
- Timestamps are normalised to UTC + `Z` suffix + second precision.
- Sessions within a file are sorted by `started_at` (ascending),
  `id` as a tiebreaker.
- Turns within a session are ordered by source-file arrival — never
  sorted on `at` alone, because sources that lack per-turn timestamps
  would become non-deterministic.
- Array values (`tags`, `paths_touched`) are emitted in source order
  and de-duplicated; the writer does not alphabetically sort, because
  human edits carry meaning in order.
- Never emit machine-generated "helpful" comments. A `.prompts` file
  has exactly one header comment, fixed text.

---

## Hand editing

Users SHOULD edit `.prompts` files. The rules:

- **Rewrite `text`** to tighten, redact, or clarify a user prompt. The
  compiler treats the new text as authoritative.
- **Write `title`, `summary`, `lesson`.** These are author-written and
  drive the `/prompts` feed.
- **Set `outcome` and `visibility`** deliberately. Default `visibility`
  is `public`; flip to `private` for sensitive sessions (the commit's
  file still needs to exist, but the feed skips that session).
- **Delete turns** to prune exploratory noise.
- **Insert new `[[sessions.turns]]` blocks** by hand. A hand-authored
  user turn is indistinguishable from a captured one — by design.
- **Add an `[[edits]]` entry** describing the change. Not enforced at
  write time, but the validator flags edits without a corresponding
  log entry (warn, not error).

Forbidden:

- **Deleting `[[edits]]` entries** — the log is append-only.
- **Changing a session's `id`** — ids are primary keys for cross-file
  references (forks, compile lineage, permalinks).
- **Removing `[commit].author_email`** — required for git/Cloud
  identity linkage.

---

## Visibility

Public by default. Per-session override.

- `visibility = "public"` (default) → session appears in `/prompts`
  and is linkable by permalink.
- `visibility = "private"` → session is still a build input (the
  compiler reads it the same way) but the feed and any public API
  refuse to surface it.
- A future **bundle-level private flag** (not yet modelled) will
  trump per-session visibility entirely.

The file itself is always committed to git. "Private" is a publish
gate, not a storage hide.

---

## Secret scrubbing

A regex pass runs at write time on:

- Every tool-call `args` string field (`command`, `url`, free text).
- Every session's `title`, `summary`, `lesson`, `tags[*]`.
- Every turn's `text` (user) and `summary` (assistant).

Patterns scrubbed (non-exhaustive): `sk-…`, `ghp_…`, `AKIA…`,
`Bearer …`, `eyJ…` (JWT), arbitrary `password=`, `token=`, `apikey=`
assignments. Matches are replaced with `[REDACTED]`. Defense in depth
— users may still paste tokens anywhere.

---

## Review lifecycle

Prompts are promoted through the review lifecycle using four CLI commands;
every state change is expressed as ordinary filesystem moves so the audit
log *is* the git history of the bundle.

```
  capture                submit                   review
┌──────────┐ prompts/   ┌──────────┐ prompts/     ┌──────────┐
│ agent    │──────────▶ │ captured │────────────▶ │ _pending │
│ store    │            └──────────┘              └────┬─────┘
└──────────┘                                           │
                                            accept ───┴─── reject
                                              │             │
                                              ▼             ▼
                                        ┌──────────┐     deleted
                                        │ curated  │     from worktree
                                        └──────────┘
```

1. **Capture** (`spec prompts capture`) pulls new sessions from the
   local agent store (Claude Code today) and writes them into
   `prompts/captured/`. Capture is idempotent across tiers — once a
   session id appears anywhere under `prompts/`, it won't be re-written.
2. **Submit** (`spec prompts submit <file>` or `--all-captured`)
   moves a file into `prompts/curated/_pending/`. The author commits the
   move and opens a PR; the pending file now appears in the PR's diff
   alongside whatever spec edits it accompanies.
3. **Review** (`spec prompts review`) walks each pending file on the
   reviewer's checkout of the PR. For each file the reviewer either
   **accepts** it (the file moves to `prompts/curated/`) or **rejects**
   it (the file is deleted from the worktree). Review only mutates the
   filesystem; the reviewer stages, commits, and pushes the result, so
   the acceptance or rejection is captured in normal git history.
4. **Check** (`spec prompts check --ci`) is a CI-friendly gate that
   exits non-zero whenever `prompts/curated/_pending/` is non-empty. Wire
   it as a GitHub Actions required status check and enable branch
   protection: a PR is mergeable only when every pending prompt has been
   accepted or rejected. Because rejection deletes the file, a rejected
   prompt never lands on the default branch — the code that merges is
   exactly the code the reviewer approved.

### GitHub Actions example

```yaml
# .github/workflows/spec-prompts.yml
name: spec prompts
on: [pull_request]
jobs:
  check:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - name: Install spec CLI
        # Do not `pip install spec-cli` — PyPI's "spec-cli" is a different
        # project. Same install as https://spec.lightreach.io/install
        run: |
          curl -LsSf https://spec.lightreach.io/install.sh | sh
          echo "$HOME/.local/bin" >> $GITHUB_PATH
      - run: spec prompts check --ci
```

In GitHub branch protection, mark `spec prompts / check` as a
required status check for the default branch. Done — no merge with
pending prompts.

---

## Capture flow

```
spec prompts capture   Discover new sessions from local Claude Code /
                            Cursor stores (not yet implemented). Writes
                            a single prompts/captured/<ISO-timestamp>.prompts
                            file containing every session captured in this
                            invocation.

  --since <iso>             Only sessions started after this time.
  --source claude_code|all  Restrict to one source. Default: all.
  --verbose                 Capture full assistant text. Marks the file
                            with `session.verbose = true` on each such
                            session. Off by default.
  --dry-run                 Print counts, don't write.

spec prompts validate  Run schema + determinism checks against every
                            .prompts file in the bundle. Exit 0 clean,
                            1 on any error.

  [<path>...]               Restrict to given paths.
  --strict-unknown          Treat unknown tool names as errors, not
                            warnings.

spec prompts simulate  Re-run a session through the compiler in a
                            read-only sandbox, up to the specified turn,
                            and print (or record) the simulated response.

  --session <id|path>       Session id or containing file. Required.
  --up-to-turn <n>          Stop after turn n. Default: last turn.
  --model <name>            Override compiler.model.
  --record                  POST the simulation record to Cloud under
                            the bundle's cloud.project (hashes + metadata
                            only).
  --no-tools                Refuse all tool calls — pure text reply.
  --dry-run                 Show the plan, don't call the model.
```

### `prompts capture` semantics (authoritative)

1. Discover candidate sessions from enabled sources (Claude Code,
   and in future Cursor).
2. Drop any session already present in any existing `.prompts` file in
   `prompts/` (by `id`). Capture is idempotent.
3. Read git context: `git rev-parse --abbrev-ref HEAD`, `git config
   user.name`, `git config user.email`. These seed `[commit]`.
4. Timestamp the output filename with `datetime.now(UTC)`.
5. Write `<bundle-root>/prompts/<timestamp>.prompts` with a `[commit]`
   block plus one `[[sessions]]` block per discovered session.
6. Never overwrite an existing file; abort if the timestamp collides
   (second-precision collisions require two captures in the same
   second — extremely rare, and the fix is "wait a second and re-run").

### `prompts simulate` semantics

Unchanged from previous spec. Simulations run in the **compiler** on
the user's machine, never in Cloud. Cloud only stores a `--record`
summary (hashes + metadata).

**Hard invariants:**

- Simulated responses NEVER land in committed `.prompts` files.
- Simulated responses NEVER trigger repo writes.
- Captured and simulated responses are distinct rows in Cloud with
  distinct UI treatments.

---

## Open questions (for review)

1. **Pre-commit hook vs explicit capture.** A hook could auto-write
   the `.prompts` file before every commit. For v0.1 capture is
   explicit; hook integration is a v0.2 comfort.
2. **Maximum file size.** Cap at N sessions / N KiB per `.prompts`?
   A commit with 50 sessions is unusual; probably a sign the user
   should have split the commit.
3. **Sidechain / sub-agent turns.** Claude Code emits
   `isSidechain: true` for Task-launched sub-agents. Fold into the
   parent session under a `[[turns.sidechains]]` nested array, or
   represent sub-agents as separate sessions with a
   `parent_session_id`? Leaning nested for v0.1 to avoid file
   explosion.
4. **File-level edits for redaction.** Today edits record per-session
   turn indices. If a user rewrites `[commit].author_name` (unlikely
   but possible), there's no session to pin to. A `"file"` sentinel
   for `sessions` in an `[[edits]]` entry is one shape; noted, not
   yet decided.
