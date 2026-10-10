from chronobox import commands


def test_read_only_commands_do_not_change_state() -> None:
    for command in (
        "find / -name x 2>/dev/null | head",
        "strings app.pyc | head",
        "cat f 2>&1",
        "sqlite3 db.sqlite 'select count(*) from t'",
        "curl -s localhost:9100",
        "git status",
    ):
        assert not commands.changes_state(command), command


def test_writes_change_state() -> None:
    for command in (
        "echo hi > /etc/x",
        "sed -i s/a/b/ f",
        "curl -s -X POST localhost",
        "ledgerctl migrate",
        "pip install x",
        "python3 fix.py",
    ):
        assert commands.changes_state(command), command


def test_destructive_commands_need_rehearsal_but_plain_writes_do_not() -> None:
    assert commands.needs_rehearsal("rm -rf /srv/data")
    assert commands.needs_rehearsal("systemctl restart nginx")
    assert commands.needs_rehearsal("psql -c 'DELETE FROM orders'")
    assert commands.needs_rehearsal("uv pip install pricing==2.1")
    assert not commands.needs_rehearsal("echo keep > /root/notes")
    assert commands.is_risky("echo keep > /root/notes")


def test_normalize_ignores_spacing() -> None:
    assert commands.normalize("rm   -f  /tmp/x ") == commands.normalize("rm -f /tmp/x")
