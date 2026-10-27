# notes: an example project

A small notes web app (`app.py`, `GET /notes`) and its PostgreSQL database, described for
chronobox by [`chronobox.toml`](chronobox.toml): the image, how to create the database, the two
services, and what every hand-off replay adds (users writing notes during the replay, and the
schema and API compared with the agent's machine).

## Try it

With chronobox installed and set up ([Install](../../README.md#install)):

```bash
cd examples/notes
claude mcp add chronobox -- chronobox mcp --project .
claude
```

The first session prepares the machine (about a minute: it creates the database) and saves a
warm start; the next ones start in under a second. Things to ask for:

- **A change on the running server:** "Notes need a title. Add a required title column to the
  notes table on the running server, filled for the existing notes from the first three words of
  their body, and return it in GET /notes. Keep the app working for its users."
- **A script to run elsewhere:** "Write me a migration script that adds a `created_at` column with
  the time each note was saved, for our production server." The agent checks it with
  `verify_script`, which replays it on a fresh copy while [`traffic.py`](traffic.py) keeps writing
  notes, kills it midway and runs it again; the hand-off lands in `.chronobox/exports/handoff/`.
- **Undo:** "Delete every note, then roll the machine back."

## Files

| File | What |
|---|---|
| `chronobox.toml` | How chronobox brings the project up, and the `[handoff]` checks |
| `app.py` | The web app: `GET /notes`, `GET /health` |
| `migrate.py` | Creates the table and two first notes (run by `setup`) |
| `traffic.py` | Users: a note every 0.1 s, and `--report` fails if a saved note is gone |
