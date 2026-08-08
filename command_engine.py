"""
command_engine.py — the "brain" of the CLI-CR simulator.

Given a single shell command line (already interpolated by the Java engine, i.e.
all ${...} are concrete values), decide what bytes a real Linux host would print
and what exit code it would return.

Two layers:
  1. Scenario rules (user-supplied, ordered, optionally stateful) — first match wins.
  2. Built-in intelligent defaults per command family seen in the SBC + DLU MOPs.

The engine returns a CmdResult describing a sequence of "ops" the shell loop must
perform.  An op is one of:
    ("out",  text)          -> send text to the client
    ("ask",  prompt_text)   -> send prompt_text, then BLOCK until the client sends
                               one reply line (used for sftp/scp "password:" prompts)
plus an integer exit_code (used only when the engine appended the __CMD_DONE__ marker).

Everything here is pure/data-driven and unit-testable without a socket.
"""

from __future__ import annotations

import base64
import hashlib
import re
from dataclasses import dataclass, field
from typing import Callable, List, Optional, Tuple

# ----------------------------------------------------------------------------- #
#  Result type
# ----------------------------------------------------------------------------- #

Op = Tuple[str, str]  # ("out"|"ask", text)


@dataclass
class CmdResult:
    ops: List[Op] = field(default_factory=list)
    exit_code: int = 0
    # when True the shell does NOT re-emit the node prompt after this command,
    # which is how a real hung/mangled command produces the engine's
    # "prompt not ready before command on node: ..." failure.
    suppress_prompt: bool = False

    def out(self, text: str) -> "CmdResult":
        if text:
            self.ops.append(("out", text))
        return self

    def line(self, text: str = "") -> "CmdResult":
        self.ops.append(("out", text + "\n"))
        return self

    def ask(self, prompt: str) -> "CmdResult":
        self.ops.append(("ask", prompt))
        return self


# ----------------------------------------------------------------------------- #
#  Deterministic checksum logic
# ----------------------------------------------------------------------------- #

_TS = re.compile(r"_\d{4}-\d{2}-\d{2}[_T]\d{2}[-:]\d{2}[-:]\d{2}")


def logical_key(path: str) -> str:
    """Canonicalise a file path to a stable logical key so the same logical
    artifact yields the same checksum on node, repo and local.

    Examples that must collapse to one key:
        /storage/Signaling_SBC-1_2026-06-29_21-22-46.xml
        /repo-server/.../Signaling_Activity.xml
        /mnt/shared_data/.../Signaling_Rollback.xml      -> "Signaling" (rollback kept separate? see below)
        /storage/CRFTargetList_2026-06-29_..xml
        /repo-server/.../CRFTargetList_Activity.xml      -> "CRFTargetList"
    """
    base = re.split(r"[\\/]", path.strip())[-1]
    base = base.split("|")[0].strip()
    # strip a trailing .ext
    stem = re.sub(r"\.[A-Za-z0-9]+$", "", base)
    # strip an embedded timestamp (_2026-06-29_21-22-46)
    stem = _TS.sub("", stem)
    # NOTE: every checksum check in the MOP is an equality test, so collapsing
    # variants of the same base can only HELP (more matches). We deliberately
    # reduce all per-host aliases of one artifact to a single key:
    #   Signaling_SBC-1_<date>.xml  ==  Signaling_Activity.xml  ==  Signaling_Rollback.xml  -> "Signaling"
    #   <table>_<date>.xml  ==  <table>_Activity.xml  ==  <table>_Rollback.xml  ==  <table>_..._Postcheck.xml -> "<table>"
    # collapse SBC signaling/media families regardless of any suffix or node token
    # (node file is Signaling_SBC-1_<date>, repo is Signaling_Activity, etc.)
    m = re.match(r"^(Signaling|MGW01|MGW02)", stem)
    if m:
        return m.group(1)
    # generic: drop the Activity/Rollback/Postcheck role tokens (may be doubled)
    stem = re.sub(r"_(Activity|Rollback|ROLLBACK|Postcheck)\b", "", stem)
    return stem or base


