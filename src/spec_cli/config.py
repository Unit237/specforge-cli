"""
Reads/writes the two config surfaces the CLI owns:

  1. `spec.yaml` at the bundle root — the manifest the compiler and the
     cloud both consume. We only load/dump it here; validation lives in the
     compiler (source of truth for the schema). Known YAML merge typos
     (e.g. ``output:hropic``) are auto-repaired on load: the original file is
     copied to ``.spec/spec.yaml.invalid-backup`` and a canonical ``spec.yaml``
     is rewritten unless ``SPEC_NO_MANIFEST_AUTOWRITE`` is set.

  2. `~/.spec/credentials` — a JSON file holding the Spec session token,
     the Cloud API base URL, and the signed-in user's public handle.
     0600 perms. For CI (GitHub Actions, cron on a headless host), you can
     instead set ``SPEC_ACCESS_TOKEN`` (and optionally ``SPEC_API``,
     ``SPEC_USER_HANDLE``) in the environment — those override the file
     when present.

Everything path-related is resolved from the bundle root, which we find by
walking up from cwd (or ``SPEC_BUNDLE_ROOT``), — when inside a git repo
without a parent ``spec.yaml`` — by discovering tracked bundle manifests
under the worktree (same rules as git hooks), and — when cwd is a parent
of a nested bundle (wrapper checkout) — by scanning descendants for
``spec.yaml``.
"""

from __future__ import annotations

import json
import logging
import os
import re
import stat
import subprocess
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlparse

import yaml

from .constants import MANIFEST_FILENAME

log = logging.getLogger(__name__)


class BundleNotFoundError(FileNotFoundError):
    """Raised when no `spec.yaml` is found in cwd or any parent."""


class ManifestYamlError(ValueError):
    """``spec.yaml`` exists but is not valid YAML (and no autorepair applied)."""

    def __init__(self, path: Path, message: str):
        super().__init__(message)
        self.path = path


_MERGED_OUTPUT_TYPO_KEY_RE = re.compile(
    r"(?m)^(?P<indent>[ \t]*)output:hropic[ \t]*$"
)
# Placeholder must be a valid YAML mapping key and unlikely to collide.
_AUTOREPAIR_PLACEHOLDER_KEY = "spec_cli_autorepair__merged_output_typo"


def _try_repair_manifest_yaml(raw: str) -> tuple[dict[str, Any], str] | None:
    """Handle a known ``spec.yaml`` corruption: ``output:hropic`` line.

    Editors / merges occasionally concatenate ``output:`` + ``anthropic`` into
    one token, leaving the following ``model`` / ``temperature`` lines
    indented under an invalid key so PyYAML raises. We remap those fields
    back onto ``compiler`` and ``output`` heuristically.
    """
    if not _MERGED_OUTPUT_TYPO_KEY_RE.search(raw):
        return None
    patched = _MERGED_OUTPUT_TYPO_KEY_RE.sub(
        lambda m: f"{m.group('indent')}{_AUTOREPAIR_PLACEHOLDER_KEY}:",
        raw,
        count=1,
    )
    try:
        data = yaml.safe_load(patched) or {}
    except yaml.YAMLError:
        return None
    if not isinstance(data, dict):
        return None
    pod = data.pop(_AUTOREPAIR_PLACEHOLDER_KEY, None)
    if not isinstance(pod, dict):
        return None

    compiler = data.get("compiler")
    if not isinstance(compiler, dict):
        compiler = {}
        data["compiler"] = compiler
    output = data.get("output")
    if not isinstance(output, dict):
        output = {}
        data["output"] = output

    for key in ("engine", "model", "temperature", "max_output_tokens"):
        if key in pod:
            compiler[key] = pod.pop(key)
    for key in ("target", "changelog", "commit_style"):
        if key in pod:
            output[key] = pod.pop(key)
    # Anything left (unknown keys that lived under the typo line) lands on
    # ``output`` so we do not drop user data silently.
    for k, v in pod.items():
        output[k] = v

    note = (
        'merged invalid key "output:hropic" → `compiler` (model/engine/…) '
        "and `output` (target/changelog/…)"
    )
    return data, note


