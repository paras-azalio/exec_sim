"""
workflow.py — parse a Nokia CLI-CR workflow YAML, flatten its phases/steps into a
linear index, and provide runtime services to the simulator:

  * find_match(cmd, node_kind) -> next step whose `send:` template matches the
    concrete command bytes coming in over SSH. Substitutes ${VAR} placeholders
    with '.+?' (non-greedy) so matching survives Java's engine having already
    interpolated captured variables.
  * synthesize_output(step, fail=False) -> bytes the sim should emit so that
      - every named group in `register.regex` binds to something,
      - `validation.success.criteria.expr` evaluates true (or false when fail=True),
      - the step's `prompt_regex` is honoured by the caller.
  * progress log line ("[phase 3/5 ACTIVITY_CONFIGURATION] step 12/58 (global 87)").

Loops (`type: loop`) are flattened once — their sub-steps are appended inline
with a "loop_iter_group" tag so a run can match the same sub-step many times
(the Java engine iterates; we just accept repeated matches).

`--fail` selector: "PHASE_NAME:step_number" where step_number is 1-based and
counts every step in that phase's flattened list (including loop sub-steps
expanded once).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

try:
    import yaml
except Exception:                       # pragma: no cover
    yaml = None


# ----------------------------------------------------------------------------- #
#  Data model
# ----------------------------------------------------------------------------- #


@dataclass
class Step:
    phase_key: str
    phase_name: str
    phase_idx: int                      # 1-based phase number
    step_idx: int                       # 1-based within phase (including loop children)
    global_idx: int                     # 1-based across whole workflow
    node: str                           # niam_sbc_server / repo_server / local / ...
    node_kind: str                      # sbc | repo | local | sftp
    description: str
    send_template: str                  # raw `send:` string (may contain ${VAR})
    send_regex: "re.Pattern"            # compiled regex to match concrete command line
    prompt_regex: str                   # explicit step prompt_regex (or "")
    register: List[Dict[str, Any]] = field(default_factory=list)
    validation: Dict[str, Any] = field(default_factory=dict)
    on_failure: str = "stop"
    loop_group: Optional[str] = None    # non-empty means this step lives inside a loop
    raw: Dict[str, Any] = field(default_factory=dict)


# ----------------------------------------------------------------------------- #
#  Helpers
# ----------------------------------------------------------------------------- #


def _node_kind(node: str, nodes_def: Dict[str, Any]) -> str:
    if not node:
        return "sbc"
    if node == "local":
        return "local"
    ndef = (nodes_def or {}).get(node) or {}
    typ = str(ndef.get("type", "")).lower()
    if typ == "sftp":
        return "sftp"
    # sbc profile uses interactive login; repo does not. Fall back on the id.
    if node.startswith("repo"):
        return "repo"
    if ndef.get("interactive_prompts"):
        return "sbc"
    return "sbc"


_VAR_RE = re.compile(r"\$\{[^}]+\}")


def _template_to_regex(send_template: str) -> re.Pattern:
    """Turn a `send:` template into a regex that matches the concrete command
    line Java will actually send. Every ${VAR} becomes '.+?' (non-greedy).
    Whitespace/newlines collapse (YAML folded scalars vs. shell one-liners)."""
    s = send_template.strip()
    # Collapse whitespace runs (folded/lit scalars can carry \n) — the client
    # sends one line, so we compare against a single-line normalisation.
    s = re.sub(r"\s+", " ", s)
    # Split around ${...}; escape literal parts; join with '.+?'
    parts = _VAR_RE.split(s)
    escaped = [re.escape(p) for p in parts]
    pattern = ".+?".join(escaped)
    # Anchor: allow trailing "; echo __CMD_DONE__:$?" or a trailing marker to be
    # already stripped by the shell loop before we see it. Match anywhere.
    return re.compile(pattern)


def _flatten_steps(steps_yaml: List[Any], nodes_def: Dict[str, Any],
                   phase_key: str, phase_name: str, phase_idx: int,
                   start_step_idx: int = 1, start_global_idx: int = 1,
                   loop_tag: Optional[str] = None) -> Tuple[List[Step], int]:
    """Recursively flatten a `steps:` list (expanding `type: loop` once).
    Returns (flat_list, next_global_idx)."""
    out: List[Step] = []
    step_idx = start_step_idx
    global_idx = start_global_idx
    for entry in steps_yaml or []:
        if not isinstance(entry, dict):
            continue
        if entry.get("type") == "loop":
            child_tag = (loop_tag or "") + f":{entry.get('item_var', 'loop')}"
            children, global_idx = _flatten_steps(
                entry.get("steps") or [], nodes_def, phase_key, phase_name,
                phase_idx, step_idx, global_idx, loop_tag=child_tag,
            )
            step_idx += len(children)
            out.extend(children)
            continue
        # sftp-only step (no `send:`) — index it but skip regex matching later.
        if "sftp" in entry and "send" not in entry:
            send_tpl = ""
            send_re = re.compile(r"$never^")
        else:
            send_tpl = entry.get("send") or ""
            send_re = _template_to_regex(send_tpl) if send_tpl else re.compile(r"$never^")
        step = Step(
            phase_key=phase_key,
            phase_name=phase_name,
            phase_idx=phase_idx,
            step_idx=step_idx,
            global_idx=global_idx,
            node=entry.get("node", ""),
            node_kind=_node_kind(entry.get("node", ""), nodes_def),
            description=str(entry.get("command_description", "")).strip(),
            send_template=send_tpl,
            send_regex=send_re,
            prompt_regex=str(entry.get("prompt_regex", "")),
            register=list(entry.get("register") or []),
            validation=dict(entry.get("validation") or {}),
            on_failure=str(entry.get("on_failure", "stop")),
            loop_group=loop_tag,
            raw=entry,
        )
        out.append(step)
        step_idx += 1
        global_idx += 1
    return out, global_idx


# ----------------------------------------------------------------------------- #
#  Output synthesis
# ----------------------------------------------------------------------------- #


def _sample_for_named_group(regex_src: str, name: str) -> str:
    """Best-effort: return a string that matches the named group `name` inside
    the register regex `regex_src`. We hand-tune a few high-signal patterns
    that appear across the SBC MOPs; everything else falls back to a generic
    printable token.
    """
    # extract just the sub-pattern between (?<NAME> and its matching ) — best-effort
    m = re.search(r"\(\?<" + re.escape(name) + r">(.+?)\)", regex_src, re.DOTALL)
    inner = m.group(1) if m else ""

    # Hard-coded literals — these are the "success" tokens the MOP looks for
    lit_map = {
        "ALARMHEALTHSTATUS": "Alarm health check passed",
        "CHKALLSUCCESS":     "chk_all completed successfully",
        "XMLGENSTATUS":      "SUCCESS",
        "FILECOPY":          "100%",
        "MGW01FILECOPY":     "100%",
        "MGW02FILECOPY":     "100%",
        "PCSSTATUS":         "No outstanding PCS procedures.",
        "AUDITSTATUS":       "Health check passed, no audit errors were detected.",
        "COMMANDSTATUS":     "Command succeeded!",
        # A multiline sample the (?s)<config...</config> regex will capture.
        "SIGNALINGXML":      (
            '<config xmlns="http://nokia.com/yang/isbc-sig">'
            '<clicr-sim>deterministic fake content</clicr-sim>'
            '</config>'
        ),
    }
    if name in lit_map:
        return lit_map[name]

    # Common by-shape helpers
    if re.search(r"\[a-fA-F0-9\]\{64\}", inner):
        return "d41d8cd98f00b204e9800998ecf8427ee6cd0e2e0d3a1d5f6a7b8c9d0e1f2a3b"
    if re.search(r"\[a-fA-F0-9\]\+", inner):
        return "d41d8cd98f00b204e9800998ecf8427ee6cd0e2e0d3a1d5f6a7b8c9d0e1f2a3b"
    if re.search(r"\\d\{4\}-\\d\{2\}-\\d\{2\}_\\d\{2\}-\\d\{2\}-\\d\{2\}", inner):
        return "2026-07-10_12-00-00"
    if re.search(r"\\d\{4\}-\\d\{2\}-\\d\{2\}", inner):
        return "2026-07-10"

    # A negated (alarm) group like "root user is not allowed" — we should NOT
    # emit anything that matches, since presence of the group means failure.
    # (The engine calls us with fail=True to force this; on success path we
    # deliberately omit.)
    if name in {"ERRORMSG", "ROOTNOTALLOWED", "NETCONFABORTED", "BADVMSTATE",
                "INVALIDHOST", "INVALIDSERVICE", "INVALIDCARDSTATE",
                "INVALIDREMCSTATE"}:
        return ""

    # Otherwise: a plain single word token.
    return "OK"


def synthesize_output(step: Step, fail: bool = False) -> str:
    """Build a plausible stdout for `step` so Java's engine either passes or
    fails its validation.

    Strategy:
      * On success:  for every `register.regex` entry whose named group is a
        "positive" (success) token, embed a sample line that matches. For
        "negative" groups (their presence means failure) — emit NOTHING.
      * On fail:     invert. For negative groups, embed a matching line. For
        positive groups, omit them so `${X != ""}` is false.

    Also honours `validation.success.criteria.regex` (literal token like
    "WRITE_OK", "xml_valid", "provision is OK").
    """
    lines: List[str] = []

    # Handle the criteria.regex family (raw success tokens)
    crit = ((step.validation or {}).get("success") or {}).get("criteria") or {}
    crit_regex = crit.get("regex")
    if crit_regex and not fail:
        # Emit the literal token if the regex is a plain string
        token = crit_regex if not re.search(r"[\\\[\](){}|*+?]", crit_regex) else "OK"
        lines.append(token)

    # Emit lines per register regex
    for reg in step.register or []:
        if not isinstance(reg, dict):
            continue
        pat = reg.get("regex")
        if not pat:
            continue
        named = re.findall(r"\(\?<([A-Za-z_][A-Za-z0-9_]*)>", pat)
        for name in named:
            is_negative = name in {
                "ERRORMSG", "ROOTNOTALLOWED", "NETCONFABORTED", "BADVMSTATE",
                "INVALIDHOST", "INVALIDSERVICE", "INVALIDCARDSTATE",
                "INVALIDREMCSTATE",
            }
            if fail:
                # On failure path: emit tokens for NEGATIVE groups, skip positive ones
                if is_negative:
                    tok = _negative_sample(name)
                    if tok:
                        lines.append(tok)
            else:
                if is_negative:
                    continue                   # do NOT emit
                sample = _sample_for_named_group(pat, name)
                if sample:
                    lines.append(sample)

    # Ensure we produce at least a newline so the client's read doesn't hang
    if not lines:
        lines.append("")
    return "\n".join(lines) + "\n"


def _negative_sample(name: str) -> str:
    return {
        "ERRORMSG":         "cannot create [No such file or directory]",
        "ROOTNOTALLOWED":   "root user is not allowed",
        "NETCONFABORTED":   "provision is aborted: netconfprov failed",
        "BADVMSTATE":       "shut off",
        "INVALIDHOST":      "DISABLED",
        "INVALIDSERVICE":   "FAILED",
        "INVALIDCARDSTATE": "0 0 0 x y z InserviceBad",
        "INVALIDREMCSTATE": "REMc is Down",
    }.get(name, "FAILURE")


# ----------------------------------------------------------------------------- #
#  Failure selector
# ----------------------------------------------------------------------------- #


@dataclass
class FailSelector:
    """Parses --fail 'PHASE_NAME:step_idx' and --fail-global N. Matches steps
    by phase (case-insensitive by `name:` or `phase_key`) + 1-based index."""
    by_phase: List[Tuple[str, int]] = field(default_factory=list)
    globals_: List[int] = field(default_factory=list)

    @classmethod
    def parse(cls, spec_list: List[str], global_list: List[int]) -> "FailSelector":
        by = []
        for spec in spec_list or []:
            for part in spec.split(","):
                part = part.strip()
                if not part or ":" not in part:
                    continue
                p, idx = part.rsplit(":", 1)
                try:
                    by.append((p.strip().lower(), int(idx)))
                except ValueError:
                    continue
        return cls(by_phase=by, globals_=list(global_list or []))

    def hits(self, step: Step) -> bool:
        if step.global_idx in self.globals_:
            return True
        for pname, idx in self.by_phase:
            if step.step_idx != idx:
                continue
            if pname in (step.phase_key.lower(), step.phase_name.lower()):
                return True
        return False


# ----------------------------------------------------------------------------- #
#  Workflow index + runtime state
# ----------------------------------------------------------------------------- #


class Workflow:
    def __init__(self, path: str):
        if yaml is None:
            raise RuntimeError("PyYAML is required")
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            self.raw = yaml.safe_load(fh) or {}
        self.name = self.raw.get("name", "workflow")
        self.nodes = self.raw.get("nodes") or {}
        self.phases_raw = self.raw.get("phases") or {}
        self.steps: List[Step] = []
        self.phase_totals: Dict[str, int] = {}     # phase_key -> total step count
        self.total_phases = 0

        global_idx = 1
        phase_idx = 0
        for phase_key, phase_body in self.phases_raw.items():
            if not isinstance(phase_body, dict):
                continue
            phase_idx += 1
            phase_name = str(phase_body.get("name", phase_key))
            flat, global_idx = _flatten_steps(
                phase_body.get("steps") or [], self.nodes,
                phase_key, phase_name, phase_idx,
                start_step_idx=1, start_global_idx=global_idx,
            )
            self.steps.extend(flat)
            self.phase_totals[phase_key] = len(flat)
        self.total_phases = phase_idx

    def summary(self) -> str:
        parts = [f"workflow={self.name!r} phases={self.total_phases} steps={len(self.steps)}"]
        for k, n in self.phase_totals.items():
            parts.append(f"  - {k}: {n} steps")
        return "\n".join(parts)


class WorkflowState:
    """Advances a cursor as commands arrive. Shared across all SSH sessions of
    a single simulator run (a MOP touches both niam and repo users)."""

    def __init__(self, wf: Workflow, fail_selector: FailSelector, log=lambda *_: None):
        self.wf = wf
        self.fail = fail_selector
        self.log = log
        self.cursor = 0                    # last matched index (0 = none yet)
        self.match_counts: Dict[int, int] = {}   # global_idx -> #matches (loops)
        # look-ahead limit when scanning forward on a miss
        self.scan_ahead = 40

    def _log_step(self, step: Step, cmd: str, hit_fail: bool):
        marker = "  FAIL-INJECT" if hit_fail else ""
        p_total = self.wf.phase_totals.get(step.phase_key, 0)
        self.log(
            f"[wf] [phase {step.phase_idx}/{self.wf.total_phases} {step.phase_name}] "
            f"step {step.step_idx}/{p_total} (global {step.global_idx}/{len(self.wf.steps)}) "
            f"node={step.node} :: {step.description or '(no description)'}"
            f"{marker}"
        )
        self.log(f"[wf]   cmd: {cmd[:180]}{'...' if len(cmd) > 180 else ''}")

    def match(self, cmd: str, node_kind: str) -> Optional[Tuple[Step, bool]]:
        """Find the workflow step that this command corresponds to.
        Returns (step, fail_flag) or None if no match within look-ahead."""
        if not self.wf.steps:
            return None
        c = re.sub(r"\s+", " ", cmd.strip())
        # 1) try the current step (loop repeat) then advance forward
        start = max(self.cursor, 1)
        # Search current step first (if any) so a loop iteration matches without advancing.
        candidates = []
        if 1 <= self.cursor <= len(self.wf.steps):
            candidates.append(self.cursor)
        for i in range(start, min(len(self.wf.steps), start + self.scan_ahead) + 1):
            if i not in candidates:
                candidates.append(i)
        for i in candidates:
            step = self.wf.steps[i - 1]
            if step.node_kind not in (node_kind, "any"):
                # `local` steps never reach us via SSH; skip
                if step.node_kind == "local":
                    continue
            if not step.send_template:
                continue
            if step.send_regex.search(c):
                # advance cursor (never rewind)
                if i > self.cursor:
                    self.cursor = i
                self.match_counts[step.global_idx] = self.match_counts.get(step.global_idx, 0) + 1
                fail = self.fail.hits(step)
                self._log_step(step, cmd, fail)
                return step, fail
        # no match
        self.log(f"[wf] no-match at cursor={self.cursor} for cmd: {cmd[:120]}")
        return None
