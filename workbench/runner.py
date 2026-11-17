"""The runner: plain code that moves cards across the board. It never calls a model to decide.

Loop (every poll_seconds):
  1. collect open cards whose dependencies are done, across every project
  2. order them fairly: project priority first, then the project served least recently
  3. give each to its assignee, or the first fallback with a free slot and quota
  4. poll async jobs that agents left behind (e.g. a Hi3D generation)

One attempt:
  claim -> agent works (tool calls become events) -> agent prints a <handoff> block and exits
  -> handoff saved -> async job? wait for it -> check command -> done / back to the agent with feedback
"""
from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import threading
import time
from pathlib import Path

from . import agents, core


class Runner:
    def __init__(self):
        self.cfg = core.config()
        self.agents = self.cfg["agents"]
        self.state = {a: {"busy": 0, "tasks": [], "cooldown_until": 0, "runs": 0} for a in self.agents}
        self.last_served: dict[str, float] = {}
        self.polling: set[str] = set()
        self.lock = threading.RLock()
        self.stop = threading.Event()

    # ------------------------------------------------------------ agent bookkeeping
    def save_state(self):
        core.write_json(core.ROOT / "logs" / "agents.json", self.state)

    def available(self, aid: str) -> bool:
        a, s = self.agents.get(aid), self.state.get(aid)
        if not a or a.get("disabled"):
            return False
        return s["busy"] < a.get("max_parallel", 1) and s["cooldown_until"] <= time.time()

    def take(self, aid, pid, tid):
        with self.lock:
            s = self.state[aid]
            s["busy"] += 1
            s["runs"] += 1
            s["tasks"].append(f"{pid}/{tid}")
            self.save_state()

    def release(self, aid, pid, tid):
        with self.lock:
            s = self.state[aid]
            s["busy"] = max(0, s["busy"] - 1)
            if f"{pid}/{tid}" in s["tasks"]:
                s["tasks"].remove(f"{pid}/{tid}")
            self.save_state()
        core.emit("idle", project=pid, agent=aid, task=tid)

    # ------------------------------------------------------------ main loop
    def recover(self):
        for pid in core.project_ids():
            for c in core.cards(pid):
                if c.get("status") in ("running", "checking"):
                    core.update_card(pid, c["id"], status="open", attempts=max(0, c.get("attempts", 1) - 1))
                    core.emit("recovered", project=pid, task=c["id"], detail="上次运行中断，重新排队")

    def tick(self):
        candidates = []
        for pid in core.project_ids():
            proj = core.project(pid)
            board = {c["id"]: core.normalize_card(c) for c in core.cards(pid)}
            for c in board.values():
                st = c["status"]
                if st == "open":
                    if not all(board.get(d, {}).get("status") == "done" for d in c["depends_on"]):
                        continue
                    if c.get("not_before", 0) > time.time():
                        continue
                    if c["mode"] == "manual":
                        core.update_card(pid, c["id"], status="waiting_human")
                        core.emit("waiting_human", project=pid, agent="human", task=c["id"],
                                  detail=f"手动任务：{c['title']}")
                        continue
                    candidates.append((proj, c))
                elif st == "waiting_job":
                    self.maybe_poll(proj, c)

        candidates.sort(key=lambda pc: (-pc[0]["priority"], self.last_served.get(pc[0]["id"], 0),
                                        pc[1].get("created", 0)))
        for proj, c in candidates:
            order = [c.get("assignee")] + list(c.get("fallback", []))
            aid = next((a for a in order if a and self.available(a)), None)
            if aid is None:
                continue
            pid = proj["id"]
            if aid != c.get("assignee"):
                core.emit("reassign", project=pid, task=c["id"], agent="runner",
                          detail=f"{c.get('assignee')} 不可用，改派 {aid}", to=aid)
            core.emit("dispatch", project=pid, task=c["id"], detail=f"派给 {aid}", to=aid)
            c = core.update_card(pid, c["id"], status="running", attempts=c["attempts"] + 1, agent=aid,
                                 started=time.time())
            self.take(aid, pid, c["id"])
            self.last_served[pid] = time.time()
            threading.Thread(target=self.attempt, args=(proj, c, aid), daemon=True).start()

    def run_forever(self):
        core.emit("runner_start", detail=f"{len(core.project_ids())} 个项目，{len(self.agents)} 个 agent")
        self.recover()
        self.save_state()
        every = self.cfg.get("poll_seconds", 2)
        while not self.stop.is_set():
            try:
                self.tick()
            except Exception as e:  # keep the loop alive; the error is in the log
                core.emit("runner_error", detail=repr(e)[:300])
            self.stop.wait(every)

    # ------------------------------------------------------------ one attempt
    def attempt(self, proj, card, aid):
        pid, tid = proj["id"], card["id"]
        acfg = self.agents[aid]
        claim = core.emit("claim", project=pid, agent=aid, task=tid, detail=card["title"],
                          attempt=card["attempts"])
        refs = [claim["n"]]

        def tool(detail):
            refs.append(core.emit("tool_call", project=pid, agent=aid, task=tid, detail=detail)["n"])

        throttle = agents.Throttle(tool, every=acfg.get("event_every", 2.5))
        log_path = core.pdir(pid) / "runs" / f"{tid}.{card['attempts']}.log"
        try:
            res = agents.run(aid, acfg, card, proj, build_prompt(proj, card, aid, acfg), throttle, log_path)
        except Exception as e:
            res = agents.Result(1, f"runner 调用 agent 出错：{e!r}")
        throttle.flush()

        try:
            if res.auth_failed:
                login = acfg.get("login_hint", f"重新登录 {aid}")
                self.state[aid]["cooldown_until"] = time.time() + 10 * 60
                core.emit("error", project=pid, agent=aid, task=tid, detail=f"{aid} 没登录或登录过期，先 {login}", kind="auth")
                core.update_card(pid, tid, status="failed", feedback=problem(
                    f"{aid} 的登录状态", "命令行返回认证失败（401 或 token 过期）",
                    f"在终端运行 {login}，然后 ./wb retry {pid} {tid}"))
                core.emit("failed", project=pid, agent=aid, task=tid, detail="等你登录后重新排队")
                return
            if res.quota_out:
                until = time.time() + acfg.get("cooldown_minutes", 60) * 60
                self.state[aid]["cooldown_until"] = until
                core.emit("quota_out", project=pid, agent=aid, task=tid,
                          detail=f"额度用完，{acfg.get('cooldown_minutes', 60)} 分钟后再派", until=until)
                core.update_card(pid, tid, status="open", attempts=card["attempts"] - 1)
                return
            if res.transient:
                core.emit("error", project=pid, agent=aid, task=tid, detail="临时错误，60 秒后重试", kind="transient")
                core.update_card(pid, tid, status="open", attempts=card["attempts"] - 1, not_before=time.time() + 60)
                return
            block = core.extract_handoff(res.output)
            if block is None:
                tail = res.output.strip()[-600:] or f"退出码 {res.exit_code}，没有输出"
                core.emit("error", project=pid, agent=aid, task=tid, detail=f"没有交接总结（退出码 {res.exit_code}）")
                self.retry_or_fail(proj, tid, problem(
                    f"{aid} 的输出（runs/{tid}.{card['attempts']}.log）",
                    f"没有输出 <handoff> 交接总结，退出码 {res.exit_code}。输出结尾：{tail}",
                    "完成任务后，最后一条回复必须以 <handoff> 块结尾。"))
                return
            meta, body = core.parse_frontmatter(block)
            ev = core.emit("submit", project=pid, agent=aid, task=tid, detail="交出交接总结",
                           artifacts=meta.get("artifacts"))
            refs.append(ev["n"])
            core.write_handoff(pid, tid, aid, meta, body, [f"log#{n}" for n in refs])
            fields = {"result": {"artifacts": meta.get("artifacts", []), "handoff": f"handoffs/{tid}.md"},
                      "feedback": None}
            if meta.get("status") == "blocked":
                core.update_card(pid, tid, status="waiting_human", **fields)
                core.emit("waiting_human", project=pid, agent="human", task=tid, detail="需要你决定")
                return
            job = meta.get("job")
            if isinstance(job, dict) and job.get("poll"):
                job = {"id": job.get("id", f"{tid}-job"), "poll": job["poll"], "every": job.get("every", 30),
                       "next_poll": 0, "started": time.time(), "timeout": job.get("timeout", 7200)}
                core.update_card(pid, tid, status="waiting_job", job=job, **fields)
                core.emit("async_wait", project=pid, task=tid, detail=f"轮询 {job['id']}", job=job["id"])
                return
            core.update_card(pid, tid, **fields)
        finally:
            self.release(aid, pid, tid)
        self.finish(proj, tid)

    # ------------------------------------------------------------ async jobs
    def maybe_poll(self, proj, card):
        key = f"{proj['id']}/{card['id']}"
        job = card.get("job") or {}
        if key in self.polling or job.get("next_poll", 0) > time.time():
            return
        self.polling.add(key)
        threading.Thread(target=self.poll, args=(proj, card), daemon=True).start()

    def poll(self, proj, card):
        pid, tid, job = proj["id"], card["id"], card["job"]
        try:
            code, out = shell(job["poll"], proj["repo"], 120)
            if code == 0:
                core.emit("async_done", project=pid, task=tid, detail=f"{job['id']} 完成", job=job["id"])
                core.update_card(pid, tid, job=None)
                self.finish(proj, tid)
            elif code == 2 or time.time() - job["started"] > job["timeout"]:
                core.emit("async_failed", project=pid, task=tid, detail=f"{job['id']} 失败", job=job["id"])
                core.update_card(pid, tid, job=None)
                self.retry_or_fail(proj, tid, problem(f"异步任务 {job['id']}", out.strip()[-600:] or "检查命令报告失败，没有输出",
                                                      "检查提交参数后重新提交任务。"))
            else:
                job["next_poll"] = time.time() + job["every"]
                core.update_card(pid, tid, job=job)
        finally:
            self.polling.discard(f"{pid}/{tid}")

    # ------------------------------------------------------------ checks and outcome
    def finish(self, proj, tid):
        pid = proj["id"]
        card = core.normalize_card(core.load_card(pid, tid))
        if card.get("check"):
            core.update_card(pid, tid, status="checking")
            core.emit("check_start", project=pid, task=tid, detail=card["check"][:80])
            code, out = shell(card["check"], proj["repo"], card.get("check_timeout", 600))
            if code != 0:
                core.emit("check_fail", project=pid, task=tid, detail=(out.strip().splitlines() or [f"校验未通过（退出码 {code}）"])[-1][:120],
                          to=card.get("agent"))
                self.retry_or_fail(proj, tid, problem(f"校验命令 `{card['check']}`（退出码 {code}）",
                                                      out.strip()[-800:] or "命令没有输出",
                                                      "按原因修正产物后重新提交，不要改校验命令。"))
                return
            core.emit("check_pass", project=pid, task=tid, detail=(out.strip().splitlines() or ["通过"])[-1][:120])
        if card.get("review"):
            core.update_card(pid, tid, status="waiting_human")
            core.emit("waiting_human", project=pid, agent="human", task=tid, detail="等你审阅")
            return
        self.complete(pid, tid)

    def complete(self, pid, tid, by=None):
        card = core.update_card(pid, tid, status="done", completed=time.time())
        nxt = [c for c in core.cards(pid) if tid in c.get("depends_on", [])]
        to = nxt[0].get("assignee") if nxt else "runner"
        core.emit("done", project=pid, agent=by or card.get("agent", "runner"), task=tid, detail=card["title"])
        core.emit("handoff", project=pid, agent=by or card.get("agent", "runner"), task=tid,
                  detail=f"交接总结 → {to}", to=to, handoff=f"handoffs/{tid}.md")

    def retry_or_fail(self, proj, tid, feedback):
        pid = proj["id"]
        card = core.normalize_card(core.load_card(pid, tid))
        if card["attempts"] >= card["max_attempts"]:
            core.update_card(pid, tid, status="failed", feedback=feedback)
            core.emit("failed", project=pid, task=tid, agent=card.get("agent", "runner"),
                      detail=f"试了 {card['attempts']} 次仍未通过，需要你处理")
        else:
            core.update_card(pid, tid, status="open", feedback=feedback, assignee=card.get("agent") or card.get("assignee"))
            core.emit("bounce", project=pid, task=tid, detail=f"打回第 {card['attempts']} 次", to=card.get("agent"))


