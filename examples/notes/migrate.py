"""Create the notes table and its first notes (safe to run again)."""

import psycopg

with psycopg.connect("host=127.0.0.1 user=postgres dbname=postgres") as connection:
    connection.execute("create table if not exists notes (id serial primary key, body text not null)")
    if connection.execute("select count(*) from notes").fetchone()[0] == 0:
        connection.execute("insert into notes (body) values ('buy milk'), ('ship chronobox')")
