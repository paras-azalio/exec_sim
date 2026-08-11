#!/usr/bin/env bash
# Compile the simulator-only driver against the CLI-CR engine, WITHOUT putting
# any of it inside the CLI-CR source tree.
#
# SimRunner has to stay in package ilink.nei.clicr.any because it calls
# MopExecutionUtil's package-private report helpers - but the .java and the
# .class both live here, and clicr_patch/classes simply goes first on the
# classpath at run time.
set -eu
cd "$(dirname "$0")"
JAVA_DIR="${JAVA_DIR:-C:/Users/parmahaj/OneDrive - Nokia/Documents/Projects/Nokia/JAVA_NOKIA_CLICR_AUTOMATION}"

mkdir -p classes
javac -nowarn -encoding UTF-8 \
      -cp "$JAVA_DIR/target/classes;$JAVA_DIR/target/lib/*" \
      -d classes \
      java/ilink/nei/clicr/any/SimRunner.java
echo "built -> $(pwd)/classes/ilink/nei/clicr/any/SimRunner.class"
