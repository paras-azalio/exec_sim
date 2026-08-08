"""DPA node personality for the CLI-CR simulator.

This module makes the simulator behave like the two BHPPNIMP01_DPA_0x nodes and
the repo server of the DPA / DLU_INSTALLATION MOP.

Design rule (important): the simulator NEVER reads the workflow YAML. Every
command is recognised here by its real command bytes, and the reply is produced
from *simulated node state* - the installed DLU version, what is staged in
/tmp, which backups exist under OldDLUBKP, whether the PHONES loader has run.
That way the workflow's own `register` regexes and `validation.success.criteria`
expressions are genuinely evaluated by the Java engine against a node that
behaves like the real one, instead of being handed a pre-baked verdict.

Failure injection is declared in the scenario file under `dpa.faults` and is
expressed in node terms ("the import script does not print Script finished"),
not in workflow terms.

Verbatim health-check output (df/free/ps/top/checkProcesses) comes from
dpa_fixtures.py, captured from a real East_execution.log run.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from dpa_fixtures import FIXTURES

# --------------------------------------------------------------------------- #
#  Defaults (overridable from the scenario's `dpa:` section)
# --------------------------------------------------------------------------- #

DEFAULTS = {
    "order_no": "CR-DPA_DLU05082026",
    "node1_name": "BHPPNIMP01_DPA_01",
    "node2_name": "BHPPNIMP01_DPA_02",
    "old_version": "DPA19.11SP1_DLU233-Interim",
    "old_tac": "271422",
    "new_version": "DPA19.11SP1_DLU234-Interim",
    "new_tac": "271900",
    "new_file": "phones_234.dat",
    "hang_sec": 180.0,          # used by the `timeout` fault mode
    # "old" = nodes still on the pre-change DLU (a fresh activity run).
    # "new" = the activity already completed, e.g. a standalone rollback run.
    "start_version": "old",
    # version reported by the `version: drift` fault (default: the new version)
    "drift_version": "",
}

# host name embedded in prompts / loader file names
HOSTS = {"node1": "bhppnimp01-dpa-01", "node2": "bhppnimp01-dpa-02"}

# node1 is SELinux-labelled in the real listings ("-rw-r--r--."), node2 is not.
DOT = {"node1": ".", "node2": ""}

REPO_BANNER = """Warning: Permanently added '{ip}' (ECDSA) to the list of known hosts.
WARNING! This computer system and network is PRIVATE and PROPRIETARY and
may only be accessed by authorized users. Unauthorized use of this computer
system or network is strictly prohibited and may be subject to criminal
prosecution, employee discipline up to and including discharge, or the
termination of vendor/service contracts. The owner, or its agents, may
monitor any activity or communication on the computer system or network.
The owner, or its agents, may retrieve any information stored within the
computer system or network. By accessing and using this computer system or
network, you are consenting to such monitoring and information retrieval
for law enforcement and other purposes. Users should have no expectation
of privacy as to any communication on or information stored within the
computer system or network, including information stored locally or
remotely on a hard drive or other media in use with this computer system
or network."""

IMPORT_HEAD = """DPA phone importer started.
Script started.
Dat importer is starting...
License reading:
  SLAlicense1911SP1Bharti.lic license file has been read
com.nokia.ntms.util.phoneimport.common.OMADMModel; local class incompatible: stream classdesc serialVersionUID = 3690836205357366959, local class serialVersionUID = 8414107616534827192
com.nokia.ntms.util.phoneimport.common.OMADMModel; local class incompatible: stream classdesc serialVersionUID = 3690836205357366959, local class serialVersionUID = 8414107616534827192
com.nokia.ntms.util.phoneimport.common.OMADMModel; local class incompatible: stream classdesc serialVersionUID = 3690836205357366959, local class serialVersionUID = 8414107616534827192"""

IMPORT_TAIL = """Device data was imported into DPA core successfully.
Dat importer is finished.

