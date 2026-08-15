# Spec's human workflow

```bash
spec discover                     # once; system-wide saved workspaces
spec on                           # connect + watch every registered project
spec status                       # Watching / Stopped / Needs connection
spec activity                     # all teammates and agent narration
spec activity --show-tool-runs    # expanded tools and code blocks
spec prs                          # your open PRs across watched repositories
spec review --with cloud          # your SHA-bound Compress reviewer
spec review --with @alice         # native GitHub request to a Spec teammate
spec publish                      # explicit staged bundle-content upload
spec off                          # stop local watchers; cloud jobs continue
```

Discovery stores workspace roots separately from bundle roots. Running it from
an explicit ROOT remembers that workspace; running it without ROOT later scans
all saved workspaces from any directory. Existing Spec repositories are
registered even when no initialization is needed.
Disposable `.codex-worktrees/` task branches are excluded; the stable checkout
remains the machine-wide watcher target.

New initialization is Git-rooted: `spec init` resolves
`git rev-parse --show-toplevel`, uses that root when invoked below it, and
refuses to write outside a Git worktree. Existing nested bundles remain
readable, but are not created accidentally.

`spec on` may write the server-minted immutable `cloud.bundle_id` and canonical
`owner/slug` to `spec.yaml`, then refreshes managed Codex/Claude/Cursor rules and
starts watchers. It sends no bundle file content. `spec publish` is the separate
content transfer; `spec push` remains a compatibility alias.
The first watcher start records existing local transcripts as a baseline and
streams only later turns, preventing a historical replay storm. Historical
capture remains an explicit `spec prompts capture` action.

`spec activity` is the existing workspace-wide team stream under a shorter
name. `spec watch` consumes the same Cloud workspace stream. A solo user sees
all of their own local Codex, Claude Code, Cursor, and Compress sessions;
accepted teammates see all sessions authored by one another. Unrelated users
remain limited to conversations in bundles they can already read. Tool calls
are still inspected by the critic when collapsed.

`spec prs` uses the signed-in GitHub CLI identity and deduplicates GitHub origins
across registered bundles. `spec review --with cloud` adds the explicit
`agent-review` label, allowing Actionairy to authorize a read-only, exact-SHA
Compress review. `--with @handle` resolves the candidate from the bundle's Spec
team and uses GitHub's requested-reviewer mechanism.
