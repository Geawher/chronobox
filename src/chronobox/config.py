"""``chronobox.toml``: how to bring a project up in a chronobox machine.

A project commits one file next to its code::

    image = "myapp"                     # a built image (`chronobox images`), or a table:
    # image = { dockerfile = "Dockerfile.dev", context = ".", name = "myapp" }
    # image = { reference = "postgres:16" }
    prepare = ["make build"]            # after the project is copied, before services start
    setup = ["bin/migrate", "bin/seed"] # once the services are ready

    [machine]
    mem_mib = 2048
    disk_mib = 8192

    [project]
    path = "."                          # copied into the machine at `target`
    target = "/src"
    exclude = [".git", "node_modules"]

    [[service]]
    name = "db"
    start = "docker-entrypoint.sh postgres"
    ready = "pg_isready -h 127.0.0.1"
    env = { POSTGRES_HOST_AUTH_METHOD = "trust" }

    [handoff]                           # what `verify_script` adds to every replay
    prepare = ["bin/more-data"]         # make the replay fork look like production first
    load = "bin/traffic"                # runs during the replay, like real users
    load_check = "bin/traffic --report" # after it: fails if users saw errors or lost writes
    checks = ["bin/smoke-test"]         # must pass after the script
    compare = ["pg_dump -s -U postgres"] # same output on the agent's machine and the replay
    sample = ["bin/keep-a-tenth"]       # optional: shrink the data, to check the logic fast

Relative paths are relative to the file's folder. Machines have no network: anything to
install belongs in the image.
"""

from __future__ import annotations

import json
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from chronobox.images import DEFAULT_IMAGE, ImageError, check_image_name, image_name

CONFIG_FILE = "chronobox.toml"
DEFAULT_TARGET = "/src"
DEFAULT_EXCLUDES = (".git", ".chronobox")
DEFAULT_READY_TIMEOUT_S = 120.0
DEFAULT_MEM_MIB = 1024
DEFAULT_DISK_MIB = 2048
DOCKERFILE = "Dockerfile"
# Folders usually too heavy or too machine-specific to copy; `chronobox init` excludes those it finds.
COMMON_EXCLUDES = (
    "node_modules", ".venv", "venv", "__pycache__", "target", "dist",
    ".pytest_cache", ".mypy_cache", ".ruff_cache",
)  # fmt: skip
TOP_LEVEL_KEYS = frozenset({"image", "prepare", "setup", "machine", "project", "service", "handoff"})
MACHINE_KEYS = frozenset({"mem_mib", "disk_mib", "vcpus", "command_timeout"})
PROJECT_KEYS = frozenset({"path", "target", "exclude"})
HANDOFF_KEYS = frozenset({"prepare", "load", "load_check", "checks", "compare", "sample", "review"})
IMAGE_KEYS = frozenset({"name", "reference", "dockerfile", "context"})
SERVICE_KEYS = frozenset(
    {"name", "start", "ready", "env", "workdir", "restart", "ready_timeout", "after_setup"}
)
# Service names become file names in commands, so keep them to what needs no quoting.
SERVICE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")
ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
# What `chronobox init` writes: every option, commented, around the values it could guess.
STARTER_TEMPLATE = """\
# How to bring this project up in a chronobox machine (docs: README, "Your project").
# Machines have no network: everything the project needs must be installed in the image.

image = {image}
# image = "myapp"                          # an image built with `chronobox build`
# image = {{ reference = "postgres:16" }}    # a Docker image, as is
# image = {{ dockerfile = "Dockerfile" }}    # built from a Dockerfile (context: its folder)

# Commands run in the project folder in the machine, when it is prepared.
prepare = []  # after the project is copied in, before the services, e.g. ["make build"]
setup = []    # once every service is ready, e.g. ["bin/migrate", "bin/seed"]

[machine]
mem_mib = {mem_mib}
disk_mib = {disk_mib}

[project]
exclude = {exclude}  # not copied in (.git and .chronobox never are)

# One block per long-running process, started in order; each must be ready before the next.
# [[service]]
# name = "db"
# start = "docker-entrypoint.sh postgres"  # runs in the foreground; restarted if it exits
# ready = "pg_isready -h 127.0.0.1"        # succeeds once the service answers
# env = {{ POSTGRES_HOST_AUTH_METHOD = "trust" }}
#
# [[service]]
# name = "app"
# start = "python manage.py runserver 127.0.0.1:8000"
# ready = "curl -sf 127.0.0.1:8000/"
# after_setup = true                       # start once setup (e.g. migrations) is done
"""


