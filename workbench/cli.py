"""./wb command line."""
from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import sys
import threading
import time
import webbrowser
from pathlib import Path

from . import core

STATUS_ZH = {"open": "待办", "running": "进行中", "waiting_job": "等异步任务", "checking": "校验中",
             "waiting_human": "等你处理", "done": "完成", "failed": "失败"}


def cmd_init(a):
    root = core.ROOT
    for d in ("projects", "logs", "templates", "dashboard"):
        (root / d).mkdir(parents=True, exist_ok=True)
    if not (root / "workbench.json").exists():
        shutil.copy(root / "workbench.example.json", root / "workbench.json")
        print("已生成 workbench.json，按需修改各 agent 的命令。")
    print(f"工作台位置：{root}")


def cmd_new_project(a):
    pid = a.id
    if not pid.replace("-", "").replace("_", "").isalnum():
        raise SystemExit("项目 id 只能用字母、数字、- 和 _")
    d = core.pdir(pid)
    if (d / "project.json").exists():
        raise SystemExit(f"项目 {pid} 已存在")
    data = {"title": a.title or pid, "priority": a.priority, "color": a.color or "#3E7CB1"}
    if a.repo:   # left out: projects/<id>/workspace, kept relative so the folder can be moved
        data["repo"] = str(Path(a.repo).expanduser().resolve())
    for sub in ("board", "handoffs", "runs", "contracts"):
        (d / sub).mkdir(parents=True, exist_ok=True)
    core.write_json(d / "project.json", data)
    repo = core.project(pid)["repo"]
    Path(repo).mkdir(parents=True, exist_ok=True)
    core.emit("project_created", project=pid, detail=data["title"])
    print(f"已创建项目 {pid}：agent 的工作目录是 {repo}")


def _add_card(pid, c):
    if pid not in core.project_ids():
        raise SystemExit(f"项目 {pid} 不存在，先运行 ./wb new-project {pid}")
    c = core.normalize_card(dict(c))
    if not c.get("id") or not c.get("assignee"):
        raise SystemExit("任务卡至少要有 id 和 assignee")
    if core.load_card(pid, c["id"]):
        raise SystemExit(f"{pid}/{c['id']} 已存在")
    if c["assignee"] not in core.config()["agents"] and c["mode"] != "manual":
        raise SystemExit(f"assignee {c['assignee']} 不在 workbench.json 的 agents 里")
    core.save_card(pid, c)
    core.emit("card_added", project=pid, task=c["id"], detail=c["title"])
    print(f"已添加 {pid}/{c['id']}")


def cmd_add(a):
    data = json.load(sys.stdin if a.file == "-" else open(a.file, encoding="utf-8"))
    for c in (data if isinstance(data, list) else [data]):
        _add_card(a.project, c)


def cmd_card(a):
    c = {"id": a.id, "assignee": a.assignee, "goal": a.goal, "title": a.title or a.goal[:24],
         "depends_on": [x for x in (a.after or "").split(",") if x],
         "fallback": [x for x in (a.fallback or "").split(",") if x],
         "refs": [x for x in (a.refs or "").split(",") if x]}
    if a.check:
        c["check"] = a.check
    if a.manual:
        c["mode"] = "manual"
    if a.review:
        c["review"] = True
    _add_card(a.project, c)


def cmd_status(a):
    pids = [a.project] if a.project else core.project_ids()
    todo = [(pid, c) for pid in pids for c in core.cards(pid) if c.get("status") in ("waiting_human", "failed")]
    if todo:
        pid, c = todo[0]
        more = f"（另有 {len(todo) - 1} 件）" if len(todo) > 1 else ""
        print(f"下一步：在看板收件箱里处理 {pid}/{c['id']}「{c.get('title', c['id'])}」{more}")
    else:
        print("下一步：没有需要你处理的事。")
    for pid in pids:
        p = core.project(pid)
        print(f"\n■ {p['title']}（{pid}）  工作目录 {p['repo']}")
        for c in core.cards(pid):
            c = core.normalize_card(c)
            who = c.get("agent") or c["assignee"]
            dep = f"  ← {','.join(c['depends_on'])}" if c["depends_on"] else ""
            print(f"  {STATUS_ZH.get(c['status'], c['status']):<6} {c['id']:<18} {who:<8} {c['title']}{dep}")
    live = core.read_json(core.ROOT / "logs" / "agents.json", {}) or {}
    if live:
        print("\nagent：")
        for aid, s in live.items():
            cd = s.get("cooldown_until", 0) - time.time()
            extra = f"  冷却中 {int(cd // 60)} 分钟" if cd > 0 else ""
            print(f"  {aid:<8} 忙碌 {s['busy']}  已运行 {s['runs']} 次  {' '.join(s['tasks'])}{extra}")