def _raise_manifest_yaml_error(path: Path, raw: str, err: yaml.YAMLError) -> None:
    """Turn a raw PyYAML traceback into a short, path-centric CLI error."""
    hint = str(err).strip()
    mark = getattr(err, "problem_mark", None)
    if mark is not None:
        lines = raw.splitlines()
        idx = mark.line
        if 0 <= idx < len(lines):
            bad = lines[idx].rstrip()
            pointer = f"{' ' * mark.column}^"
            hint = (
                f"{hint}\n  {path}:{mark.line + 1}: {bad}\n  {pointer}\n"
                "  Hint: look for a merged token like `output:hropic` (should be "
                "separate `compiler:` / `output:` blocks)."
            )
    raise ManifestYamlError(path, f"Invalid YAML in {path}:\n{hint}") from err


def _persist_manifest_repair(
    root: Path, path: Path, original_raw: str, data: dict[str, Any], note: str
) -> None:
    """Backup broken YAML, then write a canonical repaired ``spec.yaml``."""
    bdir = root / ".spec"
    bdir.mkdir(parents=True, exist_ok=True)
    backup = bdir / "spec.yaml.invalid-backup"
    backup.write_text(original_raw, encoding="utf-8")
    dump_manifest(Manifest(path=path, data=data))
    log.warning(
        "spec.yaml: auto-repaired known corruption (%s). "
        "Original saved to %s — review formatting and run `git diff spec.yaml`.",
        note,
        backup,
    )


