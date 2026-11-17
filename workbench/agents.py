"""Agent adapters. Each one runs one card attempt and returns the raw output.

kinds:
  cli          a local command line agent (Claude Code, Codex, Kimi Code ...)
  siliconflow  an OpenAI compatible chat model on SiliconFlow (or any compatible base_url)
  mock         a simulated agent for trying the workbench without spending quota
"""
from __future__ import annotations

import json
import os
import random
import re
import subprocess
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from . import core


@dataclass
class Result:
    exit_code: int
    output: str
    quota_out: bool = False      # out of plan quota or rate limited: cool the agent down, hand card to fallback
    transient: bool = False      # temporary failure: retry later without counting an attempt
    auth_failed: bool = False    # not logged in / token expired: stop at once, the human has to log in


AUTH_PATTERNS = ["authentication_failed", "oauth access token has expired", "re-authenticate",
                 "not logged in", "please log in", "login required", "invalid api key", "api_error_status\":401"]


class Throttle:
    """Turn a stream of tool calls into at most one event every few seconds."""

    def __init__(self, emit_fn, every=2.5):
        self.emit_fn, self.every, self.last, self.pending = emit_fn, every, 0.0, None

    def __call__(self, detail: str):
        t = time.time()
        if t - self.last >= self.every:
            self.last, self.pending = t, None
            self.emit_fn(detail)
        else:
            self.pending = detail

    def flush(self):
        if self.pending:
            self.emit_fn(self.pending)
            self.pending = None


# ------------------------------------------------------------- tool call sniffing

def _short(v, n=60) -> str:
    if isinstance(v, list):
        v = " ".join(str(x) for x in v)
    if isinstance(v, dict):
        v = next((v[k] for k in ("command", "file_path", "path", "pattern", "url", "prompt") if k in v), "")
    s = re.sub(r"\s+", " ", str(v)).strip()
    return s[:n] + ("…" if len(s) > n else "")


def sniff_tool(obj) -> str | None:
    """Best effort: find a tool call in one JSON line from Claude Code, Codex or Kimi Code."""
    hit = None

    def walk(v):
        nonlocal hit
        if hit is not None:
            return
        if isinstance(v, dict):
            t = v.get("type")
            if t == "tool_use" and "name" in v:                       # Claude Code stream-json
                hit = f"{v['name']} {_short(v.get('input', ''))}".strip()
            elif t in ("command_execution", "exec_command_begin") and "command" in v:   # Codex --json
                hit = f"shell {_short(v['command'])}"
            elif t in ("mcp_tool_call",) and "tool" in v:
                hit = f"{v.get('server', 'mcp')}.{v['tool']}"
            elif t == "file_change":
                hit = "edit " + _short(v.get("changes", v.get("path", "")))
            elif isinstance(v.get("function"), dict) and "name" in v["function"]:  # Kimi / OpenAI style
                args = v["function"].get("arguments", "")
                try:
                    args = json.loads(args) if isinstance(args, str) else args
                except json.JSONDecodeError:
                    pass
                hit = f"{v['function']['name']} {_short(args)}".strip()
            else:
                for x in v.values():
                    walk(x)
        elif isinstance(v, list):
            for x in v:
                walk(x)

    walk(obj)
    return hit


# ------------------------------------------------------------- cli

