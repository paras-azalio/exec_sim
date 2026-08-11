#!/usr/bin/env bash
# SBC_33 LIC_LOADING_IN_SBC - PRECHECK, end to end against the local simulator.
#
#   ./run_sbc33_precheck.sh [node-group]        (default SBC-1)
#
# The workflow's `node: local` steps run on this box through a POSIX shell, and
# they interpolate paths unquoted - so every host path they touch has to be
# space free. That is the only reason for the /c/clicr-sim staging area; the
# operator's own folder keeps the deliverables.
set -u

SIM_DIR="C:/Users/parmahaj/OneDrive - Nokia/Documents/Projects/Nokia/clicr-simulator"
JAVA_DIR="C:/Users/parmahaj/OneDrive - Nokia/Documents/Projects/Nokia/JAVA_NOKIA_CLICR_AUTOMATION"
PRECHECK="C:/Users/parmahaj/OneDrive - Nokia/Documents/UT Artifacts/License/precheck"
RUN="$PRECHECK/run"
BASH_EXE="C:/Program Files/Git/bin/bash.exe"

NODE="${1:-SBC-1}"
CR="CR1"
REQ="40528"

# C:/ form, not /c/ - these paths are handed to Windows python (the compare
# script and the xmllint shim), which cannot resolve an MSYS /c/... path.
STAGE="C:/clicr-sim"
SHARED="$STAGE/shared_data"
PYDIR="$STAGE/pyscripts"
SHIMS="$STAGE/shims"

stop_sim() {
  powershell -NoProfile -Command \
    "Get-CimInstance Win32_Process -Filter \"Name like '%python%'\" | Where-Object { \$_.CommandLine -like '*clicr_sim.py*' } | ForEach-Object { Stop-Process -Id \$_.ProcessId -Force }" \
    >/dev/null 2>&1
}

# ---- staging -----------------------------------------------------------------
mkdir -p "$SHARED/$REQ/$NODE/XML/Precheck" "$SHARED/$REQ/PREREQUISITE" "$PYDIR" "$SHIMS" "$RUN"
cp "$JAVA_DIR/src/main/resources/script/python/"*.py "$PYDIR/"
cp "$SIM_DIR/shims/xmllint" "$SHIMS/xmllint" && chmod +x "$SHIMS/xmllint"
# LocalCommandExecutor runs local steps via `bash -lc`, and a *login* shell
# rebuilds PATH from the profile - an exported PATH here would be discarded.
# So the shim has to sit in a directory the login PATH already contains.
LOGIN_BIN="$HOME/bin"
mkdir -p "$LOGIN_BIN"
cp "$SIM_DIR/shims/xmllint" "$LOGIN_BIN/xmllint" && chmod +x "$LOGIN_BIN/xmllint"
"$BASH_EXE" -lc 'command -v xmllint >/dev/null' \
  || { echo "xmllint shim not visible to a login shell; aborting"; exit 1; }

# The compare script rewrites the CIQ in place, so work on a copy and keep the
# operator's original pristine.
#
# The copy lives under the space-free staging root as well: these two paths are
# embedded in a `node: local` command as quoted arguments, and Java's Windows
# argument encoding does not survive the round trip through `bash -lc` with
# spaces inside quotes - argparse ends up seeing the path split at the space.
CIQ_SRC="$PRECHECK/SBC_33_LICENSE_LOADING_IN_SBC.json"
CIQ="$STAGE/ciq/SBC_33_LICENSE_LOADING_IN_SBC.json"
mkdir -p "$STAGE/ciq" "$STAGE/licenses"
cp "$CIQ_SRC" "$CIQ"
cp "$PRECHECK/"*.txt "$STAGE/licenses/" 2>/dev/null

stop_sim
sleep 1

cd "$SIM_DIR" || exit 1
# RESPONSES=answers.txt | answers.yaml  -> use a response file instead of the
# scenario. Unset keeps the generated scenario.yaml.
if [ -n "${RESPONSES:-}" ]; then
  echo "answers       : $RESPONSES"
  python clicr_sim.py --port 22 --responses "$RUN/$RESPONSES" -v > "$RUN/simulator.log" 2>&1 &
else
  python clicr_sim.py --port 22 --scenario "$RUN/scenario.yaml" -v > "$RUN/simulator.log" 2>&1 &
fi

python - <<'PY' || { echo "simulator did not come up on port 22"; stop_sim; exit 1; }
import socket, sys, time
deadline = time.time() + 30
while time.time() < deadline:
    try:
        with socket.create_connection(("127.0.0.1", 22), timeout=1):
            time.sleep(0.3); sys.exit(0)
    except OSError:
        time.sleep(0.25)
sys.exit(1)
PY

# ---- run the real engine ------------------------------------------------------
cd "$JAVA_DIR" || exit 1
export MSYS2_ARG_CONV_EXCL="*"
export MSYS_NO_PATHCONV=1
export PATH="$SHIMS:$PATH"

java -Dclicr.local.shell="$BASH_EXE" \
     -Dclicr.local.pathmap="/mnt/shared_data=$SHARED;/data/cloud-user/om/install/sas/bin/macro_server/java/CLICR/ANY/V1/script/python=$PYDIR" \
     -cp "$SIM_DIR/clicr_patch/classes;target/classes;target/lib/*" ilink.nei.clicr.any.SimRunner \
     --template "$RUN/SBC_33_PRECHECK_sim.yaml" \
     --json    "$CIQ" \
     --schema  "src/main/resources/templates/jsonTemplate/SBC_33_LIC_LOADING_IN_SBC_json-output.yaml" \
     --node "$NODE" --cr "$CR" --node-type SBC --sub-activity 33_LIC_LOADING_IN_SBC \
     --out "$RUN" --sim-port 22 \
     --vars "CHILD_REQ_ID=$REQ;NODE_NAME=$NODE;LICENSE_FILE_PATH=$STAGE/licenses;INPUT_JSON_FILE_NAME=$CIQ;ORDER_NO=CR-SBC33-$REQ" \
     2>&1 | tee "$RUN/runner.log"
rc=${PIPESTATUS[0]}

stop_sim

# ---- collect deliverables into the operator's folder --------------------------
cp "$SHARED/$REQ/PREREQUISITE/"*.html "$PRECHECK/" 2>/dev/null && \
  echo "prerequisite report -> $PRECHECK/"
mkdir -p "$PRECHECK/XML/Precheck"
cp "$SHARED/$REQ/$NODE/XML/Precheck/"*.xml "$PRECHECK/XML/Precheck/" 2>/dev/null && \
  echo "precheck XMLs      -> $PRECHECK/XML/Precheck/"
# the compare script updates the CIQ with the values read off the node
cp "$CIQ" "$RUN/SBC_33_LICENSE_LOADING_IN_SBC.updated.json" 2>/dev/null && \
  echo "updated CIQ        -> $RUN/SBC_33_LICENSE_LOADING_IN_SBC.updated.json"

echo "node-group=$NODE exit=$rc"
exit "$rc"
