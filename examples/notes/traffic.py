"""Users of the notes service, for chronobox's hand-off replays (``[handoff]`` in chronobox.toml).

    python3 traffic.py            # write a note every 0.1 s and read the list, until stopped
    python3 traffic.py --report   # after: fail if a note the users were told was saved is missing

A write counts as saved once its insert committed; failed writes and reads are only counted
(a restart may fail a few), but a saved note that is gone is lost data.
"""

import sys
import time
import urllib.request
from pathlib import Path

import psycopg

DATABASE = "host=127.0.0.1 user=postgres dbname=postgres"
SAVED = Path("/tmp/traffic-saved")
FAILED = Path("/tmp/traffic-failed")
PAUSE_S = 0.1
READ_EVERY = 5


def write(number: int) -> None:
    """Save one note, and record it as saved only once the insert committed."""
    try:
        with psycopg.connect(DATABASE, connect_timeout=2) as connection:
            connection.execute("insert into notes (body) values (%s)", (f"traffic {number}",))
    except psycopg.Error:
        with FAILED.open("a") as failed:
            failed.write(f"write {number}\n")
        return
    with SAVED.open("a") as saved:
        saved.write(f"traffic {number}\n")


def read() -> None:
    """List the notes through the service, as a user's page would."""
    try:
        urllib.request.urlopen("http://127.0.0.1:8000/notes", timeout=5).read()
    except OSError:
        with FAILED.open("a") as failed:
            failed.write("read\n")


def report() -> int:
    """Exit status: 1 if a saved note is missing from the table."""
    saved = set(SAVED.read_text().splitlines()) if SAVED.exists() else set()
    with psycopg.connect(DATABASE) as connection:
        rows = connection.execute("select body from notes where body like 'traffic %'").fetchall()
    missing = saved - {body for (body,) in rows}
    failed = FAILED.read_text().splitlines() if FAILED.exists() else []
    print(f"{len(saved)} notes saved, {len(missing)} of them missing; {len(failed)} failed requests")
    return 1 if missing else 0


if __name__ == "__main__":
    if sys.argv[1:] == ["--report"]:
        sys.exit(report())
    for number in range(1, sys.maxsize):
        write(number)
        if number % READ_EVERY == 0:
            read()
        time.sleep(PAUSE_S)