@dataclass
class Manifest:
    path: Path
    data: dict[str, Any] = field(default_factory=dict)

    @property
    def name(self) -> str | None:
        return self.data.get("name")

    @property
    def cloud_project(self) -> str | None:
        cloud = self.data.get("cloud") or {}
        return cloud.get("project")

    @property
    def cloud_bundle_id(self) -> str | None:
        """Stable, server-minted bundle identity (``cloud.bundle_id`` in
        ``spec.yaml``).

        This is the half of the manifest the user *cannot* sensibly
        edit — it's stamped by the CLI on the first successful push and
        verified on every subsequent push so a working tree bound to
        bundle A can never be retargeted to bundle B by editing
        ``cloud.project``. ``None`` when missing — older manifests
        won't carry it, and the first-push adoption flow is what fills
        it in. See ``ensure_bundle_id_binding`` for the verify/adopt
        logic and PLAN.md §11 for the design rationale.
        """
        cloud = self.data.get("cloud") or {}
        value = cloud.get("bundle_id")
        if not isinstance(value, str):
            return None
        cleaned = value.strip()
        return cleaned or None

    def set_cloud_bundle_id(self, bundle_id: str) -> None:
        """Write ``cloud.bundle_id`` into the in-memory manifest.

        Caller is responsible for persisting via ``dump_manifest``. We
        keep the mutation explicit (no auto-save) so the push pipeline
        can decide *when* the on-disk file changes — the adopt-on-first-
        push flow only writes after the upload succeeds, so a failed
        push doesn't leave the manifest in a confusing half-bound state.
        """
        cloud = self.data.get("cloud")
        if not isinstance(cloud, dict):
            cloud = {}
            self.data["cloud"] = cloud
        cloud["bundle_id"] = bundle_id

    def set_cloud_project(self, project: str) -> None:
        """Write ``cloud.project`` (bare slug or ``<handle>/<slug>``)."""
        cloud = self.data.get("cloud")
        if not isinstance(cloud, dict):
            cloud = {}
            self.data["cloud"] = cloud
        cloud["project"] = project

    @property
    def prompt_stream(self) -> dict[str, Any]:
        """Spec Live (real-time prompt sharing) settings.

        Read from ``cloud.prompt_stream`` in ``spec.yaml``. Accepted
        shapes:

        * Missing / ``null``          → default ON, summary-only.
        * Boolean ``true``/``false``  → ``{"enabled": <bool>, "verbose": False}``.
        * ``"enabled"`` / ``"on"`` / ``"true"`` (string) → ON.
        * ``"disabled"`` / ``"off"`` / ``"false"`` (string) → OFF.
        * Mapping ``{enabled, verbose}`` → explicit fine-grained control.

        **Default is ON.** Spec Live works the moment a teammate
        installs the CLI — the value-prop of "your team feed lights up
        as you type" disappears the second we make it opt-in. Privacy
        is preserved by (a) per-user mute via ``spec live mute`` (see
        ``spec_cli.preferences``), (b) unconditional secret redaction
        on every outbound payload, and (c) summary-only assistant
        bodies unless ``verbose: true`` is explicitly set.

        Set ``cloud.prompt_stream: disabled`` (or run ``spec live off``)
        to turn it off for the whole bundle.
        """
        cloud = self.data.get("cloud") or {}
        raw = cloud.get("prompt_stream") if isinstance(cloud, dict) else None

        # Default policy as of Spec Live v0.4: when broadcasting is on,
        # ship the full assistant ``text`` body as well as the summary.
        # ``spec team watch`` is a real-time review tool and a reviewer
        # cannot debug what they cannot read. Teams that want the old
        # privacy posture can set ``verbose: false`` explicitly — the
        # property still honours that opt-out.
        enabled = True
        verbose = True
        if raw is None:
            return {"enabled": enabled, "verbose": verbose}
        if isinstance(raw, bool):
            enabled = raw
        elif isinstance(raw, str):
            value = raw.strip().lower()
            if value in {"disabled", "off", "false", "no", "0"}:
                enabled = False
            elif value in {"enabled", "on", "true", "yes", "1"}:
                enabled = True
            # Anything else falls through to the default ON.
        elif isinstance(raw, dict):
            enabled = bool(raw.get("enabled", True))
            # Explicit ``verbose`` key wins. Absent key inherits the
            # verbose-by-default policy above so existing manifests
            # that pre-date the flip still benefit from the change.
            verbose = bool(raw.get("verbose", True))
        return {"enabled": enabled, "verbose": verbose}

    def set_cloud_prompt_stream(self, *, enabled: bool, verbose: bool | None = None) -> None:
        """Write ``cloud.prompt_stream`` to the in-memory manifest.

        Caller persists via ``dump_manifest``. We always write the
        explicit mapping form so the state in ``spec.yaml`` is
        unambiguous to a human reader — the implicit default is what
        shipped users get, and the explicit form is what shows up
        once anyone has flipped the toggle.
        """
        cloud = self.data.get("cloud")
        if not isinstance(cloud, dict):
            cloud = {}
            self.data["cloud"] = cloud
        if verbose is None:
            cloud["prompt_stream"] = {"enabled": bool(enabled)}
        else:
            cloud["prompt_stream"] = {
                "enabled": bool(enabled),
                "verbose": bool(verbose),
            }

    @property
    def prompt_stream_enabled(self) -> bool:
        """Convenience: is broadcasting opted in for this bundle?"""
        return bool(self.prompt_stream.get("enabled"))

    @property
    def prompt_stream_verbose(self) -> bool:
        """Convenience: should the watcher broadcast assistant full text?"""
        return bool(self.prompt_stream.get("verbose"))

    @property
    def root(self) -> Path:
        return self.path.parent


