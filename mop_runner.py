"""
mop_runner.py — a minimal Python stand-in for the Nokia Java CLI-CR engine, so
you can validate the simulator locally without booting the real JVM engine.

What it does:
  * Parses the same workflow YAML the sim consumes.
  * Opens ONE persistent SSH shell per remote-node (niam_sbc_server / repo_server)
    against the running simulator.
  * Walks phases -> steps in order (loops are unrolled once — the runner is a
    smoke-tester, not a full engine):
      - substitutes ${VAR} in `send:` from the current binding table,
      - sends the command, waits for the node prompt (or step's prompt_regex),
      - captures named groups from `register.regex` into the binding table,
      - evaluates `validation.success.criteria.expr` in a tiny sandbox and
        prints PASS / WARN / FAIL per step,
      - honours `on_failure: stop` by aborting the run.
  * Prints a compact summary at the end.

Usage:
    # in one terminal:
    python clicr_sim.py --port 2222 --workflow workflows/SBC_96_FIXED_LINE_CONFIGURATION_IN_SBC.yaml -v

    # in another:
    python mop_runner.py --port 2222 \
        --workflow workflows/SBC_96_FIXED_LINE_CONFIGURATION_IN_SBC.yaml \
        --var NODE_NAME=SBC-1 --var ORDER_NO=ORD1 --var CHILD_REQ_ID=REQ1 \
        --var REPO_IP=127.0.0.1 --var REPO_USER=installer --var REPO_PASSWORD=x \
        --var niamID=nei1 --var ROLLBACK_ONLY=false

This is deliberately not a full JSch replica — it doesn't attempt loops with
runtime iteration counts, retry loops, or SFTP subsystem operations. It's an
end-to-end smoke check to prove the sim's workflow-driven output pipeline works.
"""

from __future__ import annotations

import argparse
import re
import socket
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

import paramiko
import yaml


VAR_RE = re.compile(r"\$\{([^}]+)\}")


def subst(text: str, bindings: Dict[str, str]) -> str:
    def repl(m):
        expr = m.group(1).strip()
        # trivial ${VAR} — no ternary support
        return str(bindings.get(expr, "${" + expr + "}"))
    return VAR_RE.sub(repl, text)


_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z_0-9.]*")


def eval_expr(expr: str, bindings: Dict[str, str]) -> bool:
    """Evaluate the '${A == "" && B != ""}' patterns from validation/when.

    Convention: the whole expression is wrapped in ${...}. Inside, variables
    are BARE identifiers (not ${VAR}), operators are && / || / == / !=.

    Strategy:
      * strip outer ${...}
      * replace each bare identifier with its bindings value as a Python string
        literal (unset -> "")
      * swap && / || for and / or
      * eval in a sandbox
    """
    e = str(expr).strip()
    m = re.match(r"^\$\{(.*)\}$", e, re.DOTALL)
    if m:
        e = m.group(1)
    e = e.replace("&&", " and ").replace("||", " or ")

    # Do NOT touch identifiers inside string literals. Split on quoted strings.
    parts = re.split(r'("(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\')', e)
    for i, seg in enumerate(parts):
        if seg.startswith('"') or seg.startswith("'"):
            continue
        def repl_ident(m2):
            ident = m2.group(0)
            if ident in ("and", "or", "not", "True", "False", "None", "in"):
                return ident
            # Only look up known bindings; drop dotted refs (a.b) safely -> ""
            val = bindings.get(ident, "")
            return repr(val)
        parts[i] = _IDENT_RE.sub(repl_ident, seg)
    e2 = "".join(parts)
    try:
        return bool(eval(e2, {"__builtins__": {}}, {}))
    except Exception:
        return False


PROMPT_ANY = re.compile(r"(#\s*$)|(\$\s*$)")


