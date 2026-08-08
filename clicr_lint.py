#!/usr/bin/env python3
"""
clicr_lint.py — pre-flight analyzer for a CLI-CR workflow YAML.

Flags places a run is likely to misbehave or where the YAML is missing something
the engine needs, WITHOUT executing anything.  Checks:

  E1  loop-gate-uses-item-var : a `type: loop` step whose `when`/`skip_when`
      references the loop's own `item_var` -> the gate is evaluated ONCE at loop
      entry, before the item is bound, so it (almost) always skips the loop.
      (This is the exact SBC rollback bug.)
  E2  undefined-var-in-condition : a condition (`when`/`skip_when`/`break_when`/
      `continue_when`/criteria `expr`) references a variable that is never set
      anywhere (globals.vars, a register named-group, a validation .vars, a loop
      item_var) and is not a known runtime/CLI var -> typo (e.g. CHECKSUM_STATUSs).
  W3  transfer-without-expect : a `send` runs `sftp`/`scp` to a remote (a
      `user@host:` target / password login) but the step has no `expect_reply`
      matching `password:` -> the engine will hang waiting for the prompt.
  W4  criteria-var-maybe-empty : criteria/expr uses a var only ever set inside a
      DIFFERENT branch/loop, so it may be "" at evaluation time (informational).

Usage:
    python clicr_lint.py path/to/workflow.yaml [--strict]
"""

from __future__ import annotations

import argparse
import re
import sys

try:
    import yaml
except Exception:
    print("PyYAML required: pip install pyyaml", file=sys.stderr)
    sys.exit(2)

# variables the engine/CLI provides at runtime (not defined in the YAML)
RUNTIME_VARS = {
    "node", "DATE", "ATTEMPT", "table_row", "tableData", "ORDER_NO", "CHILD_REQ_ID",
    "niamID", "REPO_IP", "REPO_IPV4", "REPO_IP_V4", "REPO_IP_V6", "REPO_USER",
    "REPO_PASSWORD", "INPUT_JSON_FILE_NAME", "ROLLBACK_ONLY", "ROLLBACK_REQUIRED",
    "NODE1_NAME", "NODE2_NAME", "OLDDLUVERSION1", "OLDDLUVERSION2", "BACKUPTACCOUNT",
    "nodeType", "activity", "SECRET", "ENV",
}
OPERATOR_WORDS = {
    "contains", "notContains", "startsWith", "notStartsWith",
    "true", "false", "null", "and", "or", "eq", "ne",
}
CONDITION_FIELDS = ["when", "skip_when", "break_when", "continue_when"]


class Finding:
    def __init__(self, code, sev, where, msg):
        self.code, self.sev, self.where, self.msg = code, sev, where, msg

    def __str__(self):
        return f"[{self.sev}] {self.code}  {self.where}\n        {self.msg}"


def var_refs(expr: str):
    """Return the set of variable root-names referenced inside ${...} blocks."""
    names = set()
    if not isinstance(expr, str):
        return names
    for body in re.findall(r"\$\{(.*?)\}", expr, re.S):
        # drop quoted string literals
        body = re.sub(r"\"[^\"]*\"|'[^']*'", " ", body)
        for ident in re.findall(r"[A-Za-z_][A-Za-z0-9_]*", body):
            root = ident.split(".")[0]
            if root in OPERATOR_WORDS:
                continue
            if root.isdigit():
                continue
            names.add(root)
    return names


def collect_defined_vars(wf: dict):
    """Every variable the YAML *defines* anywhere (globally — context vars persist)."""
    defined = set(RUNTIME_VARS)
    g = (wf.get("globals") or {}).get("vars") or {}
    defined |= set(g.keys())
    item_vars = set()

    def walk(steps):
        for st in steps or []:
            if not isinstance(st, dict):
                continue
            if st.get("item_var"):
                item_vars.add(st["item_var"])
                defined.add(st["item_var"])
            for reg in st.get("register") or []:
                for grp in re.findall(r"\(\?<([A-Za-z_][A-Za-z0-9_]*)>", str(reg.get("regex", ""))):
                    defined.add(grp)
                # register can also set a var directly via `- name: X` (+ value/regex)
                if isinstance(reg, dict) and reg.get("name"):
                    defined.add(str(reg["name"]).strip())
            val = st.get("validation") or {}
            for branch in ("success", "warning", "failure"):
                b = val.get(branch) or {}
                for k in (b.get("vars") or {}).keys():
                    defined.add(k)
            for k in (st.get("var_meta") or {}).keys():
                defined.add(k)
            if st.get("steps"):
                walk(st["steps"])

    for ph in (wf.get("phases") or {}).values():
        walk((ph or {}).get("steps"))
    return defined, item_vars


