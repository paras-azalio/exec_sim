#!/usr/bin/env bash
# Apply the two opt-in engine patches to a pristine CLI-CR checkout.
set -eu
cd "$(dirname "$0")"
JAVA_DIR="${JAVA_DIR:-C:/Users/parmahaj/OneDrive - Nokia/Documents/Projects/Nokia/JAVA_NOKIA_CLICR_AUTOMATION}"
for p in patches/*.patch; do
  echo "applying $p"
  git -C "$JAVA_DIR" apply --3way "$(pwd)/$p" \
    || echo "  already applied or conflicting - skipped"
done
