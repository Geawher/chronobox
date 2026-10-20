"""``chronobox cp`` sides and archives, and the CLI's argument handling (no VM)."""

import io
import tarfile
from pathlib import Path

import pytest

from chronobox.cli import main, parse_args, serve_mcp
from chronobox.copying import CopyError, Location, check_direction, pack_local, parse_location, unpack_local


@pytest.mark.parametrize(
    ("text", "location"),
    [
        ("main:/src/app.py", Location("/src/app.py", "main")),
        ("try-1:/src", Location("/src", "try-1")),
        ("./out", Location("./out")),
        ("/tmp/out", Location("/tmp/out")),
        ("notes:relative", Location("notes:relative")),
        ("./a:/b", Location("./a:/b")),
    ],
)
def test_a_side_is_on_a_machine_only_as_where_colon_absolute_path(text: str, location: Location) -> None:
    """Only ``WHERE:/absolute`` names a machine; relative or ``./``-prefixed paths stay local, even with a
    colon."""
    assert parse_location(text) == location


def test_exactly_one_side_is_on_the_machine() -> None:
    """A copy goes between here and a machine: two local or two remote sides are refused."""
    check_direction(Location("./x"), Location("/src", "main"))
    with pytest.raises(CopyError):
        check_direction(Location("./x"), Location("./y"))
    with pytest.raises(CopyError):
        check_direction(Location("/a", "main"), Location("/b", "fork"))


def test_a_folder_travels_as_its_content_and_a_file_under_its_name(tmp_path: Path) -> None:
    """A folder's content goes into the destination, a file goes in under its own name, and a missing source
    fails clearly."""
    (tmp_path / "fixtures" / "sub").mkdir(parents=True)
    (tmp_path / "fixtures" / "sub" / "a.txt").write_text("a")
    assert unpack_local(pack_local(tmp_path / "fixtures"), tmp_path / "copy") == ["sub"]
    assert (tmp_path / "copy" / "sub" / "a.txt").read_text() == "a"
    assert unpack_local(pack_local(tmp_path / "fixtures" / "sub" / "a.txt"), tmp_path / "one") == ["a.txt"]
    with pytest.raises(CopyError, match="does not exist"):
        pack_local(tmp_path / "missing")


def test_an_archive_from_the_machine_cannot_write_outside_the_destination(tmp_path: Path) -> None:
    """An archive from the machine with a link pointing out is refused, and a file as destination is refused
    too: the machine cannot write elsewhere on the host."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as tar:
        link = tarfile.TarInfo("escape")
        link.type, link.linkname = tarfile.SYMTYPE, "/etc/passwd"
        tar.addfile(link)
    with pytest.raises(CopyError, match="refused"):
        unpack_local(buffer.getvalue(), tmp_path / "out")
    (tmp_path / "file").write_text("x")
    with pytest.raises(CopyError, match="must be a folder"):
        unpack_local(buffer.getvalue(), tmp_path / "file")


def test_options_after_mcp_go_to_the_server() -> None:
    """Every option after ``mcp``, ``--help`` included, goes to the MCP server; other commands still reject
    unknown options."""
    args = parse_args(["mcp", "--help", "--project", "."])
    assert args.handler is serve_mcp and args.server_args == ["--help", "--project", "."]
    with pytest.raises(SystemExit):
        parse_args(["images", "--bogus"])


def test_init_writes_a_starter_config_once(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """``chronobox init`` writes a ``chronobox.toml`` and says what to do next, but never overwrites
    one: a project's own config is not replaced by a template."""
    main(["init", "--project", str(tmp_path)])
    assert (tmp_path / "chronobox.toml").read_text().startswith("# How to bring this project up")
    printed = capsys.readouterr().out
    assert "chronobox mcp --project ." in printed and "AGENTS.md" in printed
    assert "## Working with chronobox" in (tmp_path / "AGENTS.md").read_text()
    with pytest.raises(SystemExit, match="already exists"):
        main(["init", "--project", str(tmp_path)])
