# CLI-CR Node Simulator

A fake **SSH + SFTP** server that the **unmodified** Nokia CLI-CR engine (Java + JSch)
can run an entire MOP against. It impersonates the SBC/DPA OAM node and the
repository server, manufactures realistic Linux output per command, keeps
checksums consistent so every integrity check passes, and lets you **inject
failures** (version mismatch, checksum mismatch, apply failure → auto-rollback)
from a small scenario file — so you can exercise success / warning / failure /
rollback paths end-to-end without any real network elements.

> Built from a reverse-engineered contract of the JSch client (persistent shell
> channel + PTY, `__CMD_DONE__:$?` exit sentinel, per-node prompt matching,
> `expect_reply` password prompts, real SFTP subsystem). See `clicr_lint.py` for
> a static analyzer of the workflow YAML itself.

## Install

```
pip install -r requirements.txt        # paramiko, pyyaml
python --version                        # 3.8+
```

## Run

```
python clicr_sim.py --port 2222 --scenario scenarios/success.yaml -v
```

Then point **every node** in your workflow YAML at the simulator. The same port
works for all nodes — the simulator picks the right "personality" by **username**:

```yaml
nodes:
  niam_sbc_server:
    ssh: { host: "127.0.0.1", port: 2222, user: "a1tuj88u", auth: { password: "x" } }
  repo_server:
    ssh: { host: "127.0.0.1", port: 2222, user: "installer", auth: { password: "x" } }
  repo_server_sftp:
    type: sftp
    sftp: { host: "127.0.0.1", port: 2222, user: "installer", auth: { password: "x" } }
```

`${REPO_IP}`, `${REPO_USER}`, `${REPO_PASSWORD}` (and `niamID`, `ORDER_NO`, …)
are runtime values — supply them via your CIQ JSON / Arglist so the nodes resolve
to the simulator. If you cannot edit the YAML host literals, redirect them with an
`/etc/hosts` entry pointing the real hostnames at `127.0.0.1` and run on the
matching port.

## How it works (the parts that matter)

| Concern | What the simulator does |
|---|---|
| **Transport** | paramiko SSH server, RSA host key, accepts any password / keyboard-interactive. |
| **Login** | emits the MOTD ack + "Command line editor?" prompts, waits for the client's replies, then the shell prompt. |
| **Prompt** | re-emits the node prompt after **every** command (SBC `<host:user>/path:\n# `, repo `[installer@…]$ `). |
| **Exit codes** | when the client appends `; echo __CMD_DONE__:$?`, emits `__CMD_DONE__:<exit>` so the engine reads the code. Steps that carry a `prompt_regex` complete on the prompt (exit ignored) — both handled. |
| **sftp/scp in a shell** | emits a `password:` prompt (pauses for the reply) then a `100%` progress line. |
| **`type: sftp` nodes** | a real paramiko SFTP subsystem over a temp dir (`_fakefs/`) — `put`/`get` round-trip, `mkdir`/`create_dirs`/`overwrite` honored. |
| **Checksums** | `sha256sum` returns a hash derived from the file's **logical name** (host/dir/timestamp stripped), so node = repo = local for the same artifact → every comparison passes. |

## Scenario files (success / failure injection)

A scenario YAML is loaded at startup (`--scenario`). Schema:

```yaml
defaults: { shell_exit: 0 }

users:                       # username -> profile ("sbc" | "repo")
  a1tuj88u: sbc
  installer: repo

profiles:                    # optional prompt/banner overrides
  sbc:   { prompt: "<LabP1-oam-a:root>/root:\n# ", interactive: ["Enter return ...:", "Command line editor? (default = vi):"] }
  repo:  { prompt: "[installer@mano-repo-server ~]$ " }

checksum:
  force_mismatch: [Signaling]   # corrupt these logical keys on the NODE side

rules:                       # ordered; first match wins. match = regex on the real command
  - match: 'Device Library file version:'
    profile: any
    stdout: "DPA19.11SP1_DLU229\n"   # equals the new CIQ version -> version check fails
    exit: 0
  - match: 'netconfprov .*--onerror abort .*ServiceProfileTable'
    profile: sbc
    stdout: "ERROR: provisioning failed\n"   # no "provision is OK" -> ROLLBACK_REQUIRED
  - match: 'sha256sum .*Signaling'
    profile: sbc
    occurrences: [1]                 # only the 1st call...
    checksum_corrupt: true           # ...mismatches, retry then passes
```

Bundled examples (`scenarios/`):

| File | Demonstrates |
|---|---|
| `success.yaml` | clean end-to-end run (all defaults). |
| `dlu_version_fail.yaml` | installed DLU version == CIQ version → BACKUP version check fails. |
| `sbc_checksum_abort.yaml` | node checksum mismatch → retry loop → `checksum-guard` aborts. |
| `sbc_apply_fail_rollback.yaml` | `netconfprov` apply fails → `ROLLBACK_REQUIRED` → rollback phase runs. |

### Injection recipes (how each path is produced)

- **SUCCESS** — defaults already emit the matching success text (`provision is OK`, `SUCCESS`, `Alarm health check passed`, matching checksums).
- **WARNING** (step still passes) — command succeeds but emit text that fails the `success` criteria where a `warning:` branch exists (e.g. `root user is not allowed` → fallback to `netconfcli`).
- **`regex_miss` retry** — succeed but omit the success token (drop `100%` / wrong checksum).
- **`timeout` retry** — a rule that stalls (advanced; not needed for normal demos).
- **checksum loop** — `checksum.force_mismatch: [<key>]` (permanent) or an `occurrences`-scoped `checksum_corrupt` rule (transient).
- **AUTO-ROLLBACK** — fail a remote apply/version step so `ROLLBACK_REQUIRED=true`.

## Workflow linter

Static pre-flight check of a workflow YAML — finds where a run is likely to break
or what the YAML is missing, without running anything:

```
python clicr_lint.py templates/yaml/SBC_FIXED_LINE_CONFIGURATION.yaml
```

- **E1** loop `when`/`skip_when` references the loop's `item_var` (evaluated before the item is bound → loop skipped). Fix: move the filter into `continue_when`.
- **E2** a condition references a variable never set anywhere (typo, e.g. `CHECKSUM_STATUSs`).
- **W3** an `scp`/`sftp` send to a remote with no `expect_reply` for `password:`.

## Limitations / next steps

- **`local` / `loca` node steps run on the machine executing the Java engine**, not through this SSH server, so they use the real local environment. For SBC/DLU the failure/rollback *triggers* that matter are remote (version check, apply, checksums) and are fully controllable here. A `localbin/` shim (deterministic `sha256sum`/`python`/`scp` wrappers reusing `command_engine`) is the planned way to also fake local steps; ask if you want it.
- The in-shell `sftp` text and SFTP subsystem share the `_fakefs/` directory, so a shell `put` to a repo path is visible to a later subsystem `get`.
- `command_engine.py` is the place to add/adjust command families; `scenarios/*.yaml` is the place to script a specific run.

## Files

```
clicr_sim.py        SSH/SFTP server, login choreography, shell loop, exit-code sentinel
command_engine.py   command -> output, deterministic checksums, scenario rules
sftp_backend.py     paramiko SFTP subsystem over a temp-dir fake FS
clicr_lint.py       workflow YAML pre-flight analyzer
test_roundtrip.py   functional self-test (boots the server, drives it like JSch)
scenarios/          example success/failure scenario files
```
"# exec_sim" 
