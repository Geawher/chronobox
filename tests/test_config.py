"""Reading chronobox.toml (no VM)."""

from pathlib import Path

import pytest

from chronobox.config import DEFAULT_EXCLUDES, ConfigError, HandoffSettings, load_config, starter_config
from chronobox.images import DEFAULT_IMAGE

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "notes"


def write(folder: Path, text: str) -> Path:
    """Write ``text`` as the folder's chronobox.toml and return the folder."""
    (folder / "chronobox.toml").write_text(text)
    return folder


def test_the_example_project_reads_as_written() -> None:
    """The README's example project parses to the services, setup and excludes it declares."""
    config = load_config(EXAMPLE)
    assert config.path == EXAMPLE
    assert (config.image.name, config.target) == ("default", "/src")
    assert [service.name for service in config.services] == ["db", "app"]
    assert config.services[1].ready.startswith("curl")
    assert config.setup == ("python3 migrate.py",)
    assert config.exclude == (*DEFAULT_EXCLUDES, "__pycache__")
    assert config.handoff.load == "python3 traffic.py" and len(config.handoff.compare) == 2


def test_defaults_for_a_minimal_file(tmp_path: Path) -> None:
    """An empty file is a valid config: default image, project here, copied to /src."""
    config = load_config(write(tmp_path, ""))
    assert (config.image.name, config.path, config.target) == ("default", tmp_path, "/src")
    assert (config.services, config.prepare, config.setup) == ((), (), ())


def test_image_from_a_dockerfile_resolves_paths_from_the_file(tmp_path: Path) -> None:
    """A Dockerfile path is relative to the config file; its folder is the default context and gives the image
    its name."""
    (tmp_path / "docker").mkdir()
    config = load_config(write(tmp_path, 'image = { dockerfile = "docker/Dockerfile.dev" }\n'))
    assert config.image.dockerfile == tmp_path / "docker" / "Dockerfile.dev"
    assert config.image.context == tmp_path / "docker"
    assert config.image.name == "docker"


def test_image_from_a_reference_gets_a_file_safe_name(tmp_path: Path) -> None:
    """An image reference gets the same file-safe name ``chronobox build`` gives it."""
    config = load_config(write(tmp_path, 'image = { reference = "postgres:16" }\n'))
    assert (config.image.name, config.image.reference) == ("postgres-16", "postgres:16")


def test_services_keep_their_options(tmp_path: Path) -> None:
    """A service's env, restart, ready timeout and phase are read as written, the workdir defaulting to
    empty and services starting before setup unless they say otherwise."""
    text = """
[[service]]
name = "db"
start = "docker-entrypoint.sh postgres"
ready = "pg_isready"
env = { POSTGRES_HOST_AUTH_METHOD = "trust" }
restart = false
ready_timeout = 30

[[service]]
name = "app"
start = "python manage.py runserver"
after_setup = true
"""
    db, app = load_config(write(tmp_path, text)).services
    assert db.env == {"POSTGRES_HOST_AUTH_METHOD": "trust"}
    assert (db.restart, db.ready_timeout, db.workdir, db.after_setup) == (False, 30.0, "", False)
    assert app.after_setup


def test_handoff_settings_read_as_written_and_default_to_nothing(tmp_path: Path) -> None:
    """``[handoff]`` keeps its commands in order; without it a replay adds nothing."""
    assert load_config(write(tmp_path, "")).handoff == HandoffSettings()
    text = """
[handoff]
prepare = ["bin/more-data"]
load = "bin/traffic"
load_check = "bin/traffic --report"
checks = ["bin/smoke", "bin/api-test"]
compare = ["pg_dump -s"]
"""
    handoff = load_config(write(tmp_path, text)).handoff
    assert handoff.prepare == ("bin/more-data",) and handoff.checks == ("bin/smoke", "bin/api-test")
    assert (handoff.load, handoff.load_check, handoff.compare) == (
        "bin/traffic",
        "bin/traffic --report",
        ("pg_dump -s",),
    )


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("imag = 'x'\n", "unknown keys in the top level: imag"),
        ("[machine]\nmemory = 1\n", "unknown keys in machine: memory"),
        ("image = { reference = 'a', dockerfile = 'b' }\n", "exactly one"),
        ("image = { name = 'a' }\n", "exactly one"),
        ("[[service]]\nname = 'db'\n", "name and a start command"),
        ("[[service]]\nname = 'a'\nstart = 'x'\n[[service]]\nname = 'a'\nstart = 'y'\n", "unique"),
        ("[[service]]\nname = 'a'\nstart = 'x'\nenv = { PORT = 5432 }\n", "strings"),
        ("[project]\ntarget = 'src'\n", "absolute"),
        ("setup = 'make'\n", "setup must be a list of str"),
        ("image = [", "chronobox.toml"),
        ("image = '../x'\n", "bad image name"),
        ("[[service]]\nname = 'web api'\nstart = 'x'\n", "service name"),
        ("[[service]]\nname = 'a;rm'\nstart = 'x'\n", "service name"),
        ("[[service]]\nname = 'a'\nstart = 'x'\nenv = { 'A B' = 'x' }\n", "bad env variable names"),
        ("[handoff]\nchecks = 'make test'\n", "checks must be a list of str"),
        ("[handoff]\nload = ['a']\n", "handoff.load must be a string"),
        ("[handoff]\nload_check = 'x'\n", "load_check needs handoff.load"),
        ("[handoff]\nsmoke = 'x'\n", "unknown keys in handoff: smoke"),
    ],
)
def test_mistakes_are_reported_clearly(tmp_path: Path, text: str, message: str) -> None:
    """Each kind of mistake (unknown keys, wrong types, ambiguous images, unsafe names) fails with a message
    naming it, before anything boots."""
    with pytest.raises(ConfigError, match=message):
        load_config(write(tmp_path, text))


def test_a_missing_file_says_where_it_looked(tmp_path: Path) -> None:
    """A folder without chronobox.toml fails with a message saying so."""
    with pytest.raises(ConfigError, match="no chronobox.toml"):
        load_config(tmp_path)


def test_a_starter_config_is_valid_and_uses_what_the_folder_has(tmp_path: Path) -> None:
    """``chronobox init``'s file loads as it is: the folder's Dockerfile as the image when there is
    one, and only the heavy folders that exist excluded; without a Dockerfile, the default image."""
    (tmp_path / "Dockerfile").write_text("FROM alpine\n")
    (tmp_path / "node_modules").mkdir()
    config = load_config(write(tmp_path, starter_config(tmp_path)))
    assert config.image.dockerfile == tmp_path / "Dockerfile"
    assert config.exclude == (*DEFAULT_EXCLUDES, "node_modules")
    assert (config.services, config.prepare, config.setup) == ((), (), ())

    (tmp_path / "Dockerfile").unlink()
    config = load_config(write(tmp_path, starter_config(tmp_path)))
    assert (config.image.name, config.image.dockerfile) == (DEFAULT_IMAGE, None)
