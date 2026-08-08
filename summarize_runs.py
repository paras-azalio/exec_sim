"""Summarise a directory of DPA/DLU_INSTALLATION simulated runs.

    python summarize_runs.py "C:/.../version/sim run/out7"

Prints, per scenario: overall status, per-phase pass counts, whether the
ROLLBACK_CONFIGURATION phase actually executed, and every failed command -
calling out failures in PRE_NODE_HEALTH_CHECK / BACKUP separately, since a
failure there means the run never reached the behaviour it was meant to test.
"""

import json
import os
import sys

REPORT = "DPA_DLU_INSTALLATION_EXECUTION_REPORT_East.json"
VARS = "DPA_DLU_INSTALLATION_VARIABLES_East.json"
EARLY = ("PRE_NODE_HEALTH_CHECK", "BACKUP")

SHORT = {
    "PRE_NODE_HEALTH_CHECK": "PRE_HC",
    "BACKUP": "BACKUP",
    "ACTIVITY_CONFIGURATION": "ACTIVITY",
    "POST_NODE_HEALTH_CHECK": "POST_HC",
    "ROLLBACK_CONFIGURATION": "ROLLBACK",
}


def failed_steps(report):
    for key, v in report.get("data", {}).items():
        if not isinstance(v, dict):
            continue
        if not v.get("success") or v.get("validation_status") == "failed":
            yield key, v


def main(root):
    dirs = sorted(d for d in os.listdir(root)
                  if os.path.isdir(os.path.join(root, d)) and d[0].isdigit())
    early_hits = []

    for d in dirs:
        path = os.path.join(root, d, REPORT)
        print("\n" + "=" * 78)
        if not os.path.exists(path):
            print(f"{d}: NO EXECUTION REPORT (run did not finish)")
            rl = os.path.join(root, d, "runner.log")
            if os.path.exists(rl):
                tail = open(rl, encoding="utf-8", errors="replace").read().strip().splitlines()
                for line in tail[-6:]:
                    print("   |", line)
            continue

        rep = json.load(open(path, encoding="utf-8"))
        s = rep["metadata"]["execution_summary"]
        phases = s.get("phase_summary", {})
        print(f"{d}   ->   {s['overall_status']}")
        print(f"  commands: {s['total_commands']} total, "
              f"{s['successful_commands']} ok, {s['failed_commands']} failed")
        print("  phases  : " + "  ".join(
            f"{SHORT.get(k, k)} {v['success']}/{v['total']}" for k, v in phases.items()))
        print(f"  rollback phase ran: {'YES' if 'ROLLBACK_CONFIGURATION' in phases else 'no'}")

        vpath = os.path.join(root, d, VARS)
        if os.path.exists(vpath):
            v = json.load(open(vpath, encoding="utf-8"))
            keys = ["ROLLBACK_REQUIRED", "ROLLBACK_REQUIRED_NODE1", "ROLLBACK_REQUIRED_NODE2",
                    "VERSION_STATUS", "IMPORT_STATUS", "LOADER_START_STATUS",
                    "TAC_LOAD_STATUS", "VERSION_RESTORE_STATUS"]
            shown = {k: v[k] for k in keys if k in v and v[k] != ""}
            if shown:
                print("  vars    : " + ", ".join(f"{k}={val}" for k, val in shown.items()))

        fails = list(failed_steps(rep))
        if not fails:
            print("  no failed commands")
        for key, f in fails:
            phase = f.get("phase", "?")
            marker = "  *** EARLY-PHASE FAILURE ***" if phase in EARLY else ""
            print(f"\n  FAILED [{phase}] on {f.get('target')}{marker}")
            print(f"    command    : {key[:150]}")
            print(f"    description: {str(f.get('description'))[:120]}")
            out = str(f.get("output", "")).replace("\n", " | ")[:220]
            print(f"    output     : {out}")
            if f.get("reason"):
                print(f"    reason     : {str(f.get('reason'))[:220]}")
            print(f"    conclusion : {str(f.get('validation_conclusion'))[:200]}")
            if phase in EARLY:
                early_hits.append((d, phase, key, str(f.get("validation_conclusion"))[:120]))

    print("\n" + "=" * 78)
    print("EARLY-PHASE (PRE_NODE_HEALTH_CHECK / BACKUP) FAILURES")
    print("=" * 78)
    if not early_hits:
        print("None - every run got past health-check and backup cleanly.")
    else:
        for d, phase, cmd, concl in early_hits:
            print(f"  {d} [{phase}]")
            print(f"      {cmd[:140]}")
            print(f"      -> {concl}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else ".")