class ShellClient:
    """One persistent SSH shell, with a line-buffered receive loop."""

    def __init__(self, host: str, port: int, user: str, password: str,
                 profile: str, log):
        self.host = host
        self.port = port
        self.user = user
        self.password = password
        self.profile = profile
        self.log = log
        self.transport: Optional[paramiko.Transport] = None
        self.chan: Optional[paramiko.Channel] = None
        self._buf = b""

    def connect(self, interactive_prompts: List[str]):
        t = paramiko.Transport((self.host, self.port))
        t.connect(username=self.user, password=self.password)
        chan = t.open_session()
        chan.get_pty()
        chan.invoke_shell()
        self.transport = t
        self.chan = chan
        # Consume any interactive-login prompts (SBC MOTD/editor)
        for exp in interactive_prompts or []:
            self._read_until(exp, timeout=15)
            self.chan.sendall(b"\n")
        # eat first shell prompt
        self._read_until_prompt(timeout=15)
        # disable local echo to keep transcripts clean
        self.run("stty -echo", collect_marker=False)

    def _read_more(self, timeout: float) -> bool:
        end = time.time() + timeout
        while time.time() < end:
            if self.chan.recv_ready():
                self._buf += self.chan.recv(65536)
                return True
            time.sleep(0.03)
        return False

    def _read_until(self, needle: str, timeout: float = 20.0) -> str:
        pat = re.compile(re.escape(needle))
        end = time.time() + timeout
        while time.time() < end:
            if pat.search(self._buf.decode("utf-8", "replace")):
                break
            if not self._read_more(0.5):
                pass
        text = self._buf.decode("utf-8", "replace")
        m = pat.search(text)
        if not m:
            return text
        consumed = text[:m.end()]
        self._buf = text[m.end():].encode("utf-8", "replace")
        return consumed

    def _read_until_prompt(self, timeout: float = 20.0, prompt_regex: Optional[str] = None) -> str:
        pat = re.compile(prompt_regex) if prompt_regex else PROMPT_ANY
        end = time.time() + timeout
        while time.time() < end:
            text = self._buf.decode("utf-8", "replace")
            if pat.search(text):
                break
            if not self._read_more(0.5):
                pass
        text = self._buf.decode("utf-8", "replace")
        m = pat.search(text)
        if not m:
            return text
        consumed = text[:m.end()]
        self._buf = text[m.end():].encode("utf-8", "replace")
        return consumed

    def run(self, cmd: str, collect_marker: bool = True, prompt_regex: Optional[str] = None,
            timeout: float = 20.0) -> Tuple[str, int]:
        """Send a command, return (stdout, exit_code). Uses __CMD_DONE__ marker
        to reliably observe exit code (matches the JSch client behavior)."""
        line = cmd
        if collect_marker:
            line += "; echo __CMD_DONE__:$?"
        self.chan.sendall((line + "\n").encode("utf-8", "replace"))
        exit_code = 0
        if collect_marker:
            block = self._read_until("__CMD_DONE__:", timeout=timeout)
            # then the digit(s)
            tail = self._read_until("\n", timeout=timeout)
            m = re.search(r"__CMD_DONE__:(-?\d+)", tail)
            if m:
                exit_code = int(m.group(1))
            block = block + tail
        else:
            block = self._read_until_prompt(timeout=timeout, prompt_regex=prompt_regex)
        # consume the trailing prompt
        self._read_until_prompt(timeout=timeout, prompt_regex=prompt_regex)
        # strip everything after the marker (prompt bytes we don't want in stdout)
        m2 = re.split(r"__CMD_DONE__:-?\d+", block, maxsplit=1)
        stdout = m2[0]
        return stdout, exit_code

    def close(self):
        try:
            if self.chan:
                self.chan.close()
            if self.transport:
                self.transport.close()
        except Exception:
            pass


def _java_to_py_regex(pat: str) -> str:
    """Convert Java's (?<name>...) to Python's (?P<name>...). Preserve
    lookbehinds (?<= and (?<! as-is."""
    return re.sub(r"\(\?<([A-Za-z_][A-Za-z0-9_]*)>", r"(?P<\1>", pat)


def resolve_register(step: Dict[str, Any], stdout: str, bindings: Dict[str, str]) -> Dict[str, str]:
    """Apply each `register.regex` to stdout and bind named groups. Also apply
    unconditional `name/value` entries (with an optional `when:` gate)."""
    out: Dict[str, str] = {}
    for reg in (step.get("register") or []):
        if not isinstance(reg, dict):
            continue
        if "regex" in reg:
            pat = _java_to_py_regex(reg["regex"])
            try:
                m = re.search(pat, stdout, re.DOTALL | re.MULTILINE)
            except re.error:
                continue
            if not m:
                continue
            for k, v in m.groupdict().items():
                if v is not None:
                    out[k] = v
        elif "name" in reg:
            when_expr = reg.get("when")
            if when_expr and not eval_expr(when_expr, {**bindings, **out}):
                continue
            val = subst(str(reg.get("value", "")), {**bindings, **out})
            out[reg["name"]] = val
    return out


def flatten_phase_steps(phase_body: Dict[str, Any]) -> List[Dict[str, Any]]:
    flat: List[Dict[str, Any]] = []
    def walk(items):
        for it in items or []:
            if not isinstance(it, dict):
                continue
            if it.get("type") == "loop":
                # unroll once
                walk(it.get("steps") or [])
            else:
                flat.append(it)
    walk(phase_body.get("steps") or [])
    return flat


