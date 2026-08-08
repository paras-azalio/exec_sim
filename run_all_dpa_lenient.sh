#!/usr/bin/env bash
# Run all 8 DPA scenarios with a workflow in which ONLY activity/rollback
# failures are fatal: pre-health-check, backup and post-health-check phases get
# phase-level on_failure: continue, so a hiccup there no longer aborts the run.
#
#   ./run_all_dpa_lenient.sh <out-root-dir>
#
# The out-root must already contain DPA_DLU_INSTALLATION_East.json (the CIQ).
# The derived template is written into the out-root so each run is auditable.
set -u
cd "$(dirname "$0")"
OUT="${1:?usage: run_all_dpa_lenient.sh <out-root-dir>}"
SRC_TEMPLATE="C:/Users/parmahaj/Documents/Projects/Nokia/JAVA_NOKIA_CLICR_AUTOMATION/src/main/resources/templates/yaml/DPA_DLU_INSTALLATION.yaml"

export OUT_ROOT="$OUT"
export TEMPLATE="$OUT/DPA_DLU_INSTALLATION_lenient.yaml"

python make_lenient_template.py "$SRC_TEMPLATE" "$TEMPLATE" || exit 1
echo ""

run() {
  echo ""
  echo "################ $2 ################"
  bash run_dpa_scenario.sh "scenarios/dpa/$1" "$2" ${3:-} 2>&1 \
    | grep -E "overall success|^took|ROLLBACK_REQUIRED|scenario=|Exception"
  python score_lenient.py "$OUT/$2"
}

run 01_success.yaml                             01_success
run 02_autorb_standby.yaml                      02_autorb_standby
run 03_autorb_active.yaml                       03_autorb_active
run 04_autorb_startloader.yaml                  04_autorb_startloader
run 05_autorb_bothnodes.yaml                    05_autorb_bothnodes
run 06_rollback_only_pass.yaml                  06_rollback_only_pass                  --rollback-only
run 07_rollback_only_startloader_prompt.yaml     07_rollback_only_startloader_prompt     --rollback-only
run 08_rollback_only_version_not_restored.yaml   08_rollback_only_version_not_restored   --rollback-only
echo "ALL_SCENARIOS_DONE"