class ConfigError(ValueError):
    """``chronobox.toml`` is missing, unreadable or inconsistent."""


@dataclass(frozen=True)
class ImageSource:
    """Which image the machine runs, and how ``chronobox build --project`` makes it.

    Attributes:
        name: The built image's name.
        reference: A Docker image to use as is, if built from one.
        dockerfile: A Dockerfile to build, if built from one.
        context: The Dockerfile's build context.
    """

    name: str
    reference: str | None = None
    dockerfile: Path | None = None
    context: Path | None = None


@dataclass(frozen=True)
class Service:
    """A long-running process of the project, started in the machine before ``setup`` (or after it).

    Attributes:
        name: Used for its log, ``/var/log/chronobox/<name>.log``.
        start: Shell command that runs the service in the foreground.
        ready: Shell command that succeeds once the service answers; empty: started is ready.
        env: Extra environment variables for ``start``.
        workdir: Where ``start`` runs; defaults to the project's target.
        restart: Start it again whenever it exits (like Docker's ``unless-stopped``).
        ready_timeout: Seconds to wait for ``ready`` before the boot fails.
        after_setup: Start it once ``setup`` is done instead of before, e.g. an app server that
            needs the migrations ``setup`` runs against a database service.
    """

    name: str
    start: str
    ready: str = ""
    env: dict[str, str] = field(default_factory=dict)
    workdir: str = ""
    restart: bool = True
    ready_timeout: float = DEFAULT_READY_TIMEOUT_S
    after_setup: bool = False


@dataclass(frozen=True)
class HandoffSettings:
    """What the project adds to every ``verify_script`` replay (the ``[handoff]`` table), so a
    hand-off is checked against what the project's owner knows about production, not only against
    the agent's own check.

    Attributes:
        prepare: Commands run on the replay fork before anything else, e.g. to load more or odder
            data, so it looks more like production than the agent's copy.
        load: A command started in the background before the script and stopped after the checks:
            traffic like the real users'.
        load_check: Run once ``load`` is stopped; fails if the users had a problem (errors, lost
            writes).
        checks: Commands that must succeed after the script (and the agent's check).
        compare: Commands whose output must be the same on the machine the agent worked on and on
            the replay, e.g. a schema dump: the script must reproduce what the agent tested.
        sample: Commands that shrink the replay's data, on every replay (the agent can also pass
            its own): the logic is checked in minutes, and the report says it ran on a sample.
        review: A command on the host that reviews each passed hand-off, e.g. ``claude -p``: it
            gets the user's goal, the scripts, the check and the report on stdin, and answers ``OK``
            or the gaps it sees in the check, which the agent is shown.
    """

    prepare: tuple[str, ...] = ()
    load: str = ""
    load_check: str = ""
    checks: tuple[str, ...] = ()
    compare: tuple[str, ...] = ()
    sample: tuple[str, ...] = ()
    review: str = ""


@dataclass(frozen=True)
class ProjectConfig:
    """A parsed ``chronobox.toml``.

    Attributes:
        file: The config file itself.
        image: The machine's image.
        path: The project folder copied into the machine.
        target: Where it is copied in the machine.
        exclude: Names or relative paths not copied (``.git`` and ``.chronobox`` always).
        prepare: Commands run after the copy, before the services.
        services: Started in order; each must be ready before the next starts.
        setup: Commands run once every service is ready.
        mem_mib: Guest memory.
        disk_mib: Writable disk.
        vcpus: Virtual CPUs.
        command_timeout: Default timeout of one command, in seconds.
        handoff: What ``verify_script`` adds to every replay.
    """

    file: Path
    image: ImageSource
    path: Path
    target: str = DEFAULT_TARGET
    exclude: tuple[str, ...] = DEFAULT_EXCLUDES
    prepare: tuple[str, ...] = ()
    services: tuple[Service, ...] = ()
    setup: tuple[str, ...] = ()
    mem_mib: int = DEFAULT_MEM_MIB
    disk_mib: int = DEFAULT_DISK_MIB
    vcpus: int = 2
    command_timeout: float = 300.0
    handoff: HandoffSettings = HandoffSettings()


