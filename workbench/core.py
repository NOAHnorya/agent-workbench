"""Shared plumbing: workspace paths, config, task cards, event logs, handoff parsing.

Everything lives in plain files so any agent, script or human can read it:

  <root>/workbench.json                      global config: agents, ports, models
  <root>/projects/<p>/project.json            project config: title, repo, color, priority
  <root>/projects/<p>/board/<task>.json       one task card per file
  <root>/projects/<p>/handoffs/<task>.md      latest handoff summary per card (L1)
  <root>/projects/<p>/runs/<task>.<n>.log     raw agent output per attempt (L2)
  <root>/projects/<p>/events.jsonl            append-only event log, one line per event
  <root>/logs/runner.jsonl                    events that belong to no project
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from pathlib import Path

ROOT = Path(os.environ.get("WB_ROOT", Path(__file__).resolve().parent.parent))
_lock = threading.RLock()

STATUSES = (
    "open",            # ready to be dispatched once dependencies are done
    "running",         # an agent is working on it
    "waiting_job",     # agent submitted an async job, runner is polling it
    "checking",        # runner is running the card's check command
    "waiting_human",   # manual card or human review pending
    "done",
    "failed",          # out of attempts, needs a human
)


def set_root(path: str | Path) -> None:
    global ROOT
    ROOT = Path(path).expanduser().resolve()


def now() -> float:
    return time.time()


def read_json(path: Path, default=None):
    try:
        return json.loads(path.read_text("utf-8"))
    except FileNotFoundError:
        return default


def write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", "utf-8")
    os.replace(tmp, path)


# ---------------------------------------------------------------- config

def config() -> dict:
    cfg = read_json(ROOT / "workbench.json")
    if cfg is None:
        raise SystemExit(f"找不到 {ROOT / 'workbench.json'}，先运行 ./wb init")
    return cfg


def projects_dir() -> Path:
    return ROOT / "projects"


def project_ids() -> list[str]:
    d = projects_dir()
    if not d.exists():
        return []
    return sorted(p.name for p in d.iterdir() if (p / "project.json").exists())


def project(pid: str) -> dict:
    p = read_json(projects_dir() / pid / "project.json")
    if p is None:
        raise SystemExit(f"项目 {pid} 不存在")
    p.setdefault("id", pid)
    p.setdefault("title", pid)
    p.setdefault("priority", 0)
    repo = p.get("repo") or str(projects_dir() / pid / "workspace")
    p["repo"] = str(Path(repo).expanduser())
    return p


def pdir(pid: str) -> Path:
    return projects_dir() / pid


# ---------------------------------------------------------------- cards

def card_path(pid: str, tid: str) -> Path:
    return pdir(pid) / "board" / f"{tid}.json"


def load_card(pid: str, tid: str) -> dict | None:
    return read_json(card_path(pid, tid))


def cards(pid: str) -> list[dict]:
    d = pdir(pid) / "board"
    if not d.exists():
        return []
    out = []
    for f in sorted(d.glob("*.json")):
        c = read_json(f)
        if c:
            c.setdefault("id", f.stem)
            out.append(c)
    return out


def normalize_card(c: dict) -> dict:
    c.setdefault("title", c.get("id"))
    c.setdefault("mode", "auto")          # auto | manual
    c.setdefault("status", "open")
    c.setdefault("depends_on", [])
    c.setdefault("fallback", [])
    c.setdefault("refs", [])
    c.setdefault("attempts", 0)
    c.setdefault("max_attempts", 3)
    c.setdefault("review", False)         # true: stop at waiting_human after passing checks
    c.setdefault("created", now())
    return c


def save_card(pid: str, c: dict) -> None:
    with _lock:
        c["updated"] = now()
        write_json(card_path(pid, c["id"]), c)


def update_card(pid: str, tid: str, **fields) -> dict:
    with _lock:
        c = load_card(pid, tid)
        if c is None:
            raise KeyError(f"{pid}/{tid}")
        c.update(fields)
        save_card(pid, c)
        return c


# ---------------------------------------------------------------- events

_seq: dict[str, int] = {}


def _log_file(pid: str | None) -> Path:
    if pid:
        return pdir(pid) / "events.jsonl"
    return ROOT / "logs" / "runner.jsonl"


def _next_seq(pid: str | None) -> int:
    key = pid or "-"
    if key not in _seq:
        f = _log_file(pid)
        n = 0
        if f.exists():
            with f.open("rb") as fh:
                n = sum(1 for _ in fh)
        _seq[key] = n
    _seq[key] += 1
    return _seq[key]


def emit(type_: str, *, project: str | None = None, agent: str = "runner", task: str = "-",
         detail: str = "", **extra) -> dict:
    """Append one event. The log line number doubles as its reference: log#<n>."""
    with _lock:
        ev = {"ts": round(now(), 3), "n": _next_seq(project), "project": project or "-",
              "agent": agent, "type": type_, "task": task, "detail": detail}
        ev.update({k: v for k, v in extra.items() if v is not None})
        f = _log_file(project)
        f.parent.mkdir(parents=True, exist_ok=True)
        with f.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(ev, ensure_ascii=False) + "\n")
        return ev