def _is_bundle_manifest_file(manifest_path: Path) -> bool:
    """True when ``manifest_path`` looks like a Spec bundle (not a random yaml)."""
    try:
        with manifest_path.open("r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
    except (OSError, UnicodeDecodeError, yaml.YAMLError):
        return False
    if not isinstance(data, dict):
        return False
    schema = data.get("schema")
    return isinstance(schema, str) and schema.startswith("spec/")


def discover_bundle_roots_under_git_root(git_root: Path) -> list[Path]:
    """Directories under ``git_root`` that contain a tracked bundle ``spec.yaml``."""
    try:
        result = subprocess.run(
            ["git", "-C", str(git_root), "ls-files"],
            capture_output=True,
            text=True,
            check=False,
            timeout=120,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if result.returncode != 0:
        return []
    roots: list[Path] = []
    seen: set[Path] = set()
    for line in (result.stdout or "").splitlines():
        line = line.strip().replace("\\", "/")
        if not line.endswith(MANIFEST_FILENAME):
            continue
        if PurePosixPath(line).name != MANIFEST_FILENAME:
            continue
        manifest = (git_root / line).resolve()
        parent = manifest.parent
        if parent in seen:
            continue
        if not _is_bundle_manifest_file(manifest):
            continue
        seen.add(parent)
        roots.append(parent)
    return sorted(roots, key=lambda p: str(p))


# Descendant scan: skip heavy / irrelevant dirs (performance + false positives).
_BUNDLE_DESCEND_SKIP_DIR_NAMES: frozenset[str] = frozenset(
    {
        ".git",
        "node_modules",
        ".venv",
        "venv",
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        ".tox",
        "dist",
        "build",
        ".next",
        ".turbo",
        "out",
        ".spec",
    }
)

_DEFAULT_BUNDLE_DESCEND_MAX_DEPTH = 8


def _bundle_descend_max_depth() -> int:
    raw = os.environ.get("SPEC_BUNDLE_DESCEND_MAX_DEPTH", "").strip()
    if not raw:
        return _DEFAULT_BUNDLE_DESCEND_MAX_DEPTH
    try:
        depth = int(raw)
    except ValueError:
        return _DEFAULT_BUNDLE_DESCEND_MAX_DEPTH
    return max(1, min(depth, 32))


def discover_bundle_roots_under_cwd(
    start: Path,
    *,
    max_depth: int | None = None,
) -> list[Path]:
    """Bundle roots with a valid ``spec.yaml`` somewhere under ``start``.

    Does not search above ``start``. Used when the shell cwd is a wrapper
    folder that contains a nested Spec bundle (e.g. ``~/project/spec/`` with
    the manifest at ``~/project/spec/spec/spec.yaml``).
    """
    here = start.resolve()
    limit = max_depth if max_depth is not None else _bundle_descend_max_depth()
    found: list[Path] = []
    seen: set[Path] = set()

    def _record(directory: Path) -> None:
        manifest = directory / MANIFEST_FILENAME
        if not manifest.is_file() or not _is_bundle_manifest_file(manifest):
            return
        root = directory.resolve()
        if root in seen:
            return
        seen.add(root)
        found.append(root)

    def _walk(directory: Path, depth: int) -> None:
        if depth > limit:
            return
        _record(directory)
        # Do not descend into a directory that is already a bundle root —
        # bundle content (docs/, prompts/) is not another bundle.
        if (directory / MANIFEST_FILENAME).is_file():
            return
        try:
            children = sorted(directory.iterdir(), key=lambda p: p.name)
        except OSError:
            return
        for child in children:
            if not child.is_dir():
                continue
            if child.name in _BUNDLE_DESCEND_SKIP_DIR_NAMES:
                continue
            _walk(child, depth + 1)

    _walk(here, 0)
    return sorted(found, key=lambda p: (len(p.parts), str(p)))


def _prefer_bundle_root(
    candidates: list[Path],
    here: Path,
    *,
    git_root: Path | None = None,
) -> Path:
    """Pick one bundle when several manifests match the same cwd context."""
    unique = []
    seen: set[Path] = set()
    for c in candidates:
        r = c.resolve()
        if r not in seen:
            seen.add(r)
            unique.append(r)
    if len(unique) == 1:
        return unique[0]
    if git_root is not None:
        preferred = (git_root / "spec").resolve()
        if preferred in unique:
            return preferred
    spec_child = (here.resolve() / "spec").resolve()
    if spec_child in unique:
        return spec_child

    here_res = here.resolve()

    def _depth_from_here(bundle: Path) -> tuple[int, str]:
        try:
            rel = bundle.relative_to(here_res)
            return (len(rel.parts), str(bundle))
        except ValueError:
            return (9999, str(bundle))

    shallowest = min(_depth_from_here(b) for b in unique)[0]
    tied = [b for b in unique if _depth_from_here(b)[0] == shallowest]
    if len(tied) == 1:
        return tied[0]
    rels: list[str] = []
    for b in tied[:8]:
        try:
            rels.append(str(b.relative_to(here_res)))
        except ValueError:
            rels.append(str(b))
    tail = "…" if len(tied) > 8 else ""
    listed = ", ".join(rels) + tail
    raise BundleNotFoundError(
        f"Multiple Spec bundles under {here} ({listed}). "
        f"Export SPEC_BUNDLE_ROOT to the bundle directory you want, then retry."
    )


def find_bundle_root(start: Path | None = None) -> Path:
    """Resolve the bundle directory containing ``spec.yaml``.

    Resolution order:

    1. ``SPEC_BUNDLE_ROOT`` — absolute path to the bundle when set (multi-
       bundle monorepos, or when ``cwd`` is outside the tree).
    2. Walk upward from ``start`` / the current working directory until a
       ``spec.yaml`` is found (classic single-bundle layout).
    3. If still missing, use the git worktree root and scan **tracked**
       ``spec.yaml`` files (same discovery as git hooks).
    4. If still missing, scan **descendants** of ``cwd`` for ``spec.yaml``
       (parent-folder / wrapper checkout layouts).
    5. When step 3 or 4 finds several bundles, prefer ``<repo>/spec`` or
       ``<cwd>/spec`` when present; otherwise the shallowest match; else
       raise with a hint to set ``SPEC_BUNDLE_ROOT``.
    """
    here = (start or Path.cwd()).resolve()

    env_root = os.environ.get("SPEC_BUNDLE_ROOT", "").strip()
    if env_root:
        p = Path(env_root).expanduser().resolve()
        if (p / MANIFEST_FILENAME).is_file():
            return p
        raise BundleNotFoundError(
            f"SPEC_BUNDLE_ROOT is set to {p} but there is no {MANIFEST_FILENAME} there."
        )

    for candidate in [here, *here.parents]:
        if (candidate / MANIFEST_FILENAME).is_file():
            return candidate

    from .git import repo_toplevel

    gt = repo_toplevel(here)
    if gt is not None:
        roots = discover_bundle_roots_under_git_root(gt)
        if roots:
            return _prefer_bundle_root(roots, here, git_root=gt)

    descend = discover_bundle_roots_under_cwd(here)
    if descend:
        return _prefer_bundle_root(descend, here, git_root=gt)

    raise BundleNotFoundError(
        f"No {MANIFEST_FILENAME} found in {here}, any parent, or descendants "
        f"(depth ≤ {_bundle_descend_max_depth()}). "
        "Run `spec init` to scaffold one, or `cd` into the bundle directory."
    )


def load_manifest(root: Path | None = None) -> Manifest:
    root = root or find_bundle_root()
    path = root / MANIFEST_FILENAME
    raw = path.read_text(encoding="utf-8")
    skip_disk = os.environ.get("SPEC_NO_MANIFEST_AUTOWRITE", "").strip().lower() in (
        "1",
        "true",
        "yes",
    )
    try:
        data = yaml.safe_load(raw) or {}
    except yaml.YAMLError as e:
        repaired = _try_repair_manifest_yaml(raw)
        if repaired is None:
            _raise_manifest_yaml_error(path, raw, e)
        data, note = repaired
        if skip_disk:
            log.warning(
                "spec.yaml: in-memory autorepair applied (%s). "
                "Unset SPEC_NO_MANIFEST_AUTOWRITE to persist the fix to disk "
                "(backup + rewritten spec.yaml).",
                note,
            )
        else:
            _persist_manifest_repair(root, path, raw, data, note)
    if not isinstance(data, dict):
        raise ValueError(f"{path} must be a YAML mapping at the top level")
    return Manifest(path=path, data=data)


def dump_manifest(manifest: Manifest) -> None:
    with manifest.path.open("w", encoding="utf-8") as f:
        yaml.safe_dump(
            manifest.data,
            f,
            sort_keys=False,
            default_flow_style=False,
            allow_unicode=True,
        )


# ---------------------------------------------------------------------------
# Credentials (~/.spec/credentials)
# ---------------------------------------------------------------------------


def _creds_dir() -> Path:
    override = os.environ.get("SPEC_HOME")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".spec"


def _creds_path() -> Path:
    return _creds_dir() / "credentials"


def default_api_base() -> str:
    return os.environ.get("SPEC_API", "https://spec.lightreach.io")


@dataclass
class Credentials:
    """What the CLI persists to ``~/.spec/credentials`` after login.

    ``access_token`` is the Spec session JWT (the same one the web app
    sends as ``Authorization: Bearer …``); ``user_handle`` is the public
    namespace prefix Cloud assigned this account, captured at login
    time so ``cloud.project: <slug>`` (without a handle) can fall back
    to it. The legacy ``refresh_token`` field stays in the dataclass
    for back-compat with previously-written credential files; we don't
    use it any more (the device-flow broker hands out a long-lived JWT
    instead of a refresh-token pair).
    """

    api_base: str
    access_token: str | None = None
    refresh_token: str | None = None
    user_email: str | None = None
    user_name: str | None = None
    user_handle: str | None = None


def load_credentials() -> Credentials | None:
    """Load saved session credentials.

    When ``SPEC_ACCESS_TOKEN`` is set (non-empty), returns in-memory
    credentials from the environment so CI jobs never have to write
    ``~/.spec/credentials``. Otherwise reads ``~/.spec/credentials``.
    """
    token = os.environ.get("SPEC_ACCESS_TOKEN", "").strip()
    if token:
        api = os.environ.get("SPEC_API", "").strip()
        return Credentials(
            api_base=api or default_api_base(),
            access_token=token,
            refresh_token=os.environ.get("SPEC_REFRESH_TOKEN", "").strip() or None,
            user_email=os.environ.get("SPEC_USER_EMAIL", "").strip() or None,
            user_name=os.environ.get("SPEC_USER_NAME", "").strip() or None,
            user_handle=os.environ.get("SPEC_USER_HANDLE", "").strip() or None,
        )
    path = _creds_path()
    if not path.is_file():
        return None
    with path.open("r", encoding="utf-8") as f:
        raw = json.load(f)
    return Credentials(
        api_base=raw.get("api_base") or default_api_base(),
        access_token=raw.get("access_token"),
        refresh_token=raw.get("refresh_token"),
        user_email=raw.get("user_email"),
        user_name=raw.get("user_name"),
        user_handle=raw.get("user_handle"),
    )


def save_credentials(creds: Credentials) -> Path:
    d = _creds_dir()
    d.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(d, stat.S_IRWXU)  # 0700
    except OSError:
        pass

    path = _creds_path()
    payload = {
        "api_base": creds.api_base,
        "access_token": creds.access_token,
        "refresh_token": creds.refresh_token,
        "user_email": creds.user_email,
        "user_name": creds.user_name,
        "user_handle": creds.user_handle,
    }
    with path.open("w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
    try:
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)  # 0600
    except OSError:
        pass
    return path


def clear_credentials() -> bool:
    path = _creds_path()
    if path.exists():
        path.unlink()
        return True
    return False


# ---------------------------------------------------------------------------
# Remote URL parsing
# ---------------------------------------------------------------------------
#
# `spec push https://spec.lightreach.io/<handle>/<slug>.git` — git-style
# one-shot remotes. The URL carries the Cloud host plus the *two-part*
# `<handle>/<slug>` identifier the server actually resolves on. Mirrors
# the GitHub URL shape exactly so muscle memory carries over.
#
# Rules, kept deliberately boring:
#   scheme     · http or https only (session tokens never travel over
#                anything else).
#   host       · becomes the Cloud API base for this invocation
#                (overrides `SPEC_API` and the `api_base` in saved
#                credentials).
#   path       · exactly two segments: ``<handle>/<slug>``. Trailing
#                ``.git`` stripped. More segments fail loudly so a
#                pasted nested URL routes somewhere obvious instead of
#                being silently flattened into a slug.
#   query/frag · rejected; they have no meaning here and a silent drop
#                would be a footgun.


class RemoteUrlError(ValueError):
    """The URL handed to push/pull can't be interpreted as a Cloud remote."""


# Handle + slug syntactic checks (kept intentionally lax — the server is
# the source of truth on availability, this is just "shape looks right").
_HANDLE_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,37}[a-z0-9])?$")
_SLUG_RE = re.compile(r"^[a-z0-9](?:[a-z0-9._-]{0,254}[a-z0-9])?$", re.IGNORECASE)


