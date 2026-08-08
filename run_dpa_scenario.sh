#!/usr/bin/env bash
# Run one DPA/DLU_INSTALLATION scenario end-to-end against the node simulator.
#
#   ./run_dpa_scenario.sh <scenario-file> <out-subdir> [--rollback-only]
#
# Starts the simulator on port 22 (the workflow's repo_server pins port 22, and
# the DPA nodes reach the same port via M2MPORT), runs the real Java engine
# through SimRunner, then stops the simulator.
set -u

SIM_DIR="C:/Users/parmahaj/Documents/Projects/Nokia/clicr-simulator"
JAVA_DIR="C:/Users/parmahaj/Documents/Projects/Nokia/JAVA_NOKIA_CLICR_AUTOMATION"
# Override with OUT_ROOT=".../out2" to keep a previous run's results intact.
OUT_ROOT="${OUT_ROOT:-C:/Users/parmahaj/Documents/Projects/Nokia/version/sim run/out}"
# Override with TEMPLATE=".../DPA_DLU_INSTALLATION_lenient.yaml" to run a derived
# workflow (e.g. one where only activity/rollback failures are fatal).
TEMPLATE="${TEMPLATE:-src/main/resources/templates/yaml/DPA_DLU_INSTALLATION.yaml}"
CIQ="$OUT_ROOT/DPA_DLU_INSTALLATION_East.json"
BASH_EXE="C:/Program Files/Git/bin/bash.exe"
SHARED="C:/clicr-sim/shared_data"

SCENARIO="$1"
OUTDIR="$OUT_ROOT/$2"
ROLLBACK_ONLY="false"
[ "${3:-}" = "--rollback-only" ] && ROLLBACK_ONLY="true"

stop_sim() {
  powershell -NoProfile -Command \
    "Get-CimInstance Win32_Process -Filter \"Name like '%python%'\" | Where-Object { \$_.CommandLine -like '*clicr_sim.py*' } | ForEach-Object { Stop-Process -Id \$_.ProcessId -Force }" \
    >/dev/null 2>&1
}

mkdir -p "$OUTDIR"
stop_sim
sleep 1

cd "$SIM_DIR" || exit 1
python clicr_sim.py --port 22 --scenario "$SCENARIO" -v > "$OUTDIR/simulator.log" 2>&1 &

# Wait until the simulator actually accepts a TCP connection. A flat sleep is a
# race: the workflow's login_retries is 1, so a single refused connect marks the
# first health-check step FAILED and aborts the whole run before ACTIVITY.
python - <<'PY' || { echo "simulator did not come up on port 22"; stop_sim; exit 1; }
import socket, sys, time
deadline = time.time() + 30
while time.time() < deadline:
    try:
        with socket.create_connection(("127.0.0.1", 22), timeout=1):
            time.sleep(0.3)          # let the accept loop settle
            sys.exit(0)
    except OSError:
        time.sleep(0.25)
sys.exit(1)
PY

cd "$JAVA_DIR" || exit 1
# MSYS rewrites any argument that looks like a unix path ("/mnt/shared_data=...")
# into a Windows path before exec. Disable that for this call.
export MSYS2_ARG_CONV_EXCL="*"
export MSYS_NO_PATHCONV=1
java -Dclicr.local.shell="$BASH_EXE" \
     -Dclicr.local.pathmap="/mnt/shared_data=$SHARED" \
     -cp "target/classes;target/lib/*" ilink.nei.clicr.any.SimRunner \
     --template "$TEMPLATE" \
     --json    "$CIQ" \
     --schema  "src/main/resources/templates/jsonTemplate/DPA_DLU_INSTALLATION_json-output.yaml" \
     --node East --cr CR1 --out "$OUTDIR" --sim-port 22 \
     --parent-req-id 40501 --rollback-only "$ROLLBACK_ONLY" \
     2>&1 | tee "$OUTDIR/runner.log"
rc=${PIPESTATUS[0]}

stop_sim
echo "scenario=$2 exit=$rc"
exit "$rc"