def read_events(pid: str | None, task: str | None = None) -> list[dict]:
    f = _log_file(pid)
    if not f.exists():
        return []
    out = []
    for line in f.read_text("utf-8").splitlines():
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if task is None or ev.get("task") == task:
            out.append(ev)
    return out


def all_log_files() -> list[Path]:
    files = [ROOT / "logs" / "runner.jsonl"]
    files += [pdir(p) / "events.jsonl" for p in project_ids()]
    return files


# ---------------------------------------------------------------- handoffs

HANDOFF_RE = re.compile(r"<handoff>(.*?)</handoff>", re.S)


def extract_handoff(output: str) -> str | None:
    """Find the last <handoff> block in raw agent output.

    Agents print plain text or JSON lines depending on the CLI, so the block may be
    JSON-escaped inside a string. We search the raw text first, then every string
    value of every JSON line.
    """
    strings: list[str] = []

    def walk(v):
        if isinstance(v, str):
            strings.append(v)
        elif isinstance(v, dict):
            for x in v.values():
                walk(x)
        elif isinstance(v, list):
            for x in v:
                walk(x)

    for line in output.splitlines():
        line = line.strip()
        if line.startswith("{"):
            try:
                walk(json.loads(line))
            except json.JSONDecodeError:
                pass
    for s in reversed(strings):
        m = HANDOFF_RE.findall(s)
        if m:
            return m[-1].strip()
    found = HANDOFF_RE.findall(output)          # plain text output
    return found[-1].strip() if found else None


def parse_frontmatter(md: str) -> tuple[dict, str]:
    """Tiny frontmatter reader: `key: value` lines; values are JSON when they parse."""
    meta: dict = {}
    body = md
    m = re.match(r"\s*---\s*\n(.*?)\n---\s*\n?(.*)", md, re.S)
    if m:
        body = m.group(2)
        for line in m.group(1).splitlines():
            if ":" not in line or line.lstrip().startswith("#"):
                continue
            k, v = line.split(":", 1)
            v = v.split(" #")[0].strip()
            try:
                meta[k.strip()] = json.loads(v)
            except json.JSONDecodeError:
                if v.startswith("[") and v.endswith("]"):
                    meta[k.strip()] = [x.strip().strip("'\"") for x in v[1:-1].split(",") if x.strip()]
                else:
                    meta[k.strip()] = v.strip("'\"")
    return meta, body.strip()


def handoff_path(pid: str, tid: str) -> Path:
    return pdir(pid) / "handoffs" / f"{tid}.md"


def write_handoff(pid: str, tid: str, agent: str, meta: dict, body: str, log_refs: list[str]) -> Path:
    head = {"task": tid, "by": agent, **meta, "log_refs": log_refs}
    lines = ["---"] + [f"{k}: {json.dumps(v, ensure_ascii=False)}" for k, v in head.items()] + ["---", "", body, ""]
    p = handoff_path(pid, tid)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("\n".join(lines), "utf-8")
    return p


def read_handoff(pid: str, tid: str) -> str | None:
    p = handoff_path(pid, tid)
    return p.read_text("utf-8") if p.exists() else None


def handoff_meta(pid: str, tid: str) -> dict:
    text = read_handoff(pid, tid)
    return parse_frontmatter(text)[0] if text else {}


def is_blocked(pid: str, card: dict) -> bool:
    """Waiting because the agent asked the owner a question (not because it wants a review).

    The handoff's own status is the source of truth, so a card with `review: true` whose agent
    came back `blocked` is still a question: approving it would mark undone work as done.
    """
    return card.get("status") == "waiting_human" and card.get("mode") != "manual" \
        and handoff_meta(pid, card["id"]).get("status") == "blocked"


IMAGE_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp",
               ".gif": "image/gif"}


def repo_file(pid: str, rel: str) -> Path | None:
    """A file inside the project's repo, or None when the path escapes it or does not exist."""
    repo = Path(project(pid)["repo"]).resolve()
    p = (repo / rel).resolve()
    return p if p.is_file() and (p == repo or repo in p.parents) else None


def card_images(pid: str, card: dict, limit: int = 12) -> list[str]:
    """Pictures among the card's artifacts (files, or pictures directly inside listed folders)."""
    arts = handoff_meta(pid, card["id"]).get("artifacts") or (card.get("result") or {}).get("artifacts") or []
    repo = Path(project(pid)["repo"]).resolve()
    out: list[str] = []
    for rel in arts if isinstance(arts, list) else []:
        p = (repo / str(rel)).resolve()
        if not (p == repo or repo in p.parents):
            continue
        found = [p] if p.is_file() else sorted(p.iterdir()) if p.is_dir() else []
        for f in found:
            r = str(f.relative_to(repo))
            if f.is_file() and f.suffix.lower() in IMAGE_TYPES and r not in out:
                out.append(r)
    return out[:limit]
