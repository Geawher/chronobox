"""A tiny notes service: GET /notes lists the notes, GET /health answers once the database does."""

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import psycopg

DATABASE = "host=127.0.0.1 user=postgres dbname=postgres"


class Notes(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        with psycopg.connect(DATABASE) as connection:
            if self.path == "/health":
                connection.execute("select 1")
                self.reply({"ok": True})
            elif self.path == "/notes":
                rows = connection.execute("select id, body from notes order by id").fetchall()
                self.reply([{"id": note_id, "body": body} for note_id, body in rows])
            else:
                self.send_error(404)

    def reply(self, payload: object) -> None:
        body = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


if __name__ == "__main__":
    ThreadingHTTPServer(("127.0.0.1", 8000), Notes).serve_forever()