@dataclass
class RemoteTarget:
    """Resolved push/pull target, explicit about which fields came from the URL.

    ``handle`` and ``slug`` are split — they're the two segments after
    the host. ``raw_url`` keeps the original string for log lines /
    error messages so the user always sees what they typed back.
    """

    api_base: str
    handle: str
    slug: str
    raw_url: str


def parse_remote_url(url: str) -> RemoteTarget:
    """Parse ``https://host/<handle>/<slug>(.git)?`` into its parts.

    Raises ``RemoteUrlError`` with a user-facing message on any
    malformed input.
    """
    if not url or not isinstance(url, str):
        raise RemoteUrlError("Remote URL is empty.")

    parsed = urlparse(url.strip())
    if parsed.scheme not in ("http", "https"):
        raise RemoteUrlError(
            f"Remote URL must be http(s), got: {url!r}. "
            "Example: https://spec.lightreach.io/acme/billing.git"
        )
    if not parsed.netloc:
        raise RemoteUrlError(f"Remote URL is missing a host: {url!r}")
    if parsed.query or parsed.fragment:
        raise RemoteUrlError(
            f"Remote URL must not carry query or fragment: {url!r}"
        )

    path = parsed.path.strip("/")
    if path.endswith(".git"):
        path = path[: -len(".git")]
    path = path.strip("/")
    if not path:
        raise RemoteUrlError(
            f"Remote URL is missing the <handle>/<slug> path: {url!r}. "
            "Example: https://spec.lightreach.io/acme/billing.git"
        )

    parts = path.split("/")
    if len(parts) == 1:
        raise RemoteUrlError(
            f"Remote URL is missing a handle: {url!r}. "
            "URLs are now `<host>/<handle>/<slug>` — the GitHub shape. "
            "Example: https://spec.lightreach.io/acme/billing.git"
        )
    if len(parts) > 2:
        raise RemoteUrlError(
            f"Remote URL must be exactly `<host>/<handle>/<slug>`, got "
            f"{url!r}. If your handle has a `/` in it, that's not "
            "supported — handles are a single segment."
        )

    handle, slug = parts[0].lower(), parts[1]
    if not _HANDLE_RE.match(handle):
        raise RemoteUrlError(
            f"Handle `{handle}` doesn't look right. Use 1–39 lowercase "
            "letters, digits, and single hyphens (no leading/trailing hyphen)."
        )
    if not _SLUG_RE.match(slug):
        raise RemoteUrlError(
            f"Slug `{slug}` doesn't look right. Use letters, digits, "
            "dots, underscores, and hyphens."
        )

    api_base = f"{parsed.scheme}://{parsed.netloc}"
    return RemoteTarget(api_base=api_base, handle=handle, slug=slug, raw_url=url)


