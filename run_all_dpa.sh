#!/usr/bin/env bash
# Run every DPA scenario end-to-end, one after another.
# Override the destination with:  OUT_ROOT=".../out6" bash run_all_dpa.sh
set -u
cd "$(dirname "$0")"
run() {
  echo ""
  echo "################ $2 ################"
  bash run_dpa_scenario.sh "scenarios/dpa/$1" "$2" ${3:-} 2>&1 \
    | grep -E "overall success|took|ROLLBACK_REQUIRED|scenario="
}
run 01_success.yaml                            01_success
run 02_autorb_standby.yaml                     02_autorb_standby
run 03_autorb_active.yaml                      03_autorb_active
run 04_autorb_startloader.yaml                 04_autorb_startloader
run 05_autorb_bothnodes.yaml                   05_autorb_bothnodes
run 06_rollback_only_pass.yaml                 06_rollback_only_pass                  --rollback-only
run 07_rollback_only_startloader_prompt.yaml   07_rollback_only_startloader_prompt    --rollback-only
run 08_rollback_only_version_not_restored.yaml 08_rollback_only_version_not_restored  --rollback-only
echo "ALL_SCENARIOS_DONE"
