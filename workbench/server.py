"""Local dashboard server (127.0.0.1 only). Streams every project's event log to the browser.

  GET /                     the office dashboard
  GET /api/state            agents, projects and their cards
  GET /api/events           Server-Sent Events: recent backlog, then live events
  GET /api/handoff?project=&task=   a handoff summary (markdown)
  GET /api/file?project=&path=      a picture from the project's repo (review thumbnails)
"""
from __future__ import annotations

import json
import queue
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import core

clients: list[queue.Queue] = []
clients_lock = threading.Lock()
backlog: list[dict] = []
BACKLOG = 400


def tail_logs(stop: threading.Event):
    offsets: dict[Path, int] = {}
    first = True
    while not stop.is_set():
        new = []
        for f in core.all_log_files():
            if not f.exists():
                continue
            size = f.stat().st_size
            off = offsets.get(f, 0)
            if size < off:
                off = 0
            if size > off:
                with f.open("rb") as fh:
                    fh.seek(off)
                    chunk = fh.read()
                cut = chunk.rfind(b"\n") + 1
                offsets[f] = off + cut
                for line in chunk[:cut].decode("utf-8", "ignore").splitlines():
                    try:
                        new.append(json.loads(line))
                    except json.JSONDecodeError:
                        pass
        if new:
            new.sort(key=lambda e: e.get("ts", 0))
            backlog.extend(new)
            del backlog[:-BACKLOG]
            if not first:
                with clients_lock:
                    for q in clients:
                        for ev in new:
                            q.put(ev)
        first = False
        stop.wait(0.4)


def typical_seconds() -> dict:
    """Median time from claim to submit per agent, so the dashboard can say "usually about N minutes"."""
    durs: dict[str, list] = {}
    for pid in core.project_ids():
        start = {}
        for e in core.read_events(pid):
            k = (e.get("agent"), e.get("task"))
            if e.get("type") == "claim":
                start[k] = e["ts"]
            elif e.get("type") == "submit" and k in start:
                durs.setdefault(e["agent"], []).append(e["ts"] - start.pop(k))
    return {a: sorted(v)[len(v) // 2] for a, v in durs.items()}


def card_view(pid: str, c: dict) -> dict:
    v = {k: c.get(k) for k in ("id", "title", "status", "assignee", "agent", "attempts", "max_attempts", "depends_on",
                               "mode", "review", "goal", "feedback", "updated", "started")}
    v["blocked"] = core.is_blocked(pid, c)       # a question for the owner, even on a review card
    v["images"] = core.card_images(pid, c) if c.get("status") in ("waiting_human", "done", "failed") else []
    return v


def state() -> dict:
    cfg = core.config()
    live = core.read_json(core.ROOT / "logs" / "agents.json", {}) or {}
    typical = typical_seconds()
    ags = {}
    for aid, a in cfg["agents"].items():
        if a.get("disabled"):
            continue
        ags[aid] = {"name": a.get("name", aid), "role": a.get("role", ""), "color": a.get("color"),
                    "kind": a.get("kind", "cli"), "typical": typical.get(aid), **live.get(aid, {})}
    projects = []
    for pid in core.project_ids():
        p = core.project(pid)
        projects.append({"id": pid, "title": p["title"], "color": p.get("color"), "priority": p["priority"],
                         "cards": [card_view(pid, c) for c in core.cards(pid)]})
    return {"agents": ags, "projects": projects, "log_reader": cfg.get("log_reader", {}).get("agent_id", "qwen"),
            "now": time.time()}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send(self, code, body: bytes, ctype: str):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urlparse(self.path)
        qs = {k: v[0] for k, v in parse_qs(u.query).items()}
        if u.path in ("/", "/index.html"):
            self.send(200, (core.ROOT / "dashboard" / "index.html").read_bytes(), "text/html; charset=utf-8")
        elif u.path == "/api/state":
            self.send(200, json.dumps(state(), ensure_ascii=False).encode(), "application/json; charset=utf-8")
        elif u.path == "/api/handoff":
            pid, tid = qs.get("project", ""), qs.get("task", "")
            if pid not in core.project_ids() or "/" in tid or ".." in tid:
                return self.send(404, b"not found", "text/plain")
            text = core.read_handoff(pid, tid)
            if not text and qs.get("optional") == "1":
                return self.send(200, b"", "text/markdown; charset=utf-8")
            self.send(200 if text else 404, (text or "还没有交接总结").encode(), "text/markdown; charset=utf-8")
        elif u.path == "/api/file":
            pid, rel = qs.get("project", ""), qs.get("path", "")
            f = core.repo_file(pid, rel) if pid in core.project_ids() and rel else None
            kind = core.IMAGE_TYPES.get(f.suffix.lower()) if f else None
            if not kind or f.stat().st_size > 40 * 1024 * 1024:
                return self.send(404, b"not found", "text/plain")
            self.send(200, f.read_bytes(), kind)
        elif u.path == "/api/events":
            self.stream()
        else:
            self.send(404, b"not found", "text/plain")

    def do_POST(self):
        """Dashboard buttons. Only same-page requests: the custom header blocks other websites."""
        from . import actions
        u = urlparse(self.path)
        origin = self.headers.get("Origin", "")
        host_ok = origin == "" or urlparse(origin).hostname in ("127.0.0.1", "localhost")
        if self.headers.get("X-Workbench") != "1" or not host_ok:
            return self.send(403, b'{"error":"forbidden"}', "application/json")
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0) or 0)) or b"{}")
        except json.JSONDecodeError:
            return self.send(400, b'{"error":"bad json"}', "application/json")
        pid, tid = str(body.get("project", "")), str(body.get("task", ""))
        text = str(body.get("text", ""))[:8000]
        routes = {
            "/api/approve": lambda: actions.approve(pid, tid, text),
            "/api/reject": lambda: actions.reject(pid, tid, text),
            "/api/done": lambda: actions.done(pid, tid, text, str(body.get("next", ""))[:4000],
                                              [str(x) for x in body.get("artifacts", [])][:50]),
            "/api/retry": lambda: actions.retry(pid, tid, text),
            "/api/answer": lambda: actions.answer(pid, tid, text),
            "/api/add": lambda: actions.add(pid, text, str(body.get("agent", ""))[:80]),
        }
        if u.path not in routes:
            return self.send(404, b'{"error":"not found"}', "application/json")
        try:
            routes[u.path]()
        except actions.ActionError as e:
            return self.send(409, json.dumps({"error": str(e)}, ensure_ascii=False).encode(), "application/json; charset=utf-8")
        self.send(200, b'{"ok":true}', "application/json")

    def stream(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        q: queue.Queue = queue.Queue()
        with clients_lock:
            snapshot = list(backlog)
            clients.append(q)
        try:
            self.wfile.write(f"event: backlog\ndata: {json.dumps(snapshot, ensure_ascii=False)}\n\n".encode())
            self.wfile.flush()
            while True:
                try:
                    ev = q.get(timeout=15)
                    self.wfile.write(f"data: {json.dumps(ev, ensure_ascii=False)}\n\n".encode())
                except queue.Empty:
                    self.wfile.write(b": keep-alive\n\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            with clients_lock:
                clients.remove(q)


def serve(port: int, stop: threading.Event) -> ThreadingHTTPServer:
    threading.Thread(target=tail_logs, args=(stop,), daemon=True).start()
    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd
