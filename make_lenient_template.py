"""Emit a copy of a CLI-CR workflow whose non-critical phases cannot fail the run.

Only ACTIVITY_CONFIGURATION and ROLLBACK_CONFIGURATION stay fatal. The
pre-health-check / backup / post-health-check blocks get phase-level
`on_failure: continue`, which is the flag ExecutionOrchestrator consults
(via YamlWorkflowLoader.normalizePhases -> PhaseDefinition.on_failure) when
deciding whether a phase failure aborts the whole execution.

Text-surgical on purpose: a YAML load/dump round-trip would reflow 2650 lines
and drop every comment, making the diff against the real template unreadable.

    python make_lenient_template.py <in.yaml> <out.yaml>
"""
import re
import sys

LENIENT_BLOCKS = ("preNodeHealthCheck", "backup", "postNodeHealthCheck")
FATAL_BLOCKS = ("activity_configuration", "rollback_configuration")


def patch(text):
    # The template is CRLF. Read/write preserves it (newline=""), so strip the
    # \r for matching and put it back on rejoin - otherwise every single line
    # shows up as changed in a diff against the original.
    eol = "\r\n" if "\r\n" in text else "\n"
    lines = [ln.rstrip("\r") for ln in text.split("\n")]
    # phase blocks are the 2-space-indented keys under a top-level `phases:`
    block_at = {}
    in_phases = False
    for i, line in enumerate(lines):
        if re.match(r"^phases:\s*$", line):
            in_phases = True
            continue
        if in_phases:
            if not line.strip() or line.lstrip().startswith("#"):
                continue                             # banner comments sit at col 0
            if re.match(r"^\S", line):
                break                                # left the phases: section
            m = re.match(r"^  ([A-Za-z_][\w]*):\s*$", line)
            if m:
                block_at[m.group(1)] = i

    missing = [b for b in LENIENT_BLOCKS if b not in block_at]
    if missing:
        raise SystemExit("phase block(s) not found: " + ", ".join(missing))

    changes = []
    # walk bottom-up so earlier line numbers stay valid while inserting
    for name in sorted(LENIENT_BLOCKS, key=lambda n: block_at[n], reverse=True):
        start = block_at[name]
        end = min([i for i in block_at.values() if i > start] + [len(lines)])
        for i in range(start + 1, end):
            m = re.match(r"^(    )on_failure:\s*(\S+)\s*$", lines[i])
            if m:                                     # existing key -> retarget
                if m.group(2).strip("'\"") != "continue":
                    lines[i] = "    on_failure: continue"
                    changes.append(f"{name}: on_failure {m.group(2)} -> continue (line {i+1})")
                break
            if re.match(r"^  \S", lines[i]):
                break
        else:
            i = None
        if i is None or not re.match(r"^    on_failure:", lines[i]):
            lines.insert(start + 1, "    on_failure: continue")
            changes.append(f"{name}: on_failure inserted (line {start+2})")
    return eol.join(lines), changes


def main():
    if len(sys.argv) != 3:
        raise SystemExit(__doc__)
    src, dst = sys.argv[1], sys.argv[2]
    with open(src, encoding="utf-8", newline="") as fh:
        text = fh.read()
    out, changes = patch(text)
    with open(dst, "w", encoding="utf-8", newline="") as fh:
        fh.write(out)

    # verify the result still parses and says what we intended
    try:
        import yaml
    except ImportError:
        yaml = None
    if yaml is not None:
        d = yaml.safe_load(out)
        for b in LENIENT_BLOCKS:
            got = d["phases"][b].get("on_failure")
            assert got == "continue", f"{b}: on_failure={got!r}"
        for b in FATAL_BLOCKS:
            got = d["phases"][b].get("on_failure")
            assert got != "continue", f"{b} must stay fatal, got {got!r}"
        print("verified: lenient=%s | fatal untouched=%s"
              % (",".join(LENIENT_BLOCKS), ",".join(FATAL_BLOCKS)))
    for c in changes:
        print("  " + c)
    print("wrote " + dst)


if __name__ == "__main__":
    main()
