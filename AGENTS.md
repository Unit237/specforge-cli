# Agent Instructions

When the user asks to capture the current Codex chat into Spec, including
phrases like `/capture`, `capture this chat`, or `send this chat to Spec`, run:

```bash
spec codex capture --index 1
```

If the local checkout is being used directly from source before the `spec`
console script is installed, use:

```bash
PYTHONPATH=src python3 -m spec_cli.cli codex capture --index 1
```

Use `--dry-run` first when the user asks to preview what would be captured.

<!-- >>> invariant-driven development >>>
## Invariant-driven development

Before fixing localized authorization, identity, lifecycle, privacy,
validation, recovery, retry, ranking, or cross-service behavior:

1. Search this repository and adjacent Lightreach systems for analogous cases.
2. Name the underlying rule and identify its owning boundary.
   Use `python3 architecture/tools/architecture.py context <changed-paths>`
   from the workspace root to load related invariants and architectural memory.
3. If multiple real manifestations share the semantics, change the versioned
   contract or canonical implementation and generate/adapt consumers.
4. Add conformance coverage and remove redundant local handling.
5. Do not generalize beyond evidence in the system; record intentionally
   distinct domain behavior as an explicit exception.
6. When the task reveals recurring conceptual pressure or an architectural
   decision, update architectural memory with durable evidence—not line-level
   task history.

When this repository is in the Lightreach workspace, consult
`architecture/repositories.json` and `architecture/invariants.json` at the
workspace root and run `python3 architecture/tools/architecture.py check`
after changing a registered invariant.
<!-- <<< invariant-driven development <<< -->