def run_cli(agent_id, acfg, card, proj, prompt, on_tool, log_path: Path) -> Result:
    cwd = proj["repo"]
    Path(cwd).mkdir(parents=True, exist_ok=True)
    outfile = str(log_path.with_suffix(".final.txt"))
    cmd = [a.replace("{prompt}", prompt).replace("{cwd}", cwd).replace("{outfile}", outfile)
           for a in acfg["cmd"]]
    env = dict(os.environ, **acfg.get("env", {}))
    try:
        p = subprocess.Popen(cmd, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
    except FileNotFoundError:
        return Result(127, f"找不到命令 {cmd[0]}，检查 workbench.json 里 {agent_id} 的 cmd", transient=False)

    timeout = acfg.get("timeout", 1800)
    killer = threading.Timer(timeout, p.kill)
    killer.start()
    chunks = []
    with log_path.open("w", encoding="utf-8") as log:
        for line in p.stdout:
            chunks.append(line)
            log.write(line)
            log.flush()
            s = line.strip()
            if s.startswith("{"):
                try:
                    tool = sniff_tool(json.loads(s))
                except json.JSONDecodeError:
                    tool = None
                if tool:
                    on_tool(tool)
    p.wait()
    killer.cancel()
    output = "".join(chunks)
    if Path(outfile).exists():                       # e.g. codex exec -o {outfile}
        output += "\n" + Path(outfile).read_text("utf-8")
    return classify(acfg, p.returncode, output)


def classify(acfg, code: int, output: str) -> Result:
    tail = output[-4000:].lower()
    quota = any(pat.lower() in tail for pat in acfg.get("quota_patterns", []))
    transient = code in acfg.get("transient_exit_codes", []) or any(
        pat.lower() in tail for pat in acfg.get("transient_patterns", []))
    auth = any(pat.lower() in tail for pat in acfg.get("auth_patterns", AUTH_PATTERNS))
    return Result(code, output, quota_out=quota and code != 0 and not auth,
                  transient=transient and code != 0 and not auth, auth_failed=auth and code != 0)


# ------------------------------------------------------------- siliconflow / openai compatible

def api_key(cfg: dict | None = None) -> str:
    """Env var first; on macOS fall back to the login Keychain (service = env var name)."""
    cfg = core.config().get("siliconflow", {}) if cfg is None else cfg
    name = cfg.get("api_key_env", "SILICONFLOW_API_KEY")
    if os.environ.get(name):
        return os.environ[name]
    try:
        r = subprocess.run(["security", "find-generic-password", "-a", cfg.get("keychain_account", "agent-workbench"),
                            "-s", name, "-w"], capture_output=True, text=True, timeout=10)
        return r.stdout.strip() if r.returncode == 0 else ""
    except (OSError, subprocess.TimeoutExpired):
        return ""


def chat(model: str, messages: list[dict], max_tokens=2048, temperature=0.3, params: dict | None = None) -> str:
    cfg = core.config().get("siliconflow", {})
    key = api_key(cfg)
    if not key:
        name = cfg.get("api_key_env", "SILICONFLOW_API_KEY")
        raise RuntimeError(f"没有找到 SiliconFlow 密钥：设置环境变量 {name}，或存进钥匙串（服务名 {name}）")
    body = json.dumps({"model": model, "messages": messages, "max_tokens": max_tokens,
                       "temperature": temperature, **(params or {})}).encode()
    req = urllib.request.Request(cfg.get("base_url", "https://api.siliconflow.com/v1").rstrip("/") + "/chat/completions",
                                 data=body, headers={"Authorization": f"Bearer {key}",
                                                     "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=180) as r:
        data = json.loads(r.read())
    return data["choices"][0]["message"]["content"]


def run_siliconflow(agent_id, acfg, card, proj, prompt, on_tool, log_path: Path) -> Result:
    on_tool(f"chat.completions {acfg['model']}")
    try:
        text = chat(acfg["model"], [{"role": "user", "content": prompt}], max_tokens=acfg.get("max_tokens", 4096),
                    params=acfg.get("params"))
    except urllib.error.HTTPError as e:
        msg = e.read().decode("utf-8", "ignore")
        log_path.write_text(msg, "utf-8")
        return Result(e.code, msg, quota_out=e.code in (402, 429), transient=e.code >= 500)
    except Exception as e:  # network, missing key
        log_path.write_text(str(e), "utf-8")
        return Result(1, str(e), transient=isinstance(e, (urllib.error.URLError, TimeoutError)))
    log_path.write_text(text, "utf-8")
    return Result(0, text)


# ------------------------------------------------------------- mock

MOCK_TOOLS = {
    "claude": ["Read 规格与交接总结", "Grep 相关代码", "Write 方案", "Edit 场景代码"],
    "codex": ["qwen_image 参考图", "shell python3 scripts/normalize.py", "hi3d.submit"],
    "kimi": ["ReadFile spec.md", "qwen_image 参考图", "hi3d.submit"],
}


def run_mock(agent_id, acfg, card, proj, prompt, on_tool, log_path: Path) -> Result:
    """Simulates a CLI agent. Behaviour comes from the card's optional "mock" block:
       {"seconds": 6, "fail_first": true, "quota_out": ["codex"], "job_seconds": 8,
        "artifacts": {"assets/x.glb": "ok"}, "ask": "为什么被打回过？"}"""
    m = card.get("mock", {})
    speed = float(os.environ.get("WB_MOCK_SPEED", "1"))
    nap = lambda s: time.sleep(s / speed)
    if agent_id in m.get("quota_out", []):
        nap(1.5)
        out = "Error: You've hit your usage limit. Try again later."
        log_path.write_text(out, "utf-8")
        return classify(acfg, 1, out)

    total = m.get("seconds", random.uniform(4, 7))
    tools = m.get("tools") or MOCK_TOOLS.get(agent_id, ["read", "edit", "write"])
    for t in tools:
        nap(total / (len(tools) + 1))
        on_tool(t)

    if m.get("ask"):
        from . import runner
        answer = runner.query_log(proj["id"], m.get("ask_task", card["id"]), m["ask"], asker=agent_id)
        on_tool("query_log → " + answer[:40])
        nap(1)

    attempt = card.get("attempts", 1)
    repo = Path(proj["repo"])
    for rel, content in m.get("artifacts", {}).items():
        f = repo / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("bad" if (m.get("fail_first") and attempt == 1) else content, "utf-8")
    nap(total / (len(tools) + 1))

    if m.get("blocked_first") and attempt == 1:
        b = m["blocked_first"]
        opts = "\n".join(f"{i}. {o}" for i, o in enumerate(b["options"], 1))
        out = ("<handoff>\n---\nstatus: \"blocked\"\nartifacts: " + json.dumps(b.get("artifacts", []), ensure_ascii=False)
               + "\n---\n## 需要你决定\n" + b["question"] + "\n" + opts + "\n## 下一步\n在收件箱里写下选哪一个。\n</handoff>\n")
        log_path.write_text(out, "utf-8")
        return Result(0, out)
    meta = {"status": "done", "artifacts": list(m.get("artifacts", {}))}
    if m.get("job_seconds"):
        deadline = int(time.time() + m["job_seconds"] / speed)
        meta["job"] = {"id": f"hj_{random.randint(1000, 9999)}",
                       "poll": f"test $(date +%s) -ge {deadline}", "every": 1}
    s = m.get("summary", {})
    retry = m.get("fail_first") and attempt > 1
    arts = list(m.get("artifacts", {}))
    body = "\n".join([
        "## 下一步", s.get("next", "可以直接接手，从产物开始。"),
        "## 现在能用的", s.get("did", f"「{card['title']}」已完成。") + (f" 验证：打开 `{arts[0]}`。" if arts else ""),
        "## 关键决策与原因", s.get("why", "按任务卡规格执行。"),
        "## 试过但放弃的", s.get("dropped", "第一次的做法没通过校验，已改正。" if retry else "无"),
        "## 未解决 / 风险", s.get("risks", "无"),
    ])
    out = "<handoff>\n---\n" + "\n".join(f"{k}: {json.dumps(v, ensure_ascii=False)}" for k, v in meta.items()) \
          + "\n---\n" + body + "\n</handoff>\n"
    log_path.write_text(out, "utf-8")
    return Result(0, out)


ADAPTERS = {"cli": run_cli, "siliconflow": run_siliconflow, "mock": run_mock}


def run(agent_id, acfg, card, proj, prompt, on_tool, log_path: Path) -> Result:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    return ADAPTERS[acfg.get("kind", "cli")](agent_id, acfg, card, proj, prompt, on_tool, log_path)