def parse_cloud_project(
    raw: str, *, default_handle: str | None = None
) -> tuple[str, str]:
    """Parse the ``cloud.project`` value from ``spec.yaml``.

    Two accepted forms:

    - ``<handle>/<slug>`` — preferred, fully qualified.
    - ``<slug>`` — legacy. Resolved against ``default_handle``
      (typically the signed-in user's handle from saved credentials).
      Raises ``RemoteUrlError`` if no handle can be derived, so users
      get a clear message instead of a confusing 404 from Cloud.

    Returns ``(handle, slug)``.
    """
    raw = (raw or "").strip()
    if not raw:
        raise RemoteUrlError(
            "`cloud.project` is empty. Set it to `<handle>/<slug>` "
            "(e.g. `acme/billing`)."
        )

    if "/" in raw:
        handle, _, slug = raw.partition("/")
        handle = handle.lower()
        if not _HANDLE_RE.match(handle):
            raise RemoteUrlError(
                f"`cloud.project: {raw}` — handle `{handle}` is malformed."
            )
        if not slug or not _SLUG_RE.match(slug):
            raise RemoteUrlError(
                f"`cloud.project: {raw}` — slug `{slug}` is malformed."
            )
        return handle, slug

    # Bare slug — fall back to the signed-in user's handle.
    if not default_handle:
        raise RemoteUrlError(
            f"`cloud.project: {raw}` is missing a handle and we don't "
            "know who you are yet. Either rewrite it as "
            f"`<handle>/{raw}` or run `spec login` so the CLI can "
            "fall back to your handle."
        )
    if not _HANDLE_RE.match(default_handle):
        raise RemoteUrlError(
            f"Saved handle `{default_handle}` is malformed. "
            "Re-run `spec login`."
        )
    if not _SLUG_RE.match(raw):
        raise RemoteUrlError(
            f"`cloud.project: {raw}` — slug looks malformed."
        )
    return default_handle, raw