def find_config(project: Path) -> Path:
    """``project`` itself if it is a file, else its ``chronobox.toml``."""
    return project if project.is_file() else project / CONFIG_FILE


def load_config(project: Path) -> ProjectConfig:
    """Read and check a project's ``chronobox.toml``.

    Args:
        project: The project folder, or the config file itself.

    Raises:
        ConfigError: If the file is missing, is not valid TOML, or has unknown keys or wrong types.
    """
    file = find_config(project).resolve()
    try:
        raw = tomllib.loads(file.read_text())
    except FileNotFoundError as error:
        raise ConfigError(f"no {CONFIG_FILE} in {project}") from error
    except tomllib.TOMLDecodeError as error:
        raise ConfigError(f"{file}: {error}") from error
    _check_keys(raw, TOP_LEVEL_KEYS, "the top level")
    base = file.parent
    machine = _table(raw, "machine", MACHINE_KEYS)
    project_table = _table(raw, "project", PROJECT_KEYS)
    services = tuple(_service(entry) for entry in _list(raw, "service", dict))
    names = [service.name for service in services]
    if len(set(names)) != len(names):
        raise ConfigError(f"service names must be unique: {names}")
    return ProjectConfig(
        file=file,
        image=_image(raw.get("image", DEFAULT_IMAGE), base),
        path=(base / project_table.get("path", ".")).resolve(),
        target=_absolute(project_table.get("target", DEFAULT_TARGET), "project.target"),
        exclude=DEFAULT_EXCLUDES + tuple(_list(project_table, "exclude", str)),
        prepare=tuple(_list(raw, "prepare", str)),
        services=services,
        setup=tuple(_list(raw, "setup", str)),
        mem_mib=int(machine.get("mem_mib", DEFAULT_MEM_MIB)),
        disk_mib=int(machine.get("disk_mib", DEFAULT_DISK_MIB)),
        vcpus=int(machine.get("vcpus", 2)),
        command_timeout=float(machine.get("command_timeout", 300.0)),
        handoff=_handoff(_table(raw, "handoff", HANDOFF_KEYS)),
    )


def starter_config(folder: Path) -> str:
    """A commented ``chronobox.toml`` for ``folder``, valid as it is (``chronobox init``).

    The image is the folder's ``Dockerfile`` if it has one, else the default image; the usual heavy
    folders it holds are excluded. Services stay a commented example: only the project knows them.
    """
    has_dockerfile = (folder / DOCKERFILE).is_file()
    image = f'{{ dockerfile = "{DOCKERFILE}" }}' if has_dockerfile else json.dumps(DEFAULT_IMAGE)
    excludes = [name for name in COMMON_EXCLUDES if (folder / name).exists()]
    return STARTER_TEMPLATE.format(
        image=image, exclude=json.dumps(excludes), mem_mib=DEFAULT_MEM_MIB, disk_mib=DEFAULT_DISK_MIB
    )


def _image(value: Any, base: Path) -> ImageSource:
    """The image source, with its name checked as an image name."""
    source = _image_source(value, base)
    try:
        check_image_name(source.name)
    except ImageError as error:
        raise ConfigError(f"image: {error}") from error
    return source