def _lock():
    lock = core.ROOT / "logs" / "runner.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    if lock.exists():
        try:
            pid = int(lock.read_text())
            os.kill(pid, 0)
            raise SystemExit(f"runner 已经在运行（进程 {pid}）")
        except (ValueError, ProcessLookupError, PermissionError):
            pass
    lock.write_text(str(os.getpid()))
    return lock


def cmd_run(a):
    from . import runner, server
    cfg = core.config()
    lock = _lock()
    stop = threading.Event()
    port = a.port or cfg.get("port", 8765)
    server.serve(port, stop)
    url = f"http://127.0.0.1:{port}/"
    print(f"工作台已启动：{url}  （Ctrl+C 停止）")
    if not a.no_open:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    r = runner.Runner()

    def bye(*_):
        r.stop.set()
        stop.set()
    signal.signal(signal.SIGINT, bye)
    signal.signal(signal.SIGTERM, bye)
    try:
        r.run_forever()
    finally:
        core.emit("runner_stop")
        lock.unlink(missing_ok=True)


def cmd_serve(a):
    from . import server
    stop = threading.Event()
    port = a.port or core.config().get("port", 8765)
    server.serve(port, stop)
    print(f"只看板模式：http://127.0.0.1:{port}/  （Ctrl+C 停止）")
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass


def cmd_query_log(a):
    from . import runner
    print(runner.query_log(a.project, a.task, a.question, asker=os.environ.get("WB_AGENT", "agent")))


def _act(fn, *args):
    from . import actions
    try:
        fn(*args)
    except actions.ActionError as e:
        raise SystemExit(str(e))


def cmd_done(a):
    from . import actions
    _act(actions.done, a.project, a.task, a.note or "", a.next or "", a.artifacts or [])
    print("已标记完成")


def cmd_approve(a):
    from . import actions
    _act(actions.approve, a.project, a.task, a.comment or "")
    print("已通过")


def cmd_reject(a):
    from . import actions
    _act(actions.reject, a.project, a.task, a.reason)
    print("已打回")


def cmd_answer(a):
    from . import actions
    _act(actions.answer, a.project, a.task, a.decision)
    print("已把决定交给 agent")


def cmd_retry(a):
    from . import actions
    _act(actions.retry, a.project, a.task, a.note or "")
    print("已重新排队")


def cmd_doctor(a):
    import shutil as sh
    cfg = core.config()
    ok = True
    for aid, ag in cfg["agents"].items():
        kind = ag.get("kind", "cli")
        if kind == "cli":
            exe = ag["cmd"][0]
            found = sh.which(exe)
            print(f"  {'✓' if found else '✗'} {aid:<8} {exe} {found or '没找到，检查是否安装或在 PATH 里'}")
            ok &= bool(found)
        elif kind == "siliconflow":
            env = cfg.get("siliconflow", {}).get("api_key_env", "SILICONFLOW_API_KEY")
            from . import agents
            has = bool(agents.api_key(cfg.get("siliconflow", {})))
            print(f"  {'✓' if has else '✗'} {aid:<8} {ag['model']}  {'' if has else '需要设置环境变量 ' + env + '，或存进钥匙串'}")
            ok &= has
        else:
            print(f"  · {aid:<8} {kind}")
    print("\n全部就绪" if ok else "\n有未就绪的项；不影响其他 agent 运行")


