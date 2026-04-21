from spec_cli.constants import (
    MANIFEST_FILENAME,
    MAX_BATCH_SIZE,
    PROMPTS_DIRNAME,
    SPEC_EXTENSIONS,
    classify,
    is_spec_file,
)


def test_allow_list_values():
    assert SPEC_EXTENSIONS == frozenset({".md", ".markdown", ".prompts"})
    assert MANIFEST_FILENAME == "spec.yaml"
    assert PROMPTS_DIRNAME == "prompts"
    assert MAX_BATCH_SIZE == 10


def test_is_spec_file_accepts_md():
    assert is_spec_file("docs/product.md")
    assert is_spec_file("deep/nest/notes.markdown")
    assert is_spec_file("spec.yaml")


def test_is_spec_file_accepts_prompts():
    # `.prompts` files are first-class bundle inputs (one per commit).
    assert is_spec_file("prompts/2026-04-21T11-47-35Z.prompts")
    assert is_spec_file("anywhere/else.prompts")


def test_is_spec_file_rejects_legacy_singular_prompt():
    # The old `.prompt` extension is no longer accepted. One file per
    # commit lives in a `.prompts` (plural) file.
    assert not is_spec_file("prompts/sessions/legacy.prompt")


def test_is_spec_file_rejects_code():
    assert not is_spec_file("src/app.py")
    assert not is_spec_file("index.ts")
    assert not is_spec_file("logo.png")
    assert not is_spec_file("spec.yml")  # only .yaml is valid


def test_classify_settings_prompts_md():
    assert classify("spec.yaml") == "settings"
    assert classify("prompts/2026-04-21T11-47-35Z.prompts") == "prompts"
    assert classify("docs/product.md") == "md"


def test_classify_prompts_ext_anywhere():
    # `.prompts` is `prompts` regardless of where it lives.
    assert classify("notes/scratch.prompts") == "prompts"
    assert classify("deeply/nested/thing.prompts") == "prompts"


def test_classify_md_under_prompts_is_md():
    # `.md` under `prompts/` is no longer magically re-classified.
    # The stack rejects it at push time (wrong place for .md, we don't
    # want anyone accidentally shipping prompt-prose as spec), but for
    # pure classification purposes an `.md` is an `md`.
    assert classify("prompts/something.md") == "md"
