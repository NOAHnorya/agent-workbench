"""Human decisions on cards. Used by both the ./wb command line and the dashboard buttons."""
from __future__ import annotations

from . import core


class ActionError(Exception):
    pass


def add(pid, title, agent=""):
    """Create one real task card from the dashboard's quick-add form."""
    if pid not in core.project_ids():
        raise ActionError(f"项目 {pid} 不存在")
    title = title.strip()
    if not title:
        raise ActionError("先写下要做什么")
    agents = core.config().get("agents", {})
    if not agent:
        agent = "claude" if "claude" in agents and not agents["claude"].get("disabled") else next(
            (aid for aid, a in agents.items() if not a.get("disabled") and a.get("kind", "cli") == "cli"), "")
    if agent not in agents or agents[agent].get("disabled"):
        raise ActionError("没有可用的执行 agent，请检查 workbench.json")
    tid = f"task_{int(core.now() * 1000)}"
    while core.load_card(pid, tid):
        tid += "x"
    card = core.normalize_card({"id": tid, "title": title[:120], "assignee": agent, "goal": title})
    core.save_card(pid, card)
    core.emit("card_added", project=pid, task=tid, agent="human", detail=card["title"], to=agent)
    return card


def _card(pid, tid, statuses):
    if pid not in core.project_ids():
        raise ActionError(f"项目 {pid} 不存在")
    c = core.load_card(pid, tid)
    if not c:
        raise ActionError(f"{pid}/{tid} 不存在")
    if c.get("status") not in statuses:
        raise ActionError(f"{tid} 现在的状态不能这样操作，刷新一下看看")
    return c


def _append_review(pid, tid, comment):
    """Put the reviewer's note into the handoff so the next agent reads it."""
    p = core.handoff_path(pid, tid)
    if comment and p.exists():
        p.write_text(p.read_text("utf-8").rstrip() + f"\n## 审阅意见\n{comment.strip()}\n", "utf-8")


def approve(pid, tid, comment=""):
    from .runner import Runner
    c = _card(pid, tid, ("waiting_human",))
    if core.is_blocked(pid, c):
        raise ActionError("这张卡在问你问题，不是等你验收。选一个选项后点「定了」交给 agent，它才会接着做。")
    _append_review(pid, tid, comment)
    core.emit("review", project=pid, task=tid, agent="human", detail="通过" + (f"：{comment.strip()[:80]}" if comment else ""))
    Runner().complete(pid, tid)


def reject(pid, tid, reason):
    if not reason or not reason.strip():
        raise ActionError("打回时请写清楚要改什么")
    c = _card(pid, tid, ("waiting_human", "failed", "done"))
    core.update_card(pid, tid, status="open", feedback=f"位置：主人的审阅\n原因：{reason.strip()}\n修复：按这条意见修改后重新提交。",
                     max_attempts=max(c.get("max_attempts", 3), c.get("attempts", 0) + 1))
    core.emit("bounce", project=pid, task=tid, agent="human", detail=reason.strip()[:120], to=c.get("agent"))


def answer(pid, tid, decision):
    """The agent asked the owner to decide something; hand the decision back and let it continue."""
    if not decision or not decision.strip():
        raise ActionError("先写下你的决定")
    c = _card(pid, tid, ("waiting_human",))
    core.update_card(pid, tid, status="open", feedback=f"主人的决定：{decision.strip()}\n按这个决定继续完成任务。",
                     max_attempts=max(c.get("max_attempts", 3), c.get("attempts", 0) + 1))
    core.emit("decision", project=pid, task=tid, agent="human", detail=decision.strip()[:120], to=c.get("agent"))


def done(pid, tid, note="", next_="", artifacts=None):
    from .runner import Runner
    _card(pid, tid, ("waiting_human", "open", "failed"))
    body = "## 下一步\n" + (next_.strip() or "可以直接接手。") + "\n## 现在能用的\n" + (note.strip() or "手动完成。")
    core.write_handoff(pid, tid, "human", {"status": "done", "artifacts": artifacts or []}, body, [])
    Runner().complete(pid, tid, by="human")


def retry(pid, tid, note=""):
    c = _card(pid, tid, ("failed", "waiting_human"))
    fields = {"status": "open", "max_attempts": c.get("attempts", 0) + 2}
    if note.strip():
        fields["feedback"] = f"重新排队时你补充的说明：\n{note.strip()}"
    core.update_card(pid, tid, **fields)
    core.emit("retry", project=pid, task=tid, agent="human", detail=note.strip()[:120] or "手动重试")
