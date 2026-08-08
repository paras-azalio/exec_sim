"""Re-score an execution report counting only ACTIVITY / ROLLBACK failures.

The engine's own overall_status counts every phase. This prints (and stores) a
second verdict that ignores pre-health-check / backup / post-health-check
failures, so simulator-side noise in the health checks cannot mask or fake an
activity/rollback result.

    python score_lenient.py <run-dir>
"""
import json
import os
import sys

FATAL_PHASES = ("ACTIVITY_CONFIGURATION", "ROLLBACK_CONFIGURATION")


def main():
    run_dir = sys.argv[1]
    report = None
    for name in os.listdir(run_dir):
        if name.endswith(".json") and "EXECUTION_REPORT" in name:
            report = os.path.join(run_dir, name)
            break
    if report is None:
        print("  lenient verdict : no execution report found")
        return

    with open(report, encoding="utf-8") as fh:
        data = json.load(fh)

    per_phase = {}
    for cmd, row in (data.get("data") or {}).items():
        phase = row.get("phase") or "?"
        agg = per_phase.setdefault(phase, {"total": 0, "failed": 0, "fails": []})
        agg["total"] += 1
        if not row.get("success"):
            agg["failed"] += 1
            agg["fails"].append({
                "command": cmd[:160],
                "target": row.get("target"),
                "description": row.get("description"),
                "reason": (row.get("reason") or row.get("failure") or "")[:300],
            })

    fatal_failed = sum(per_phase.get(p, {}).get("failed", 0) for p in FATAL_PHASES)
    noncritical = {p: v["failed"] for p, v in per_phase.items()
                   if p not in FATAL_PHASES and v["failed"]}

    verdict = {
        "engine_overall_status": (data.get("metadata", {})
                                  .get("execution_summary", {})
                                  .get("overall_status")),
        "lenient_pass": fatal_failed == 0,
        "fatal_phase_failures": fatal_failed,
        "ignored_noncritical_failures": noncritical,
        "phases": {p: {"total": v["total"], "failed": v["failed"]}
                   for p, v in per_phase.items()},
        "failures": {p: v["fails"] for p, v in per_phase.items() if v["fails"]},
    }
    with open(os.path.join(run_dir, "LENIENT_VERDICT.json"), "w", encoding="utf-8") as fh:
        json.dump(verdict, fh, indent=2)

    print("  lenient verdict : %s (activity/rollback failures=%d)"
          % ("PASS" if verdict["lenient_pass"] else "FAIL", fatal_failed))
    if noncritical:
        print("  ignored         : " + ", ".join(f"{p}={n}" for p, n in noncritical.items()))
    for phase in FATAL_PHASES:
        for f in per_phase.get(phase, {}).get("fails", []):
            print(f"  FATAL {phase}: {f['description']} :: {f['reason'][:150]}")


if __name__ == "__main__":
    main()