def deterministic_bytes(key: str) -> bytes:
    """Fixed content for a logical key.  Real sha256 of these bytes equals
    checksum_hex(key), so a real local `sha256sum` of a file we delivered via
    SFTP get also matches the node/repo checksums."""
    return ("CLICR-SIM-ARTIFACT::" + key + "\n").encode("utf-8")


def checksum_hex(key: str, corrupt: bool = False) -> str:
    data = deterministic_bytes(key)
    if corrupt:
        data = data + b"::CORRUPT"
    return hashlib.sha256(data).hexdigest()


# ----------------------------------------------------------------------------- #
#  Scenario model
# ----------------------------------------------------------------------------- #


@dataclass
class ScenarioRule:
    pattern: re.Pattern
    profile: str = "any"          # "any" | "sbc" | "repo" | node-id
    stdout: str = ""              # verbatim text (LF normalised)
    exit: int = 0
    # stateful: only apply on these 1-based occurrences (empty = always)
    occurrences: Tuple[int, ...] = ()
    # checksum behaviour for sha256sum commands
    checksum_corrupt: bool = False
    # convenience: emit a 'password:' ask + a 100% line for transfer commands
    transfer_100: bool = False


class Scenario:
    """Loaded from a YAML/JSON file.  See scenarios/*.yaml for the schema."""

    def __init__(self, raw: Optional[dict] = None):
        raw = raw or {}
        self.checksum_force_mismatch = set(raw.get("checksum", {}).get("force_mismatch", []) or [])
        self.default_exit = int(raw.get("defaults", {}).get("shell_exit", 0))
        self.rules: List[ScenarioRule] = []
        for r in raw.get("rules", []) or []:
            self.rules.append(
                ScenarioRule(
                    pattern=re.compile(r["match"]),
                    profile=r.get("profile", "any"),
                    stdout=r.get("stdout", ""),
                    exit=int(r.get("exit", 0)),
                    occurrences=tuple(r.get("occurrences", []) or []),
                    checksum_corrupt=bool(r.get("checksum_corrupt", False)),
                    transfer_100=bool(r.get("transfer_100", False)),
                )
            )
        # per-pattern stateful counters
        self._counts: dict = {}

    def match(self, command: str, profile: str) -> Optional[ScenarioRule]:
        for rule in self.rules:
            if rule.profile not in ("any", profile):
                continue
            if not rule.pattern.search(command):
                continue
            key = id(rule)
            self._counts[key] = self._counts.get(key, 0) + 1
            if rule.occurrences and self._counts[key] not in rule.occurrences:
                continue
            return rule
        return None


# ----------------------------------------------------------------------------- #
#  The engine
# ----------------------------------------------------------------------------- #