# ---------------------------------------------------------------- helpers

def problem(where: str, cause: str, fix: str) -> str:
    """Every failure is reported the same way: where it happened, why, and what to do."""
    return f"位置：{where}\n原因：{cause}\n修复：{fix}"


def shell(cmd: str, cwd: str, timeout: int) -> tuple[int, str]:
    Path(cwd).mkdir(parents=True, exist_ok=True)
    try:
        p = subprocess.run(cmd, shell=True, cwd=cwd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout + p.stderr)
    except subprocess.TimeoutExpired:
        return 124, f"超时（{timeout} 秒）"


def wb_command() -> str:
    return shlex.quote(str(core.ROOT / "wb"))


def build_prompt(proj, card, aid, acfg) -> str:
    tpl_path = core.ROOT / "templates" / "prompt.md"
    tpl = tpl_path.read_text("utf-8") if tpl_path.exists() else DEFAULT_PROMPT
    deps = []
    for d in card.get("depends_on", []):
        h = core.read_handoff(proj["id"], d)
        if h:
            deps.append(f"### {d}\n{h.strip()}")
    public = {k: v for k, v in card.items() if k not in ("mock", "feedback", "updated", "created", "job", "agent")}
    fields = {
        "agent_name": acfg.get("name", aid), "role": acfg.get("role", ""),
        "project_title": proj["title"], "project": proj["id"], "cwd": proj["repo"],
        "card": json.dumps(public, ensure_ascii=False, indent=2),
        "dep_handoffs": "\n\n".join(deps) or "（无）",
        "feedback": card.get("feedback") or "（无）",
        "task": card["id"], "wb": wb_command(),
    }
    for k, v in fields.items():
        tpl = tpl.replace("{{" + k + "}}", str(v))
    return tpl