Csv importer is starting...
phones.csv does not exist.
No phones to add.
Csv importer is finished.
Script finished."""


# --------------------------------------------------------------------------- #
#  Fault declarations
# --------------------------------------------------------------------------- #


@dataclass
class Fault:
    """One injected node-level failure.

    at    - which command family misbehaves:
            import | version | startloader | phonescopy | scp | loader_log | tac
    node  - node1 | node2 | any
    mode  - family-specific, see _fault_for() call sites
    times - how many matching invocations are affected (default: all)
    """
    at: str
    node: str = "any"
    mode: str = ""
    times: int = 0
    _used: int = 0

    def applies(self, node_key: str) -> bool:
        if self.node not in ("any", node_key):
            return False
        if self.times and self._used >= self.times:
            return False
        return True

    def consume(self):
        self._used += 1


# --------------------------------------------------------------------------- #
#  Per-node simulated state
# --------------------------------------------------------------------------- #


@dataclass
class NodeState:
    key: str
    name: str
    installed_version: str              # what phoneimporter.log last reported
    phones_dat_version: str             # what currently sits in phoneimport/phones.dat
    tmp: Dict[str, str] = field(default_factory=dict)     # /tmp file -> version
    backups: List[tuple] = field(default_factory=list)    # (filename, version, tac)
    loader_ran: bool = False
    loader_tac: str = ""


class DpaWorld:
    """Shared, mutable state for both DPA nodes + the repo server."""

    def __init__(self, cfg: Optional[dict] = None, log=lambda *_: None):
        cfg = dict(DEFAULTS, **(cfg or {}))
        self.cfg = cfg
        self.log = log
        self.order_no = cfg["order_no"]
        self.old_version = cfg["old_version"]
        self.old_tac = str(cfg["old_tac"])
        self.new_version = cfg["new_version"]
        self.new_tac = str(cfg["new_tac"])
        self.new_file = cfg["new_file"]
        self.hang_sec = float(cfg["hang_sec"])
        self.names = {"node1": cfg["node1_name"], "node2": cfg["node2_name"]}

        self.faults: List[Fault] = [
            Fault(**f) for f in (cfg.get("faults") or [])
        ]

        start_new = str(cfg.get("start_version", "old")).lower() == "new"
        start_ver = self.new_version if start_new else self.old_version
        start_tac = self.new_tac if start_new else self.old_tac
        self.nodes = {
            k: NodeState(key=k, name=self.names[k],
                         installed_version=start_ver,
                         phones_dat_version=start_ver,
                         loader_ran=True, loader_tac=start_tac)
            for k in ("node1", "node2")
        }
        # files present on the repo server: name -> version
        self.repo: Dict[str, str] = {}

    # -- helpers ------------------------------------------------------------ #

    def _fault_for(self, at: str, node_key: str) -> Optional[Fault]:
        for f in self.faults:
            if f.at == at and f.applies(node_key):
                f.consume()
                self.log(f"[dpa] FAULT injected: at={at} node={node_key} mode={f.mode}")
                return f
        return None

    def tac_for(self, version: str) -> str:
        return self.new_tac if version == self.new_version else self.old_tac

    def version_of_file(self, filename: str) -> str:
        """Map a file name to the DLU version its contents represent."""
        if filename == self.new_file:
            return self.new_version
        m = re.search(r"phones\.dat_(DPA[\w.-]+?)__", filename)
        if m:
            return m.group(1)
        return self.old_version

    @staticmethod
    def today() -> str:
        return time.strftime("%Y-%m-%d")

    @staticmethod
    def now_stamp() -> str:
        return time.strftime("%b %e %H:%M").replace("  ", "  ")

    def backup_name(self, version: str, tac: str) -> str:
        return f"phones.dat_{version}__{tac}"

    # -- main dispatch ------------------------------------------------------ #

    def handle(self, node_key: str, cmd: str):
        """Return a CmdResult for `cmd` on `node_key`, or None if not ours."""
        from command_engine import CmdResult, checksum_hex, logical_key

        if node_key in ("node1", "node2"):
            return self._node_cmd(node_key, cmd, CmdResult, checksum_hex, logical_key)
        if node_key == "repo":
            return self._repo_cmd(cmd, CmdResult, checksum_hex, logical_key)
        return None

    # -- DPA node ----------------------------------------------------------- #

    def _node_cmd(self, nk, cmd, CmdResult, checksum_hex, logical_key):
        st = self.nodes[nk]
        dot = DOT[nk]

        # --- static health-check families (verbatim fixtures) --------------
        for sub, sig in (("df -kh", "df_kh"), ("df -i", "df_i"), ("free -g", "free_g"),
                         ("ps -ef | grep dpa", "ps_dpa"), ("top -b -n 1", "top"),
                         ("checkProcesses.sh", "checkprocs")):
            if sub in cmd:
                return CmdResult().out(FIXTURES[(nk, sig)] + "\n")
        if cmd.strip() == "uptime":
            return CmdResult().out(FIXTURES[(nk, "uptime")] + "\n")
        if cmd.strip() == "date":
            return CmdResult().line(time.strftime("%a %b %e %H:%M:%S IST %Y"))

        # --- BACKUP: latest SUCCESS loader entry (TAC count) ---------------
        if "loading status: SUCCESS" in cmd and "loader_*.log" in cmd:
            return CmdResult().line(
                f"{self.today()} 02:47:22 - INFO: sadm_CEM_DPA_PHONES_{HOSTS[nk]}"
                f"_{self.today()}_02-07-52.dat loading status: SUCCESS with "
                f"{st.loader_tac} rows processed, in 501 seconds")

        # --- installed DLU version (phoneimporter.log grep) ----------------
        if "Device Library file version:" in cmd and "phoneimporter.log" in cmd:
            f = self._fault_for("version", nk)
            if f and f.mode == "blank":
                return CmdResult()
            ver = st.installed_version
            if f and f.mode == "stale":
                # phoneimporter.log never picked up the new import
                ver = self.old_version
            elif f and f.mode == "drift":
                # log reports a version other than the one just imported
                ver = self.cfg.get("drift_version") or self.new_version
            return CmdResult().line(ver)

        # --- BACKUP: create OldDLUBKP copy ---------------------------------
        if "OldDLUBKP" in cmd and "mkdir -p" in cmd and "cp /export" in cmd:
            m = re.search(r"/(phones\.dat_DPA[\w.-]+?__(?:[0-9]+|NA))'?\s*$", cmd)
            if m:
                fname = m.group(1)
                st.backups.append((fname, self.version_of_file(fname),
                                   fname.rsplit("__", 1)[-1]))
            return CmdResult().line(
                f"-rw-r--r--{dot} 1 ntms ntms 47814240 {self.now_stamp()} "
                f"/export/home/users/ntms/tools/phoneimport/phones.dat")

        # --- ROLLBACK: list backups in OldDLUBKP ---------------------------
        if "OldDLUBKP" in cmd and "ls -lrth" in cmd:
            f = self._fault_for("backup_list", nk)
            if f and f.mode == "empty":
                return CmdResult().line("total 0")
            names = [b[0] for b in st.backups] or [
                self.backup_name(self.old_version, self.old_tac)]
            if len(names) == 1:
                names = [self.backup_name(self.old_version, "270771")] + names
            r = CmdResult().line("total 92M")
            for n in names:
                r.line(f"-rw-r--r--{dot} 1 ntms ntms 46M {self.now_stamp()} {n}")
            return r

        # --- ROLLBACK: restore backup over phones.dat ----------------------
        if "OldDLUBKP" in cmd and "cp -p" in cmd:
            f = self._fault_for("restore", nk)
            m = re.search(r"/(phones\.dat_DPA[\w.-]+?__(?:[0-9]+|NA))\s", cmd)
            if f and f.mode == "missing":
                bad = m.group(1) if m else "phones.dat"
                return CmdResult(exit_code=1).line(
                    "cp: cannot stat '/export/home/users/ntms/tools/phoneimport/OldDLUBKP/"
                    f"{self.order_no}/{st.name}/{bad}': No such file or directory")
            if m:
                st.phones_dat_version = self.version_of_file(m.group(1))
            return CmdResult()

        # --- ACTIVITY: install staged file as phones.dat -------------------
        if "cp -p /tmp/" in cmd and "phoneimport/phones.dat" in cmd and "chown" in cmd:
            f = self._fault_for("phonescopy", nk)
            m = re.search(r"cp -p /tmp/(\S+)", cmd)
            fname = m.group(1) if m else self.new_file
            if f and f.mode == "fail":
                return CmdResult(exit_code=1).line(
                    f"cp: cannot stat '/tmp/{fname}': No such file or directory")
            st.phones_dat_version = st.tmp.get(fname, self.version_of_file(fname))
            return CmdResult().line(
                f"-rw-r--r--{dot} 1 ntms ntms 48127352 {self.now_stamp()} "
                f"/export/home/users/ntms/tools/phoneimport/phones.dat")

        # --- import script -------------------------------------------------
        if "importtodpa.sh" in cmd:
            f = self._fault_for("import", nk)
            ver = st.phones_dat_version
            if f and f.mode == "wrong_version":
                ver = self.cfg.get("wrong_version", "DPA19.11SP1_DLU999-Bogus")
            r = CmdResult().out(IMPORT_HEAD + "\n")
            r.line(f"Device Library file version: {ver}")
            r.line("")
            if f and f.mode == "no_script_finished":
                r.out("Dat importer FAILED: device data could not be imported into DPA core.\n")
                r.exit_code = 1
                return r
            st.installed_version = ver
            r.out(IMPORT_TAIL + "\n")
            if f and f.mode == "no_prompt":
                r.suppress_prompt = True
            return r

        # --- PHONES loader -------------------------------------------------
        if "startLoader.sh PHONES" in cmd:
            f = self._fault_for("startloader", nk)
            if f and f.mode == "timeout":
                time.sleep(self.hang_sec)
                return CmdResult()
            if f and f.mode == "no_prompt":
                r = CmdResult()
                r.suppress_prompt = True
                return r
            if f and f.mode == "error":
                return CmdResult(exit_code=1).line(
                    "startLoader.sh: PHONES loader could not be started")
            st.loader_ran = True
            st.loader_tac = self.tac_for(st.installed_version)
            return CmdResult()

        # --- loader log presence -------------------------------------------
        if "logs/PHONES" in cmd and "ls -ltrh" in cmd:
            f = self._fault_for("loader_log", nk)
            if (f and f.mode == "missing") or not st.loader_ran:
                return CmdResult(exit_code=1)
            return CmdResult().line(
                f"-rw-rw-r-- 1 ntms ntms  98K {self.now_stamp()} loader_{self.today()}.log")

        # --- loader status / TAC count -------------------------------------
        if "loading status" in cmd and "tail -n 50" in cmd:
            f = self._fault_for("tac", nk)
            if f and f.mode == "missing":
                return CmdResult(exit_code=1)
            tac = "999999" if (f and f.mode == "mismatch") else st.loader_tac
            return CmdResult().line(
                f"{self.today()} 03:32:12 - INFO: sadm_CEM_DPA_PHONES_{HOSTS[nk]}"
                f"_{self.today()}_02-15-10.dat loading status: SUCCESS with "
                f"{tac} rows processed, in 2666 seconds")

        # --- checksums on the node ----------------------------------------
        if "sha256sum" in cmd:
            return self._sha(cmd, nk, CmdResult, checksum_hex, logical_key)

        # --- scp to/from the repo server -----------------------------------
        if cmd.startswith("scp ") or " scp -" in cmd:
            return self._scp(cmd, nk, CmdResult)

        return None

    # -- repo server -------------------------------------------------------- #

    def _repo_cmd(self, cmd, CmdResult, checksum_hex, logical_key):
        if cmd.startswith("mkdir -p"):
            return CmdResult()
        if "sha256sum" in cmd:
            return self._sha(cmd, "repo", CmdResult, checksum_hex, logical_key)
        if "ls -lrth" in cmd and "/repo-server/" in cmd:
            m = re.search(r"/DLU/[^/]+/(\S+)\s*$", cmd)
            node_name = m.group(1).strip() if m else ""
            nk = "node1" if node_name == self.names["node1"] else "node2"
            names = [b[0] for b in self.nodes[nk].backups] or [
                self.backup_name(self.old_version, self.old_tac)]
            r = CmdResult().line("total 92M")
            for n in names:
                r.line(f"-rw-r--r-- 1 installer installer 46M {self.now_stamp()} {n}")
            return r
        return None

    # -- shared helpers ----------------------------------------------------- #

    def _sha(self, cmd, nk, CmdResult, checksum_hex, logical_key):
        m = re.search(r"sha256sum\s+(\S+)", cmd)
        path = m.group(1) if m else ""
        base = re.split(r"[\\/]", path)[-1]

        # The new input file is really staged on the engine host, so its OM-side
        # checksum is a real sha256. Return that same value here, or the
        # OM <-> repo <-> node comparisons in the workflow could never match.
        if base == self.new_file and self.cfg.get("new_file_sha256"):
            return CmdResult().line(self.cfg["new_file_sha256"])

        # phones.dat carries no version in its name - hash it as whatever version
        # it currently holds, so a restored backup compares equal to its source.
        if base == "phones.dat" and nk in ("node1", "node2"):
            st = self.nodes[nk]
            base = self.backup_name(st.phones_dat_version,
                                    self.tac_for(st.phones_dat_version))
        return CmdResult().line(checksum_hex(logical_key(base)))

    def _scp(self, cmd, nk, CmdResult):
        st = self.nodes[nk]
        ip = "10.99.154.186"
        m = re.search(r"@\[?([\d.]+)\]?:", cmd)
        if m:
            ip = m.group(1)
        user = "installer"
        mu = re.search(r"(\b[\w.-]+)@\[?[\d.]+\]?:", cmd)
        if mu:
            user = mu.group(1)

        remote = re.search(r"@\[?[\d.]+\]?:(\S+)", cmd)
        remote_path = remote.group(1) if remote else ""
        parts = cmd.split()
        dest = parts[-1]
        pull = bool(remote) and "@" not in dest      # remote -> this node
        fname = re.split(r"[\\/]", remote_path if pull else parts[-2])[-1]

        f = self._fault_for("scp", nk)
        r = CmdResult()
        r.out(REPO_BANNER.format(ip=ip) + "\n")
        r.ask(f"{user}@{ip}'s password: ")
        if f and f.mode == "fail":
            r.out(f"scp: {remote_path or fname}: No such file or directory\n")
            r.exit_code = 1
            return r

        if pull:
            if dest.startswith("/tmp"):
                st.tmp[fname] = self.version_of_file(fname)
            else:                       # rollback restores straight onto phones.dat
                st.phones_dat_version = self.version_of_file(fname)
        else:
            self.repo[fname] = self.version_of_file(fname)

        pad = " " * max(1, 160 - len(fname))
        r.out(f"{fname}{pad}0%    0     0.0KB/s   --:-- ETA\n")
        r.out(f"{fname}{pad}93%   43MB  42.8MB/s   00:00 ETA\n")
        r.out(f"{fname}{pad}100%   46MB  42.0MB/s   00:01\n")
        return r
