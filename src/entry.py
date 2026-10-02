import json
import re
from urllib.parse import urlparse

from workers import DurableObject, Response, WorkerEntrypoint


MARKER = "SERVERLESS_BUILD_DURABLE_OBJECTS_PYTHON_V1"
MIN = -1_000_000
MAX = 1_000_000
MAX_BODY_BYTES = 1024
NAME = re.compile(r"[A-Za-z0-9_-]{1,40}\Z")
COUNTER_PATH = re.compile(r"/counter/([^/]+)(?:/(increment|decrement|reset|set))?\Z")


def reply(data, status=200):
    return Response.from_json(data, status=status, headers={"cache-control": "no-store"})


class Counter(DurableObject):
    """One instance (and private SQLite database) per counter name."""

    def __init__(self, ctx, env):
        super().__init__(ctx, env)
        ctx.storage.sql.exec(
            "CREATE TABLE IF NOT EXISTS counter (id INTEGER PRIMARY KEY, value INTEGER NOT NULL)"
        )
        ctx.storage.sql.exec("INSERT OR IGNORE INTO counter (id, value) VALUES (1, 0)")

    def read(self):
        return self.ctx.storage.sql.exec("SELECT value FROM counter WHERE id = 1").one().value

    def change(self, delta):
        # No await between operations: one SQL UPDATE is atomic for concurrent RPC calls.
        rows = self.ctx.storage.sql.exec(
            "UPDATE counter SET value = value + ? WHERE id = 1 "
            "AND value + ? BETWEEN ? AND ? RETURNING value",
            delta,
            delta,
            MIN,
            MAX,
        ).toArray()
        return None if len(rows) == 0 else rows[0].value

    def set_count(self, value):
        return self.ctx.storage.sql.exec(
            "UPDATE counter SET value = ? WHERE id = 1 RETURNING value", value
        ).one().value


async def parse_set_value(request):
    content_type = request.headers.get("content-type", "").split(";")[0].strip().lower()
    if content_type != "application/json":
        return None, 400, "Send a JSON object with one integer value."
    try:
        if int(request.headers.get("content-length", "0")) > MAX_BODY_BYTES:
            return None, 413, "JSON body exceeds 1024 bytes."
    except ValueError:
        return None, 400, "Invalid content length."

    body = request.body
    if body is None:
        return None, 400, "Send a JSON object with one integer value."
    reader = body.getReader()
    chunks = []
    size = 0
    try:
        while True:
            item = await reader.read()
            if item.done:
                break
            chunk = item.value.to_bytes()
            size += len(chunk)
            if size > MAX_BODY_BYTES:
                await reader.cancel()
                return None, 413, "JSON body exceeds 1024 bytes."
            chunks.append(chunk)
    finally:
        reader.releaseLock()

    try:
        data = json.loads(b"".join(chunks).decode("utf-8"))
    except (ValueError, UnicodeError):
        data = None
    if (
        not isinstance(data, dict)
        or set(data) != {"value"}
        or type(data["value"]) is not int
        or not MIN <= data["value"] <= MAX
    ):
        return None, 400, f"Value must be an integer between {MIN} and {MAX}."
    return data["value"], None, None


class Default(WorkerEntrypoint):
    async def fetch(self, request):
        path = urlparse(request.url).path
        if path in ("/", "/health"):
            if request.method.value != "GET":
                return reply({"error": "Method not allowed"}, 405)
            if path == "/health":
                return reply({"ok": True, "marker": MARKER})
            return reply(
                {
                    "pattern": "SQLite-backed named counters",
                    "runtime": "Python",
                    "marker": MARKER,
                    "endpoints": [
                        "GET /counter/{name}",
                        "POST /counter/{name}/increment",
                        "POST /counter/{name}/decrement",
                        "POST /counter/{name}/reset",
                        "POST /counter/{name}/set",
                    ],
                    "limits": {
                        "min": MIN,
                        "max": MAX,
                        "name": "1–40 ASCII letters, numbers, hyphens, or underscores",
                    },
                    "note": "Each name has independent, persistent storage. Demo names are public; use a unique name.",
                }
            )

        match = COUNTER_PATH.fullmatch(path)
        if match is None:
            return reply({"error": "Not found"}, 404)
        name, operation = match.groups()
        if NAME.fullmatch(name) is None:
            return reply({"error": "Name must be 1–40 ASCII letters, numbers, hyphens, or underscores."}, 400)
        if request.method.value != ("POST" if operation else "GET"):
            return reply({"error": "Method not allowed"}, 405)

        value = None
        if operation == "set":
            value, status, error = await parse_set_value(request)
            if error:
                return reply({"error": error}, status)

        stub = self.env.COUNTER.getByName(name)
        if operation is None:
            count = await stub.read()
        elif operation == "increment":
            count = await stub.change(1)
        elif operation == "decrement":
            count = await stub.change(-1)
        else:
            count = await stub.set_count(0 if operation == "reset" else value)
        if count is None:
            return reply({"error": f"Counter must stay between {MIN} and {MAX}."}, 409)
        result = {"name": name, "count": count, "marker": MARKER}
        if operation:
            result["operation"] = operation
        return reply(result)
