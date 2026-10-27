"""The chronobox guide in a project's AGENTS.md (no VM)."""

from pathlib import Path

from chronobox.config import load_config
from chronobox.guide import AGENTS_FILE, CLAUDE_FILE, END_MARKER, START_MARKER, write_guide

TOML = '[[service]]\nname = "db"\nstart = "postgres"\n'


def _project(folder: Path) -> Path:
    """A project with one service."""
    (folder / "chronobox.toml").write_text(TOML)
    return folder


def test_a_new_guide_names_the_project_services_and_folder(tmp_path: Path) -> None:
    """Without an AGENTS.md, one is created holding only the guide, with the project's services."""
    result = write_guide(load_config(_project(tmp_path)))
    text = (tmp_path / AGENTS_FILE).read_text()
    assert result.created and not result.claude_hint
    assert text.startswith(START_MARKER) and text.rstrip().endswith(END_MARKER)
    assert "services (db)" in text and "`/src`" in text and "verify_script" in text
    assert "Change the machine, or hand over a script?" in text and "ask; if nobody can answer" in text


def test_a_project_without_hand_offs_gets_no_hand_off_guide(tmp_path: Path) -> None:
    """``[handoff] enabled = false``: the guide only tells agents how to change the machine."""
    (tmp_path / "chronobox.toml").write_text(TOML + "[handoff]\nenabled = false\n")
    write_guide(load_config(tmp_path))
    text = (tmp_path / AGENTS_FILE).read_text()
    assert "verify_script" not in text and "hand over" not in text and "Trying things safely" in text


def test_the_guide_is_added_to_an_existing_file_and_replaced_in_place(tmp_path: Path) -> None:
    """The project's own AGENTS.md text is kept around the guide; writing again replaces only the guide
    (here: a new service shows up), never duplicating it."""
    folder = _project(tmp_path)
    (folder / AGENTS_FILE).write_text("# Our rules\n\nRun make lint.\n")
    write_guide(load_config(folder))
    (folder / AGENTS_FILE).write_text((folder / AGENTS_FILE).read_text() + "\n## After\n")
    (folder / "chronobox.toml").write_text(TOML + '[[service]]\nname = "app"\nstart = "x"\n')
    result = write_guide(load_config(folder))
    text = (folder / AGENTS_FILE).read_text()
    assert not result.created and text.count(START_MARKER) == 1
    assert text.startswith("# Our rules\n\nRun make lint.\n") and text.rstrip().endswith("## After")
    assert "services (db, app)" in text


def test_a_claude_md_without_the_import_gets_a_hint_not_an_edit(tmp_path: Path) -> None:
    """Claude Code reads CLAUDE.md instead of AGENTS.md when both exist: the user is told to import it,
    and CLAUDE.md is left alone; once it imports AGENTS.md there is no hint."""
    folder = _project(tmp_path)
    (folder / CLAUDE_FILE).write_text("Be nice.\n")
    assert write_guide(load_config(folder)).claude_hint
    assert (folder / CLAUDE_FILE).read_text() == "Be nice.\n"
    (folder / CLAUDE_FILE).write_text("Be nice.\n@AGENTS.md\n")
    assert not write_guide(load_config(folder)).claude_hint


def test_markers_out_of_order_get_a_fresh_guide_instead_of_a_crash(tmp_path: Path) -> None:
    """An end marker above the start marker (a hand edit) is not a section: the guide is added after
    the text instead of failing."""
    folder = _project(tmp_path)
    (folder / AGENTS_FILE).write_text(f"{END_MARKER}\nOur rules.\n{START_MARKER}\n")
    write_guide(load_config(folder))
    text = (folder / AGENTS_FILE).read_text()
    assert text.startswith(f"{END_MARKER}\nOur rules.") and text.rstrip().endswith(END_MARKER)
    assert "Working with chronobox" in text