class CommandEngine:
    def __init__(self, scenario: Optional[Scenario] = None, fake_fs=None, log=lambda *_: None,
                 workflow_state=None, dpa_world=None):
        self.scenario = scenario or Scenario()
        self.fs = fake_fs            # FakeRepoFS (shared with the SFTP subsystem) or None
        self.log = log
        # occurrence counters for built-in stateful behaviour (checksum retries)
        self._chk_counts: dict = {}
        self.workflow_state = workflow_state         # optional workflow.WorkflowState
        self.dpa_world = dpa_world                   # optional dpa_node.DpaWorld

    # -- public ------------------------------------------------------------- #

    def handle(self, command: str, profile: str) -> CmdResult:
        cmd = command.strip()
        # 0) workflow override (if a workflow is loaded, prefer YAML-derived output)
        if self.workflow_state is not None:
            hit = self.workflow_state.match(cmd, profile)
            if hit is not None:
                from workflow import synthesize_output   # local import to avoid cycle
                step, fail_flag = hit
                text = synthesize_output(step, fail=fail_flag)
                self.log(f"[engine] workflow-synth ({'FAIL' if fail_flag else 'OK'}): {text.strip()[:200]}")
                res = CmdResult(exit_code=1 if fail_flag else 0)
                res.out(text)
                return res
        # 1) scenario override
        rule = self.scenario.match(cmd, profile)
        if rule is not None:
            return self._apply_rule(rule, cmd, profile)
        # 1b) DPA node personality (stateful: installed DLU version, /tmp, backups)
        if self.dpa_world is not None and profile in ("node1", "node2", "repo"):
            res = self.dpa_world.handle(profile, cmd)
            if res is not None:
                self.log(f"[dpa:{profile}] handled {cmd[:80]!r} exit={res.exit_code}")
                return res
        # 2) built-in defaults
        for matcher, handler in self._dispatch:
            if matcher(cmd):
                try:
                    return handler(self, cmd, profile)
                except Exception as exc:  # never crash the shell loop
                    self.log(f"[engine] handler error for {cmd!r}: {exc}")
                    return CmdResult()
        # 3) basic interactive commands (date, ls, pwd, ...) for humans
        try:
            basic = self._basic_shell(cmd, profile)
            if basic.ops:
                return basic
        except Exception as exc:
            self.log(f"[engine] basic-shell error for {cmd!r}: {exc}")
        # 4) generic fallback: empty stdout, scenario's default exit (0 unless overridden)
        return CmdResult(exit_code=self.scenario.default_exit)

    # -- scenario rule application ------------------------------------------ #

    def _apply_rule(self, rule: ScenarioRule, cmd: str, profile: str) -> CmdResult:
        res = CmdResult(exit_code=rule.exit)
        if rule.transfer_100 or self._is_transfer(cmd):
            return self._transfer(cmd, profile, exit_code=rule.exit, extra=rule.stdout)
        if self._is_sha256(cmd):
            return self._sha256(cmd, profile, corrupt=rule.checksum_corrupt)
        if rule.stdout:
            res.out(self._norm(rule.stdout))
        return res

    # -- command family matchers/handlers ---------------------------------- #

    @staticmethod
    def _is_sha256(cmd: str) -> bool:
        return cmd.startswith("sha256sum ") or " sha256sum " in cmd and "awk" in cmd

    @staticmethod
    def _is_transfer(cmd: str) -> bool:
        return ("| sftp" in cmd or "|sftp" in cmd or cmd.startswith("sftp ")
                or " sftp -" in cmd or cmd.startswith("scp ") or " scp -" in cmd)

    # --- handlers ---------------------------------------------------------- #

    def _sha256(self, cmd: str, profile: str, corrupt: bool = False) -> CmdResult:
        # extract the path between 'sha256sum' and the pipe
        m = re.search(r"sha256sum\s+(.+?)(?:\s*\|\s*awk|\s*$)", cmd)
        path = m.group(1).strip() if m else cmd
        key = logical_key(path)
        # decide corruption: scenario force_mismatch set (node side only) or rule flag
        force = corrupt or (key in self.scenario.checksum_force_mismatch and profile == "sbc")
        # stateful: if a force-mismatch key, mismatch only on the FIRST occurrence so the
        # checksum-retry loop succeeds on retry (set occurrences via scenario for finer control)
        digest = checksum_hex(key, corrupt=force)
        self.log(f"[engine] sha256sum {path} -> key={key} corrupt={force}")
        return CmdResult().line(digest)

    def _date(self, cmd: str, profile: str) -> CmdResult:
        m = re.search(r"date\s+\+(\S+|\"[^\"]+\"|'[^']+')", cmd)
        fmt = m.group(1).strip("\"'") if m else "%Y-%m-%d_%H-%M-%S"
        # produce something matching the common formats without importing time-of-day
        # (value is only ever used as a filename token downstream)
        sample = "2026-06-30_12-00-00"
        if "%Z" in fmt or "%z" in fmt or ":" in fmt:
            sample = "2026-06-30 12:00:00 IST +0530"
        return CmdResult().line(sample)

    def _base64(self, cmd: str, profile: str) -> CmdResult:
        # `base64 [-w0] <file>` (encode) -> a single-line blob of the file's logical
        # content, deterministic per logical key (same bytes sha256sum hashes, so a
        # later checksum of the decoded file would still match).
        # (decode `base64 -d` runs on the local/OM host via a pipe, not through us.)
        if re.search(r"(?:^|\s)-d\b|--decode", cmd):
            return CmdResult()
        m = re.search(r"base64\s+(?:-\S+\s+)*(/\S+|\S+\.\w+)", cmd)
        path = m.group(1) if m else cmd
        path = re.split(r"[;\s|&>]", path)[0]        # drop a trailing "; echo" etc.
        blob = base64.b64encode(deterministic_bytes(logical_key(path))).decode("ascii")
        r = CmdResult().line(blob)
        if re.search(r";\s*echo\b", cmd):            # `... ; echo` prints a blank line
            r.line("")
        return r

    def _cat(self, cmd: str, profile: str) -> CmdResult:
        # `cat <file>` (read) -> deterministic fake XML for the file's logical key.
        # Wrapped in <config>...</config> so MOP register regexes like
        # `(?s)(?<SIGNALINGXML><config[\s\S]*?</config>)` capture it. (write forms
        # `cat > file` / heredocs run on the OM/local host, not through us.)
        if ">" in cmd or "<<" in cmd:
            return CmdResult()
        m = re.search(r"\bcat\s+(/\S+|\S+\.\w+)", cmd)
        if not m:
            return CmdResult()
        key = logical_key(re.split(r"[;\s|&<>]", m.group(1))[0])
        r = CmdResult()
        r.line('<?xml version="1.0" encoding="UTF-8"?>')
        r.line(f'<config xmlns="http://nokia.com/yang/isbc-sig" key="{key}">')
        r.line(f'  <clicr-sim>deterministic fake content for {key}</clicr-sim>')
        r.line('</config>')
        if re.search(r";\s*echo\b", cmd):
            r.line("")
        return r

    def _netconfprov(self, cmd: str, profile: str) -> CmdResult:
        # apply (has --onerror or a trailing .xml that is NOT a redirect)
        if "--readall" in cmd or "--read " in cmd or re.search(r"--read\b", cmd) or ">" in cmd:
            # backup/read -> redirected to a file: empty stdout, success
            return CmdResult()
        # apply: provision OK block
        table = "TABLE"
        mt = re.search(r"/([A-Za-z0-9_]+)_[\d-]+\.xml", cmd) or re.search(r"([A-Za-z0-9_]+)\.xml", cmd)
        if mt:
            table = mt.group(1)
        r = CmdResult()
        r.line("The provisioning was started")
        r.line("CNFG db dump start")
        r.line("CNFG db dump finished")
        r.line("NetConf Server is locked")
        r.line(f"provision is OK : {table}")
        r.line("NetConf Server is unlocked")
        r.line("The provisioning was finished")
        return r

    def _gen_xml(self, cmd: str, profile: str) -> CmdResult:
        table = "TABLE"
        mt = re.search(r"--table\s+(\S+)", cmd)
        if mt:
            table = mt.group(1)
        out = re.search(r"--output\s+(\S+)", cmd)
        outpath = out.group(1) if out else f"/tmp/{table}.xml"
        r = CmdResult()
        if "--postcheck" in cmd:
            r.line(f"[1/3] Validating current XML for {table}")
            r.line("[2/3] Comparing against CIQ ...")
            r.line("[3/3] OK")
            r.line("POSTCHECK PASSED")
            return r
        r.line(f"Table filter applied: '{table}'")
        r.line(f"Table: {table}  [RecordBased]  (3 record(s))")
        r.line("Generating NETCONF XML ...")
        r.line(f"Output : {outpath}")
        r.line("SUCCESS")
        return r

    def _transfer(self, cmd: str, profile: str, exit_code: int = 0, extra: str = "") -> CmdResult:
        """Emulate an in-shell `sftp`/`scp` subprocess: optional banner, a
        'password:' prompt we WAIT on, then a 100% progress line."""
        r = CmdResult(exit_code=exit_code)
        op, src, dst, user = self._parse_transfer(cmd)
        # write uploaded artifact into the fake repo FS so a later SFTP get round-trips
        if self.fs is not None and op in ("put", "upload") and dst:
            remote = dst.split(":", 1)[1] if ":" in dst else dst
            try:
                self.fs.put_bytes(remote, deterministic_bytes(logical_key(remote)))
            except Exception as exc:
                self.log(f"[engine] fake-fs put failed for {dst}: {exc}")
        # banner (matches the real capture loosely)
        r.line("WARNING! This system is PRIVATE. Authorized users only.")
        r.line("")
        if user:
            r.ask(f"{user}@repo-server's password: ")
        else:
            r.ask("password: ")
        # post-auth transcript
        r.line("Connected.")
        local = src if op in ("put", "upload") else dst
        remote = dst if op in ("put", "upload") else src
        base = re.split(r"[\\/]", (local or remote or "file").rstrip("/."))[-1] or "file"
        if op in ("put", "upload"):
            r.line(f"sftp> put {src} {dst}")
            r.line(f"Uploading {src} to {dst}")
        else:
            r.line(f"sftp> get {src} {dst}")
            r.line(f"Fetching {src} to {dst}")
        r.line(f"{base:<40} 0%    0     0.0KB/s   --:-- ETA")
        r.line(f"{base:<40} 100%  540KB  29.8MB/s   00:00")
        r.line("sftp> bye")
        if extra:
            r.out(self._norm(extra))
        return r

    def _parse_transfer(self, cmd: str):
        """Return (op, src, dst, user). op in {put,get,upload,download}."""
        # printf "put X Y\nbye\n" | sftp ...
        m = re.search(r'printf\s+"(.*?)"\s*\|', cmd, re.DOTALL)
        if m:
            payload = m.group(1)
            for line in re.split(r"\\n|\n", payload):
                line = line.strip()
                mm = re.match(r"(put|get)\s+(\S+)\s+(\S+)", line)
                if mm:
                    op, a, b = mm.group(1), mm.group(2), mm.group(3)
                    user = self._user_from_sftp(cmd)
                    return (op, a, b, user)
        # scp [opts] SRC DST
        ms = re.search(r"\bscp\b(.*)$", cmd)
        if ms:
            toks = [t for t in ms.group(1).split() if t]
            # drop options and their args
            args = []
            skip = False
            for t in toks:
                if skip:
                    skip = False
                    continue
                if t == "-F" or t == "-o" or t == "-i" or t == "-P":
                    skip = True
                    continue
                if t.startswith("-"):
                    continue
                args.append(t)
            if len(args) >= 2:
                src, dst = args[-2], args[-1]
                if ":" in dst:        # upload to remote
                    user = dst.split("@")[0] if "@" in dst else ""
                    return ("upload", src, dst, user)
                if ":" in src:        # download from remote
                    user = src.split("@")[0] if "@" in src else ""
                    return ("download", src, dst, user)
        return ("put", "", "", "")

    @staticmethod
    def _user_from_sftp(cmd: str) -> str:
        m = re.search(r"sftp[^|]*?(\b[\w.-]+)@", cmd)
        return m.group(1) if m else ""

    # --- DLU / health command families ------------------------------------ #

    def _dlu_version(self, cmd: str, profile: str) -> CmdResult:
        # success: return the OLD installed version (different from the new CIQ version)
        # scenario can override to force VersionFail.
        return CmdResult().line("DPA19.11SP1_DLU228")

    def _importtodpa(self, cmd: str, profile: str) -> CmdResult:
        r = CmdResult()
        r.line("Starting DPA phone import...")
        r.line("Device Library file version: DPA19.11SP1_DLU229")
        r.line("Validating file structure... OK")
        r.line("Importing 266306 records into DPA...")
        r.line("Import progress: 100%")
        r.line("Import completed successfully.")
        r.line("Script finished")
        return r

    def _loader_grep(self, cmd: str, profile: str) -> CmdResult:
        return CmdResult().line(
            "2026-03-18 01:16:18 - INFO: sadm_CEM_DPA_PHONES_2026-03-18.dat "
            "loading status: SUCCESS with 266309 rows processed, in 1737 seconds"
        )

    def _alarm_health(self, cmd: str, profile: str) -> CmdResult:
        return CmdResult().line("=== Alarm subsystem ===").line("Alarm health check passed")

    def _alarm_list(self, cmd: str, profile: str) -> CmdResult:
        return CmdResult().line("INFO: No active alarms found")

    def _lcp_status(self, cmd: str, profile: str) -> CmdResult:
        r = CmdResult()
        r.line("Host Info:")
        r.line("  host-1            Status: ENABLED")
        r.line("  host-2            Status: ENABLED")
        r.line("Service Members:")
        r.line("  member-1          HOTSTANDBY")
        r.line("  member-2          ENABLEDUNLOCKED")
        return r

    def _rem_srv(self, cmd: str, profile: str) -> CmdResult:
        r = CmdResult()
        r.line("Card  Status            REMc")
        r.line("  0   InserviceActive   REMc is Active")
        r.line("  1   InserviceStbyHot  REMc is Stby")
        return r

    def _sbc_health(self, cmd: str, profile: str) -> CmdResult:
        r = CmdResult()
        r.line("Running chk_all ...")
        r.line("chk_all completed successfully")
        r.line("NAME = media01 | 0| 0| 0| 0| 0| 0|")
        r.line("NAME = media02 | 0| 0| 0| 0| 0| 0|")
        return r

    def _df_kh(self, cmd, profile):
        r = CmdResult()
        r.line("Filesystem      Size  Used Avail Use% Mounted on")
        r.line("/dev/vda1       120G   21G  100G  18% /")
        r.line("/dev/mapper/x   200G   39G  162G  20% /export")
        return r

    def _df_i(self, cmd, profile):
        r = CmdResult()
        r.line("Filesystem        Inodes IUsed     IFree IUse% Mounted on")
        r.line("/dev/vda1       62913984 61058  62852926    1% /")
        return r

    def _free(self, cmd, profile):
        r = CmdResult()
        r.line("              total        used        free      shared  buff/cache   available")
        r.line("Mem:             62          48          10           3           3          14")
        r.line("Swap:             0           0           0")
        return r

    def _checkprocs(self, cmd, profile):
        r = CmdResult()
        r.line("Running processes of ALService:")
        r.line("Database:             YES - pid:12345")
        r.line("Service:              YES - pid:12346")
        r.line("Running processes of DPA DWH:")
        r.line("Health Checker:       YES - pid:12347")
        return r

    def _uptime(self, cmd, profile):
        return CmdResult().line("13:01:00 up 107 days, 21:38,  2 users,  load average: 0.46, 0.65, 0.71")

    def _ls(self, cmd, profile):
        m = re.search(r"ls\s+-\S+\s+(\S+)", cmd)
        path = m.group(1) if m else "/tmp/file"
        base = re.split(r"[\\/]", path)[-1]
        return CmdResult().line(f"-rw-r--r--. 1 om cloud-user 48234567 Jun 30 00:19 {path}")

    def _echo_guard(self, cmd, profile):
        return CmdResult().line("checksum-guard")

    def _and_echo_tail(self, cmd, profile):
        """Generic tail-of-pipeline handler for the MOP idiom
            `<some-cmd> && echo <TOKEN>`
        used to signal success to a `criteria.regex: '<TOKEN>'` validation.

        We treat the left side as if it succeeded (exit 0) and emit only the
        trailing token — which is exactly what a real bash pipeline prints when
        the left side is silent. This runs late in dispatch, so specific
        handlers for the left side (netconfprov, base64, sha256sum, …) still
        win when they apply.
        """
        m = re.search(r"&&\s*echo\s+(?P<tok>[A-Za-z0-9_\-]+)\s*$", cmd)
        if not m:
            return CmdResult()
        return CmdResult().line(m.group("tok"))

    def _decode_write(self, cmd, profile):
        """Handle the MOP's `echo '<b64>' | base64 -d > /path && echo <TOKEN>`
        idiom without actually decoding or writing. If the pipeline succeeds in
        real bash it prints only the trailing `echo <TOKEN>` — so we do the
        same. `<TOKEN>` is captured verbatim so the step's validation regex
        (e.g. `WRITE_OK`) fires."""
        m = re.search(
            r"\|\s*base64\s+(?:-d|--decode)\b[^&|]*?"
            r"&&\s*echo\s+(?P<tok>[A-Za-z0-9_\-]+)\s*$",
            cmd, re.DOTALL,
        )
        if not m:
            return CmdResult()
        # If the destination path is captured, also write the decoded bytes into
        # the fake repo FS so a subsequent `cat`/`sha256sum` of that file is
        # consistent. Best-effort: on any failure we still emit the token.
        try:
            path_m = re.search(r">\s*(\S+)", cmd)
            b64_m = re.search(r"echo\s+'([^']*)'\s*\|\s*base64", cmd, re.DOTALL)
            if self.fs is not None and path_m and b64_m:
                blob = re.sub(r"\s+", "", b64_m.group(1))
                self.fs.put_bytes(path_m.group(1),
                                  base64.b64decode(blob, validate=False))
        except Exception as exc:
            self.log(f"[engine] decode-write side effect skipped: {exc}")
        return CmdResult().line(m.group("tok"))

    # --- interactive-friendly basics (for humans SSH-ing in directly) ------ #

    def _basic_shell(self, cmd, profile):
        """Answer a small set of harmless commands so a person SSH-ing into the
        sim can type `date`, `pwd`, `whoami`, `ls`, `help`, etc. and get
        meaningful output. Returns an empty CmdResult (no output, exit 0) if
        the command is not one of the recognised basics — callers use that to
        distinguish 'handled' from 'no idea'."""
        import getpass
        import platform
        import socket as _sock
        import datetime
        import os as _os
        parts = cmd.strip().split()
        if not parts:
            return CmdResult()
        op = parts[0]
        args = parts[1:]

        if op == "date":
            fmt_present = any(a.startswith("+") for a in args)
            if fmt_present:
                return CmdResult()   # let the MOP `date +%Y-%m-%d…` handler answer
            now = datetime.datetime.now()
            return CmdResult().line(now.strftime("%a %b %d %H:%M:%S %Z %Y").strip())
        if op == "pwd":
            return CmdResult().line(_os.getcwd().replace("\\", "/"))
        if op == "whoami":
            return CmdResult().line(getpass.getuser())
        if op == "hostname":
            return CmdResult().line(_sock.gethostname())
        if op == "id":
            u = getpass.getuser()
            return CmdResult().line(f"uid=1000({u}) gid=1000({u}) groups=1000({u})")
        if op == "uname":
            sysname = platform.system()
            if "-a" in args:
                return CmdResult().line(
                    f"{sysname} {_sock.gethostname()} {platform.release()} "
                    f"{platform.version()} {platform.machine()} GNU/Linux"
                )
            return CmdResult().line(sysname)
        if op == "echo":
            return CmdResult().line(" ".join(args))
        if op == "clear":
            r = CmdResult()
            r.out("\x1b[H\x1b[2J")
            return r
        if op == "ls":
            path = "."
            for a in args:
                if not a.startswith("-"):
                    path = a
                    break
            try:
                items = sorted(_os.listdir(path))
            except Exception as exc:
                r = CmdResult(exit_code=2)
                r.line(f"ls: cannot access {path}: {exc}")
                return r
            if any(a in ("-l", "-la", "-al") for a in args):
                lines = []
                for it in items:
                    full = _os.path.join(path, it)
                    try:
                        st = _os.stat(full)
                        kind = "d" if _os.path.isdir(full) else "-"
                        lines.append(f"{kind}rw-r--r-- 1 mahajan mahajan {st.st_size:>10} {it}")
                    except Exception:
                        lines.append(f"?????????? ? ? ? ? {it}")
                return CmdResult().line("\n".join(lines))
            return CmdResult().line("  ".join(items))
        if op == "help":
            r = CmdResult()
            r.line("Interactive commands supported by this simulator:")
            r.line("  date, pwd, whoami, hostname, id, uname [-a], echo, ls, clear, help")
            r.line("  exit / logout / quit — disconnect")
            r.line("Anything else runs through the MOP-workflow engine (register regex + validation).")
            return r
        return CmdResult()          # signal "not handled here"

    # -- helpers ------------------------------------------------------------ #

    @staticmethod
    def _norm(text: str) -> str:
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        if text and not text.endswith("\n"):
            text += "\n"
        return text

    # -- dispatch table (ordered) ------------------------------------------- #
    # (built after methods are defined, see bottom of class via _build_dispatch)


