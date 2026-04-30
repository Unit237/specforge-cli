import tomllib

from spec_cli.prompts.render import render_prompts_file
from spec_cli.prompts.schema import CommitMeta, PromptsFile, Session, Turn
from spec_cli.prompts.text_sanitize import sanitize_for_toml_text, strip_ansi_escapes


def test_strip_ansi_sgr():
    s = "hello \x1b[31mred\x1b[0m world"
    out = strip_ansi_escapes(s)
    assert "red" in out
    assert "\x1b" not in out


def test_sanitize_drops_c0_besides_whitespace():
    s = "a\x07b\nc"
    t = sanitize_for_toml_text(s)
    assert "\x07" not in t
    assert "\n" in t


def test_sanitize_for_toml_strips_ansi_for_multiline():
    s = "x\n\x1b[1mlong\x1b[0m\ny"
    t = sanitize_for_toml_text(s)
    assert "\x1b" not in t
    assert "long" in t


def test_render_long_user_text_with_ansi_produces_parseable_toml():
    bad = "line one\n\x1b[2J\x1b[0;0Hline two"
    pf = PromptsFile(
        commit=CommitMeta(
            branch="main",
            author_name="a",
            author_email="a@a.com",
        ),
        sessions=[
            Session(
                id="one",
                source="manual",
                turns=[Turn(role="user", text=bad)],
            )
        ],
    )
    body = render_prompts_file(pf)
    assert "\x1b" not in body
    tomllib.loads(body)