DEFAULT_PROMPT = "{{card}}\n\n请在最后输出 <handoff> 交接总结。"


# ---------------------------------------------------------------- query_log

def query_log(pid: str, tid: str, question: str, asker: str = "agent") -> str:
    """Let a cheap model read the long logs and answer one question in a few sentences."""
    cfg = core.config()
    reader = cfg.get("log_reader", {})
    rid = reader.get("agent_id", "qwen")
    events = core.read_events(pid, tid)
    ev_text = "\n".join(f"log#{e['n']} {e['agent']} {e['type']} {e['detail']}" for e in events)
    runs = sorted((core.pdir(pid) / "runs").glob(f"{tid}.*.log"))
    raw = "\n\n".join(f"== {r.name} ==\n{r.read_text('utf-8', 'ignore')[-8000:]}" for r in runs[-3:])
    card = core.load_card(pid, tid) or {}
    material = f"事件：\n{ev_text}\n\n最近的校验反馈：\n{card.get('feedback') or '无'}\n\n原始输出：\n{raw}"[-24000:]
    core.emit("query_log", project=pid, agent=rid, task=tid, detail=question, asker=asker,
              chars=len(material))
    try:
        answer = agents.chat(reader.get("model", "Qwen/Qwen3.8-27B"), [
            {"role": "system", "content": "你在帮另一个 agent 查日志。只根据给出的材料回答，不超过 150 字，引用 log#编号。材料里没有就直说没有。"},
            {"role": "user", "content": f"问题：{question}\n\n{material}"}], max_tokens=400,
            params=reader.get("params"))
    except Exception as e:
        hits = [e for e in events if e["type"] in ("check_fail", "bounce", "error", "quota_out", "reassign", "async_failed")]
        answer = ("（没连上小模型：" + str(e)[:60] + "，以下是相关原始事件）\n" +
                  ("\n".join(f"log#{h['n']} {h['type']}：{h['detail']}" for h in hits) or "没有找到打回或报错记录"))
    core.emit("answer", project=pid, agent=rid, task=tid, detail=answer[:200], to=asker)
    return answer
