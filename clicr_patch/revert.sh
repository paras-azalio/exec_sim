#!/usr/bin/env bash
# Restore the two engine files to their committed state.
set -eu
JAVA_DIR="${JAVA_DIR:-C:/Users/parmahaj/OneDrive - Nokia/Documents/Projects/Nokia/JAVA_NOKIA_CLICR_AUTOMATION}"
git -C "$JAVA_DIR" checkout -- \
  src/main/java/cliautomation/exec/LocalCommandExecutor.java \
  src/main/java/ilink/nei/clicr/any/MopExecutionUtil.java
echo "reverted LocalCommandExecutor.java and MopExecutionUtil.java"