def _contains(*subs):
    return lambda c: any(s in c for s in subs)


def _startswith(*subs):
    return lambda c: any(c.startswith(s) for s in subs)


def _regex(pat):
    p = re.compile(pat)
    return lambda c: bool(p.search(c))


# Ordered dispatch: (matcher, handler-method-name)
CommandEngine._dispatch = [
    (lambda c: CommandEngine._is_sha256(c), CommandEngine._sha256),
    (_regex(r"\bdate\s+\+"), CommandEngine._date),
    # `echo '<b64>' | base64 -d > /path && echo TOKEN` (used to materialise
    # SIGNALINGXML/etc. onto the target host) — must run before the generic
    # base64/echo handlers so the trailing token reaches the client.
    (_regex(r"\|\s*base64\s+(?:-d|--decode)\b.*&&\s*echo\s+\w+"), CommandEngine._decode_write),
    (_regex(r"\bbase64\b"), CommandEngine._base64),
    (_regex(r"\bcat\s+\S"), CommandEngine._cat),
    (_regex(r"\bnetconf_generate_(rollback_)?xml\.py\b"), CommandEngine._gen_xml),
    (_regex(r"\bnetconfprov\b"), CommandEngine._netconfprov),
    (lambda c: CommandEngine._is_transfer(c), CommandEngine._transfer),
    (_regex(r'Device Library file version:'), CommandEngine._dlu_version),
    (_regex(r"\bimporttodpa\.sh\b"), CommandEngine._importtodpa),
    (_regex(r'loading status: SUCCESS'), CommandEngine._loader_grep),
    (_regex(r"\balarm_check\b"), CommandEngine._alarm_health),
    (_regex(r"\balarm_cli\b"), CommandEngine._alarm_list),
    (_regex(r"\blcp_status\b"), CommandEngine._lcp_status),
    (_regex(r"\brem_srv_state\b"), CommandEngine._rem_srv),
    (_regex(r"\bsbc_health\b"), CommandEngine._sbc_health),
    (_regex(r"\bdf\s+-kh\b"), CommandEngine._df_kh),
    (_regex(r"\bdf\s+-i\b"), CommandEngine._df_i),
    (_regex(r"\bfree\s+-g\b"), CommandEngine._free),
    (_regex(r"checkProcesses\.sh"), CommandEngine._checkprocs),
    (_regex(r"\buptime\b|\btop\s+-b\b"), CommandEngine._uptime),
    (_regex(r"echo\s+checksum-guard"), CommandEngine._echo_guard),
    (_regex(r"\bls\s+-"), CommandEngine._ls),
    # Last resort for the `<unknown-cmd> && echo TOKEN` idiom — keep this LAST
    # so specific handlers above still win.
    (_regex(r"&&\s*echo\s+\w+\s*$"), CommandEngine._and_echo_tail),
]
