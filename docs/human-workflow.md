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

`spec on` may write the server-minted immutable `cloud.bundle_id` and canonical
`owner/slug` to `spec.yaml`, then refreshes managed Codex/Claude/Cursor rules and
starts watchers. It sends no bundle file content. `spec publish` is the separate
content transfer; `spec push` remains a compatibility alias.

`spec activity` is the existing workspace-wide team stream under a shorter
name. It includes local Codex, Claude Code, Cursor, and Compress sessions. Tool
calls are still inspected by the critic when collapsed.

`spec prs` uses the signed-in GitHub CLI identity and deduplicates GitHub origins
across registered bundles. `spec review --with cloud` adds the explicit
`agent-review` label, allowing Actionairy to authorize a read-only, exact-SHA
Compress review. `--with @handle` resolves the candidate from the bundle's Spec
team and uses GitHub's requested-reviewer mechanism.