def _image_source(value: Any, base: Path) -> ImageSource:
    """Read ``image``: a built image's name, or a table with a ``reference`` or a ``dockerfile``.

    Args:
        value: The raw ``image`` value.
        base: The config file's folder; ``dockerfile`` and ``context`` are relative to it.

    Raises:
        ConfigError: If it is neither a name nor a table, or the table has not exactly one source.
    """
    if isinstance(value, str):
        return ImageSource(name=value)
    if not isinstance(value, dict):
        raise ConfigError("image must be a name or a table")
    _check_keys(value, IMAGE_KEYS, "image")
    if ("reference" in value) == ("dockerfile" in value):
        raise ConfigError("image needs exactly one of reference or dockerfile")
    if "reference" in value:
        reference = value["reference"]
        return ImageSource(name=value.get("name") or image_name(reference), reference=reference)
    dockerfile = (base / value["dockerfile"]).resolve()
    context = (base / value["context"]).resolve() if "context" in value else dockerfile.parent
    name = value.get("name") or image_name(context.name)
    return ImageSource(name=name, dockerfile=dockerfile, context=context)


def _service(entry: dict[str, Any]) -> Service:
    """Read one ``[[service]]`` table.

    Raises:
        ConfigError: If it has unknown keys, no name or start command, a name that would need
            quoting, or env values or names that are not plain strings and variable names.
    """
    _check_keys(entry, SERVICE_KEYS, "service")
    if not entry.get("name") or not entry.get("start"):
        raise ConfigError(f"every service needs a name and a start command: {entry}")
    if not SERVICE_NAME.fullmatch(str(entry["name"])):
        raise ConfigError(f"service name {entry['name']!r}: use letters, digits, '.', '_' and '-'")
    env = entry.get("env", {})
    if not all(isinstance(value, str) for value in env.values()):
        raise ConfigError(f"service {entry['name']}: env values must be strings")
    bad_names = [name for name in env if not ENV_NAME.fullmatch(name)]
    if bad_names:
        raise ConfigError(f"service {entry['name']}: bad env variable names {bad_names}")
    return Service(
        name=entry["name"],
        start=entry["start"],
        ready=entry.get("ready", ""),
        env=dict(env),
        workdir=entry.get("workdir", ""),
        restart=bool(entry.get("restart", True)),
        ready_timeout=float(entry.get("ready_timeout", DEFAULT_READY_TIMEOUT_S)),
        after_setup=bool(entry.get("after_setup", False)),
    )


def _handoff(table: dict[str, Any]) -> HandoffSettings:
    """Read the ``[handoff]`` table.

    Raises:
        ConfigError: If a command list is not a list of strings, or a command not a string.
    """
    for key in ("load", "load_check", "review"):
        if not isinstance(table.get(key, ""), str):
            raise ConfigError(f"handoff.{key} must be a string")
    return HandoffSettings(
        prepare=tuple(_list(table, "prepare", str)),
        load=table.get("load", ""),
        load_check=table.get("load_check", ""),
        checks=tuple(_list(table, "checks", str)),
        compare=tuple(_list(table, "compare", str)),
        sample=tuple(_list(table, "sample", str)),
        review=table.get("review", ""),
    )


def _table(raw: dict[str, Any], key: str, allowed: frozenset[str]) -> dict[str, Any]:
    """The table under ``key`` (empty if absent), checked for unknown keys."""
    table = raw.get(key, {})
    if not isinstance(table, dict):
        raise ConfigError(f"{key} must be a table")
    _check_keys(table, allowed, key)
    return table


def _list(raw: dict[str, Any], key: str, kind: type) -> list[Any]:
    """The list under ``key`` (empty if absent), checked to hold only ``kind`` items."""
    items = raw.get(key, [])
    if not isinstance(items, list) or not all(isinstance(item, kind) for item in items):
        raise ConfigError(f"{key} must be a list of {kind.__name__}")
    return items


def _check_keys(table: dict[str, Any], allowed: frozenset[str], where: str) -> None:
    """Raise a ``ConfigError`` naming the keys of ``table`` that are not ``allowed``."""
    unknown = sorted(set(table) - allowed)
    if unknown:
        allowed_keys = ", ".join(sorted(allowed))
        raise ConfigError(f"unknown keys in {where}: {', '.join(unknown)} (allowed: {allowed_keys})")


def _absolute(path: str, key: str) -> str:
    """An absolute path in the machine without a trailing slash; ``key`` names it in the error."""
    if not path.startswith("/"):
        raise ConfigError(f"{key} must be an absolute path in the machine: {path!r}")
    return path.rstrip("/") or "/"