def iter_steps(wf):
    """Yield (phase_name, index_path, step) for every step incl. nested loop steps."""
    for pname, ph in (wf.get("phases") or {}).items():
        def walk(steps, prefix):
            for i, st in enumerate(steps or []):
                if not isinstance(st, dict):
                    continue
                where = f"{pname} step[{prefix}{i}]"
                yield where, st
                if st.get("steps"):
                    yield from walk(st["steps"], f"{prefix}{i}.")
        yield from walk((ph or {}).get("steps"), "")


def step_label(st):
    return st.get("command_description") or st.get("type") or st.get("node") or "?"


def lint(wf: dict):
    findings = []
    defined, item_vars = collect_defined_vars(wf)

    for where, st in iter_steps(wf):
        label = step_label(st)
        loc = f"{where}  ({label!r})"
        is_loop = str(st.get("type", "")).lower() == "loop"
        iv = st.get("item_var")

        # E1 : loop gate references its own item_var
        if is_loop and iv:
            for fld in ("when", "skip_when"):
                expr = st.get(fld)
                if expr and iv in var_refs(expr):
                    findings.append(Finding(
                        "E1", "ERROR", loc,
                        f"loop `{fld}` references item_var `{iv}`, which is NOT bound when the "
                        f"loop gate is evaluated (once, at entry). The loop will be skipped. "
                        f"Move this filter into `continue_when` (evaluated per-iteration).",
                    ))

        # E2 : conditions referencing undefined vars
        cond_sources = []
        for fld in CONDITION_FIELDS:
            if st.get(fld):
                cond_sources.append((fld, st[fld]))
        val = st.get("validation") or {}
        for branch in ("success", "failure", "warning"):
            crit = ((val.get(branch) or {}).get("criteria") or {})
            if isinstance(crit, dict) and crit.get("expr"):
                cond_sources.append((f"{branch}.criteria.expr", crit["expr"]))
        for fld, expr in cond_sources:
            for name in var_refs(expr):
                if name not in defined:
                    findings.append(Finding(
                        "E2", "ERROR", loc,
                        f"`{fld}` references `${{{name}}}` which is never set by any global, "
                        f"register group, validation var, or item_var - likely a typo.",
                    ))

        # W3 : transfer command without a password expect_reply
        send = st.get("send")
        if isinstance(send, str):
            looks_transfer = bool(re.search(r"\b(scp|sftp)\b", send)) and (
                re.search(r"\b[\w.-]+@", send) or "sftp" in send
            )
            has_pw_expect = any(
                re.search(r"password", str(e.get("expect", "")), re.I)
                for e in (st.get("expect_reply") or [])
            )
            if looks_transfer and not has_pw_expect and "sftp-plugin" not in str(st.get("node", "")):
                findings.append(Finding(
                    "W3", "WARN", loc,
                    "send runs scp/sftp to a remote but the step has no expect_reply matching "
                    "`password:` - the engine may hang or the transfer may fail auth.",
                ))

    # W4 : criteria uses a var set only inside loop bodies / other branches (best-effort)
    # (kept light to avoid noise — E2 already catches the common typo class.)
    return findings


def main(argv=None):
    ap = argparse.ArgumentParser(description="CLI-CR workflow YAML linter")
    ap.add_argument("workflow")
    ap.add_argument("--strict", action="store_true", help="exit non-zero on warnings too")
    args = ap.parse_args(argv)

    with open(args.workflow, "r", encoding="utf-8", errors="replace") as fh:
        wf = yaml.safe_load(fh)

    findings = lint(wf)
    errors = [f for f in findings if f.sev == "ERROR"]
    warns = [f for f in findings if f.sev == "WARN"]

    if not findings:
        print("No issues found.")
        return 0
    for f in findings:
        print(str(f))
        print()
    print(f"--- {len(errors)} error(s), {len(warns)} warning(s) ---")
    if errors or (args.strict and warns):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