def run_workflow(args) -> int:
    with open(args.workflow, "r", encoding="utf-8", errors="replace") as fh:
        wf = yaml.safe_load(fh)
    bindings: Dict[str, str] = {}
    # seed from --var K=V
    for kv in args.var or []:
        if "=" in kv:
            k, v = kv.split("=", 1)
            bindings[k.strip()] = v
    # seed with a few sensible defaults so ${...} inside expressions doesn't blow up
    bindings.setdefault("ROLLBACK_ONLY", "false")
    bindings.setdefault("USER", "root")
    bindings.setdefault("XML_BACKUP", "false")
    bindings.setdefault("NETCONF_ROOT_OK", "true")
    bindings.setdefault("CHECKSUM_STATUS", "")
    bindings.setdefault("MGW01CHECKSUMSTATUS", "")
    bindings.setdefault("MGW02CHECKSUMSTATUS", "")
    bindings.setdefault("EXECUTED_TABLES", "")
    bindings.setdefault("ROLLBACK_EXECUTED_TABLES", "")
    bindings.setdefault("ROLLBACK_REQUIRED", "false")

    def log(*a):
        print("[runner]", *a, flush=True)

    # connect once per remote node
    nodes = wf.get("nodes") or {}
    shells: Dict[str, ShellClient] = {}
    for node_id, ndef in nodes.items():
        if (ndef.get("type") or "").lower() != "remote":
            continue
        ssh = ndef.get("ssh") or {}
        user = subst(str(ssh.get("user", "")), bindings)
        host = args.host
        port = args.port
        password = subst(str(ssh.get("auth", {}).get("password", "")), bindings)
        interactive = [
            subst(re.sub(r"\\([^\w])", r"\1", p.get("expect", "")), bindings)
            for p in (ndef.get("interactive_prompts") or [])
        ]
        profile = "sbc" if interactive else "repo"
        log(f"connecting {node_id} as {user}@{host}:{port} ({profile})")
        sc = ShellClient(host, port, user, password, profile, log)
        sc.connect(interactive)
        shells[node_id] = sc

    pass_count = fail_count = skip_count = 0

    for phase_key, phase_body in (wf.get("phases") or {}).items():
        if not isinstance(phase_body, dict):
            continue
        when = phase_body.get("when")
        if when and not eval_expr(when, bindings):
            log(f"--- SKIP phase {phase_key} (when={when!r})")
            continue
        log(f"=== PHASE {phase_key} ({phase_body.get('name')}) ===")
        for step in flatten_phase_steps(phase_body):
            desc = step.get("command_description", "")
            node = step.get("node", "")
            step_when = step.get("when")
            if step_when and not eval_expr(step_when, bindings):
                skip_count += 1
                log(f"  SKIP  {desc!r}  (when={step_when!r})")
                continue
            if node == "local":
                # simulate local success token if criteria uses one
                skip_count += 1
                log(f"  LOCAL {desc!r} (skipped — runner does not exec local commands)")
                continue
            sc = shells.get(node)
            if sc is None:
                skip_count += 1
                log(f"  MISS  node {node!r} not connected — skipping")
                continue
            if "send" not in step:
                skip_count += 1
                log(f"  SFTP  {desc!r} (skipped — SFTP subsystem not driven)")
                continue
            raw_send = str(step["send"])
            cmd = subst(raw_send, bindings)
            cmd = re.sub(r"\s+", " ", cmd.strip())
            log(f"  RUN   {desc!r}")
            log(f"        $ {cmd[:180]}{'...' if len(cmd) > 180 else ''}")
            try:
                stdout, exit_code = sc.run(cmd, collect_marker=True, timeout=25.0)
            except Exception as exc:
                log(f"        EXEC-ERROR: {exc}")
                fail_count += 1
                if step.get("on_failure", "stop") == "stop":
                    break
                continue
            new_binds = resolve_register(step, stdout, bindings)
            for k, v in new_binds.items():
                bindings[k] = v
            log(f"        stdout={stdout.strip()[:140]!r}  exit={exit_code}  register={new_binds}")
            # validate
            val = step.get("validation") or {}
            if not val.get("enabled"):
                pass_count += 1
                continue
            crit = ((val.get("success") or {}).get("criteria") or {})
            ok = False
            if "expr" in crit:
                ok = eval_expr(crit["expr"], bindings)
            elif "regex" in crit:
                ok = bool(re.search(crit["regex"], stdout))
            else:
                ok = exit_code == 0
            if ok:
                pass_count += 1
                # apply success vars
                for k, v in ((val.get("success") or {}).get("vars") or {}).items():
                    bindings[k] = subst(str(v), bindings)
                log(f"        PASS  {((val.get('success') or {}).get('message') or '')!r}")
            else:
                fail_count += 1
                if (val.get("warning") or {}):
                    for k, v in (val.get("warning", {}).get("vars") or {}).items():
                        bindings[k] = subst(str(v), bindings)
                    log(f"        WARN  {(val.get('warning', {}).get('message') or '')!r}")
                else:
                    log(f"        FAIL  {(val.get('failure', {}).get('message') or '')!r}")
                if step.get("on_failure", "stop") == "stop" and not (val.get("warning") or {}):
                    log("  stopping phase — on_failure: stop")
                    break

    log(f"=== SUMMARY: PASS={pass_count}  FAIL={fail_count}  SKIP={skip_count}")
    for sc in shells.values():
        sc.close()
    return 0 if fail_count == 0 else 1


def main(argv=None):
    ap = argparse.ArgumentParser(description="Local MOP runner (Python stand-in for Java JSch engine)")
    ap.add_argument("--workflow", required=True, help="workflow YAML path")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=2222)
    ap.add_argument("--var", action="append", default=[], help="K=V binding (repeatable)")
    args = ap.parse_args(argv)
    return run_workflow(args)


if __name__ == "__main__":
    sys.exit(main())