def cmd_demo(a):
    """Create a self-contained demo workspace with simulated agents and two projects."""
    src = core.ROOT
    dst = Path(a.dir or (src / "demo")).resolve()
    if dst.exists():
        shutil.rmtree(dst)
    dst.mkdir(parents=True)
    for d in ("templates", "dashboard", "workbench"):
        shutil.copytree(src / d, dst / d, ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copy(src / "wb", dst / "wb")
    cfg = json.loads((src / "workbench.example.json").read_text("utf-8"))
    for aid, ag in cfg["agents"].items():
        ag["kind"] = "mock"
        ag["cooldown_minutes"] = 0.5
    cfg["poll_seconds"] = 1
    core.set_root(dst)
    core.write_json(dst / "workbench.json", cfg)
    demo = json.loads((src / "templates" / "demo.json").read_text("utf-8"))
    for proj in demo:
        ns = argparse.Namespace(id=proj["id"], title=proj["title"], repo=None, priority=proj.get("priority", 0),
                                color=proj["color"])
        cmd_new_project(ns)
        for c in proj["cards"]:
            _add_card(proj["id"], c)
    print(f"\n演示工作台已生成：{dst}\n运行：{dst / 'wb'} run")


def main(argv=None):
    ap = argparse.ArgumentParser(prog="wb", description="多 agent 工作台")
    ap.add_argument("--root", help="工作台目录（默认是 wb 所在目录）")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init", help="初始化工作台").set_defaults(f=cmd_init)
    p = sub.add_parser("new-project", help="新建项目")
    p.add_argument("id"); p.add_argument("--title"); p.add_argument("--repo", help="agent 的工作目录，通常是项目代码仓库")
    p.add_argument("--priority", type=int, default=0); p.add_argument("--color")
    p.set_defaults(f=cmd_new_project)
    p = sub.add_parser("add", help="从 JSON 文件添加任务卡（单张或数组，- 表示标准输入）")
    p.add_argument("project"); p.add_argument("file"); p.set_defaults(f=cmd_add)
    p = sub.add_parser("card", help="快速添加一张任务卡")
    p.add_argument("project"); p.add_argument("id"); p.add_argument("assignee"); p.add_argument("goal")
    p.add_argument("--title"); p.add_argument("--after", help="依赖的任务，逗号分隔")
    p.add_argument("--fallback", help="备用 agent，逗号分隔"); p.add_argument("--refs", help="契约文件，逗号分隔")
    p.add_argument("--check", help="校验命令，在工作目录里运行，退出码 0 为通过")
    p.add_argument("--manual", action="store_true", help="手动任务，由你在网页版完成后 ./wb done")
    p.add_argument("--review", action="store_true", help="通过校验后先等你审阅")
    p.set_defaults(f=cmd_card)
    p = sub.add_parser("status", help="查看看板"); p.add_argument("project", nargs="?"); p.set_defaults(f=cmd_status)
    p = sub.add_parser("run", help="启动 runner 和看板"); p.add_argument("--port", type=int)
    p.add_argument("--no-open", action="store_true"); p.set_defaults(f=cmd_run)
    p = sub.add_parser("serve", help="只启动看板，不派任务"); p.add_argument("--port", type=int); p.set_defaults(f=cmd_serve)
    p = sub.add_parser("query-log", help="让小模型读日志回答一个问题")
    p.add_argument("project"); p.add_argument("task"); p.add_argument("question"); p.set_defaults(f=cmd_query_log)
    p = sub.add_parser("done", help="手动任务完成")
    p.add_argument("project"); p.add_argument("task"); p.add_argument("--note"); p.add_argument("--next")
    p.add_argument("--artifacts", nargs="*"); p.set_defaults(f=cmd_done)
    p = sub.add_parser("approve", help="审阅通过"); p.add_argument("project"); p.add_argument("task")
    p.add_argument("--comment", help="审阅意见，会写进交接总结给下一棒看"); p.set_defaults(f=cmd_approve)
    p = sub.add_parser("reject", help="审阅打回"); p.add_argument("project"); p.add_argument("task"); p.add_argument("reason")
    p.set_defaults(f=cmd_reject)
    p = sub.add_parser("answer", help="agent 等你决定时，把决定交给它"); p.add_argument("project"); p.add_argument("task")
    p.add_argument("decision"); p.set_defaults(f=cmd_answer)
    p = sub.add_parser("retry", help="失败的任务重新排队"); p.add_argument("project"); p.add_argument("task")
    p.add_argument("--note", help="补充说明"); p.set_defaults(f=cmd_retry)
    sub.add_parser("doctor", help="检查各 agent 是否可用").set_defaults(f=cmd_doctor)
    p = sub.add_parser("demo", help="生成带模拟 agent 的演示工作台"); p.add_argument("--dir"); p.set_defaults(f=cmd_demo)
    a = ap.parse_args(argv)
    if a.root:
        core.set_root(a.root)
    try:
        a.f(a)
    except BrokenPipeError:      # output piped into head, less, etc.
        sys.stderr.close()


if __name__ == "__main__":
    main()
