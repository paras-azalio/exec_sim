# CLI-CR changes needed to drive the engine from the simulator

Everything the simulator needs from the CLI-CR side lives here, not in the
CLI-CR source tree.

## 1. `java/` — simulator-only, never in CLI-CR

`SimRunner.java` stands in for the ilink macro-server: it puts the simulator's
host/port into the same selector keys `MopExecutionUtil` fills from the GRC in
production, then calls the real `CliAutomationEngine.executeForNode`. The
workflow YAML is consumed unmodified, so every `when`, `register` and
`validation.criteria` is evaluated for real.

It stays in package `ilink.nei.clicr.any` because it calls `MopExecutionUtil`'s
package-private report builders — but the source and the class both live here.
`clicr_patch/classes` just goes first on the classpath.

```bash
./build.sh          # -> clicr_patch/classes/ilink/nei/clicr/any/SimRunner.class
```

## 2. `patches/` — two opt-in engine changes

These cannot live outside the engine, so they are kept as patches against a
pristine checkout. **Both are default-off**: with neither system property set,
behaviour is byte-for-byte what it was.

| patch | what it adds | why |
|---|---|---|
| `LocalCommandExecutor.patch` | `-Dclicr.local.shell=<bash>` and `-Dclicr.local.pathmap=a=b;c=d` | `node: local` steps are POSIX (`ls -lrt`, `base64 -d`, `xmllint`); on Windows they would otherwise run through PowerShell. The path map hosts `/mnt/shared_data` and the OM script dir somewhere real. |
| `MopExecutionUtil.patch` | four report helpers widened `private static` → package-private | lets SimRunner emit the identical execution-report JSON. No logic change. |

```bash
./apply.sh          # apply both to the CLI-CR checkout
./revert.sh         # git checkout -- both files (back to pristine)
```

## Known constraints when running local steps on Windows

1. `LocalCommandExecutor` runs local steps via `bash -lc` — a **login** shell,
   which rebuilds `PATH`. Exporting `PATH` before launching java does not reach
   it; a helper such as the `xmllint` shim must be installed into a directory
   the login profile already puts on `PATH` (e.g. `~/bin`).
2. Windows Python cannot resolve MSYS `/c/...` paths. Use `C:/...` in
   `-Dclicr.local.pathmap` targets.
3. **Quoted arguments containing spaces do not survive Java → `bash -lc`.**
   A local step interpolating `"C:/.../OneDrive - Nokia/..."` reaches the child
   process split at the space. Stage such inputs under a space-free directory.
   This is a real production hazard too, not only a simulator artefact.
