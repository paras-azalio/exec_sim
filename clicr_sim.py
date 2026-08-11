#!/usr/bin/env python3
"""
clicr_sim.py — a fake SSH/SFTP terminal that the UNMODIFIED Nokia CLI-CR engine
(Java + JSch) can run a full MOP against.

It speaks the exact wire protocol the JSch client expects:
  * persistent shell channel with a PTY,
  * login choreography (MOTD ack + "Command line editor?" replies),
  * a node prompt re-emitted after every command,
  * the `__CMD_DONE__:<exit>` sentinel for steps that have no prompt_regex,
  * mid-command `password:` prompts + `100%` lines for in-shell sftp/scp,
  * a real SFTP subsystem (paramiko) for `type: sftp` nodes,
and manufactures realistic Linux output per command (see command_engine.py),
with deterministic checksums so every node/repo/local comparison passes — plus a
scenario file to force success / warning / failure / auto-rollback paths.

Run:
    python clicr_sim.py --port 2222 --scenario scenarios/sbc_success.yaml -v

Then point every node in the workflow YAML at this host:port (same port is fine —
the profile is chosen by username), e.g. set niam_sbc_server.ssh.host/port and
repo_server.ssh.host/port to 127.0.0.1 / 2222, and run the Java engine as usual.
"""

from __future__ import annotations

import argparse
import os
import re
import socket
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

import paramiko

from command_engine import CommandEngine, Scenario
from sftp_backend import FakeRepoFS, FakeSFTPServer
from workflow import Workflow, WorkflowState, FailSelector

try:
    import yaml
except Exception:  # pragma: no cover
    yaml = None


# ----------------------------------------------------------------------------- #
#  Profiles
# ----------------------------------------------------------------------------- #


@dataclass
class Profile:
    kind: str                       # "sbc" | "repo"  (also used as engine side selector)
    prompt: str
    interactive: List[str] = field(default_factory=list)  # expect-texts to emit at login
    banner: str = ""


SBC_PROMPT = "<mahajan-sbc-oam:root>/root:\n# "
REPO_PROMPT = "[mahajan@sim ~]$ "

DEFAULT_PROFILES: Dict[str, Profile] = {
    "sbc": Profile(
        kind="sbc",
        prompt=SBC_PROMPT,
        interactive=[
            "Enter return to acknowledge message of the day:",
            "Command line editor? (default = vi):",
        ],
        banner="",
    ),
    "repo": Profile(kind="repo", prompt=REPO_PROMPT, interactive=[], banner=""),
    # DPA/DLU nodes: reached over the NIAM gateway, told apart by ne_id.
    "dpa_node1": Profile(kind="node1", prompt="[NM_User0@bhppnimp01-dpa-01 ~]$ "),
    "dpa_node2": Profile(kind="node2", prompt="[NM_User0@bhppnimp01-dpa-02 ~]$ "),
}

# username -> profile name.  Repo-side usernames map to the repo profile; everything
# else defaults to the interactive SBC/DPA profile.
DEFAULT_REPO_USERS = {"installer", "admin", "repo", "repouser"}


def regex_to_literal(pat: str) -> str:
    """Best-effort: turn a simple expect/prompt regex into literal text the client
    will still match (strip backslash escapes before punctuation)."""
    s = pat
    s = re.sub(r"\(\?[a-z]+\)", "", s)          # drop inline flags (?s)(?m)
    s = s.strip("^$")
    s = re.sub(r"\\([^\w])", r"\1", s)           # \?  -> ?   \( -> (
    return s


# ----------------------------------------------------------------------------- #
#  SSH server interface
# ----------------------------------------------------------------------------- #


class SimServer(paramiko.ServerInterface):
    def __init__(self):
        self.event = threading.Event()
        self.username = None
        self.ne_id = None
        self.kbd_responses = None
        self.shell = False
        self.is_sftp = False
        self.exec_cmd = None
        self.want_reply_pty = False

    # auth: accept any credential, but do NOT accept "none" - a successful
    # none-auth would short-circuit the keyboard-interactive exchange that
    # carries ne_id, and both DPA nodes would collapse onto one personality.
    def get_allowed_auths(self, username):
        return "keyboard-interactive,password,publickey"

    def check_auth_none(self, username):
        self.username = username
        return paramiko.AUTH_FAILED

    def check_auth_password(self, username, password):
        self.username = username
        return paramiko.AUTH_SUCCESSFUL

    def check_auth_publickey(self, username, key):
        self.username = username
        return paramiko.AUTH_SUCCESSFUL

    def check_auth_interactive(self, username, submethods):
        """Ask for password + NE_ID.

        Both DPA nodes reach the simulator as the same user on the same port and
        differ only by the workflow's `ssh.ne_id`. JSch replies to any prompt
        containing "ne_id"/"neid"/"ne id" with that value (see
        RemoteCommandExecutor.SshUserInfo), so asking for it here is how we learn
        which NE this session is really for.
        """
        self.username = username
        return paramiko.InteractiveQuery(
            "", "", ("Password: ", False), ("NE_ID: ", True))

    def check_auth_interactive_response(self, responses):
        try:
            self.kbd_responses = [str(r) for r in (responses or [])]
            if len(responses) >= 2 and responses[1]:
                self.ne_id = str(responses[1]).strip()
        except Exception:
            pass
        return paramiko.AUTH_SUCCESSFUL

    # channels
    def check_channel_request(self, kind, chanid):
        if kind == "session":
            return paramiko.OPEN_SUCCEEDED
        return paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED

    def check_channel_pty_request(self, channel, term, width, height, pixelwidth, pixelheight, modes):
        return True

    def check_channel_shell_request(self, channel):
        self.shell = True
        self.event.set()
        return True

    def check_channel_exec_request(self, channel, command):
        self.exec_cmd = command.decode("utf-8", "replace") if isinstance(command, bytes) else command
        self.event.set()
        return True

    def check_channel_subsystem_request(self, channel, name):
        if name == "sftp":
            self.is_sftp = True
            self.event.set()
        return super().check_channel_subsystem_request(channel, name)

    def check_channel_window_change_request(self, *a, **k):
        return True


# ----------------------------------------------------------------------------- #
#  Shell session handling
# ----------------------------------------------------------------------------- #


class LineReader:
    """Reads from a paramiko Channel and yields one submitted line at a time.
    Two modes:
      * echo_on = False  (default; used by Java/JSch and paramiko clients):
        old batched behaviour. A line is terminated by EITHER \\n or \\r.
      * echo_on = True   (interactive OpenSSH / PuTTY users):
        byte-by-byte read; each printable byte is echoed back so the user sees
        what they type. Backspace erases. Enter finishes a line (server sends
        \\r\\n).
    """

    def __init__(self, chan: paramiko.Channel, echo_on: bool = False, send=None):
        self.chan = chan
        self.buf = b""
        self._skip_lf = False
        self.echo_on = echo_on
        self._send = send                 # callable(str) — used only in echo mode
        self._line = bytearray()

    # -- interactive echo mode ------------------------------------------------ #

    def _read_line_echo(self, idle_timeout: float) -> Optional[str]:
        self.chan.settimeout(idle_timeout)
        while True:
            try:
                data = self.chan.recv(64)
            except socket.timeout:
                return None
            except Exception:
                return None
            if not data:
                if self._line:
                    out = bytes(self._line).decode("utf-8", "replace")
                    self._line = bytearray()
                    return out
                return None
            for b in data:
                if b in (13, 10):                       # CR or LF -> finish line
                    if self._send:
                        self._send("\r\n")
                    line = bytes(self._line).decode("utf-8", "replace")
                    self._line = bytearray()
                    return line
                if b in (8, 127):                       # BS or DEL
                    if self._line:
                        self._line = self._line[:-1]
                        if self._send:
                            self._send("\b \b")
                    continue
                if b == 3:                              # Ctrl-C
                    if self._send:
                        self._send("^C\r\n")
                    self._line = bytearray()
                    return ""
                if b == 4:                              # Ctrl-D on empty line -> EOF
                    if not self._line:
                        if self._send:
                            self._send("logout\r\n")
                        return None
                    continue
                if 32 <= b < 127:                       # printable ASCII
                    self._line.append(b)
                    if self._send:
                        self._send(chr(b))
                    continue
                # non-ASCII / control byte: buffer it, don't echo
                self._line.append(b)

    # -- batched line mode (existing behaviour) ------------------------------ #

    def read_line(self, idle_timeout: float = 600.0) -> Optional[str]:
        if self.echo_on:
            return self._read_line_echo(idle_timeout)
        self.chan.settimeout(idle_timeout)
        while True:
            # if the previous line ended on \r, swallow a paired leading \n
            if self._skip_lf and self.buf[:1] == b"\n":
                self.buf = self.buf[1:]
            self._skip_lf = False
            # is there already a full line in the buffer?
            idx = _first_eol(self.buf)
            if idx is not None:
                term = self.buf[idx:idx + 1]
                line = self.buf[:idx]
                rest = self.buf[idx + 1:]
                if term == b"\r":
                    if rest[:1] == b"\n":
                        rest = rest[1:]          # paired \r\n already present
                    else:
                        self._skip_lf = True     # swallow a \n that may arrive next
                self.buf = rest
                return line.decode("utf-8", "replace")
            try:
                data = self.chan.recv(4096)
            except socket.timeout:
                return None
            except Exception:
                return None
            if not data:
                # EOF: flush any trailing partial as a final line
                if self.buf:
                    line, self.buf = self.buf, b""
                    return line.decode("utf-8", "replace")
                return None
            self.buf += data


def _first_eol(b: bytes):
    n = c = None
    i = b.find(b"\n")
    j = b.find(b"\r")
    if i == -1 and j == -1:
        return None
    if i == -1:
        return j
    if j == -1:
        return i
    return min(i, j)


_MARKER_TOKEN = "__CMD_DONE__:$?"
_MARKER_STRIP = re.compile(r"\s*;\s*echo\s+__CMD_DONE__:\$\?\s*$")


def _needs_continuation(buf: str) -> bool:
    """Return True if the accumulated line-buffer looks incomplete to a POSIX
    shell — i.e. inside an unclosed single/double quote, or ends with a
    line-continuation backslash. Used so the sim can accept multi-line commands
    such as MIME-wrapped `echo 'BASE64\\nBASE64\\n' | base64 -d ...`.
    Escape-inside-double-quotes is respected; escapes inside single quotes are
    literal (matching real bash)."""
    in_single = False
    in_double = False
    i = 0
    n = len(buf)
    while i < n:
        ch = buf[i]
        if in_single:
            if ch == "'":
                in_single = False
        elif in_double:
            if ch == "\\" and i + 1 < n:
                i += 2                              # skip escaped char
                continue
            if ch == '"':
                in_double = False
        else:
            if ch == "'":
                in_single = True
            elif ch == '"':
                in_double = True
            elif ch == "\\" and i + 1 == n:
                # trailing backslash at end of buffer -> line continuation
                return True
        i += 1
    return in_single or in_double


def handle_shell(chan: paramiko.Channel, profile: Profile, engine: CommandEngine, log,
                 interactive: bool = False):
    """`interactive=True` means a human (OpenSSH/PuTTY) is at the other end:
    we echo each keystroke, use CRLF line endings, and interpret exit/logout.
    `interactive=False` matches the automation client (JSch/paramiko) — silent,
    line-oriented, marker-based exit codes.
    """
    def send(text: str):
        if not text:
            return
        if interactive:
            data = text.replace("\r\n", "\n").replace("\n", "\r\n")
            chan.sendall(data.encode("utf-8", "replace"))
        else:
            chan.sendall(text.replace("\r\n", "\n").encode("utf-8", "replace"))

    reader = LineReader(chan, echo_on=interactive, send=send)

    try:
        if profile.banner:
            send(profile.banner)
        if interactive:
            send(f"Welcome, mahajan. Type 'help' for interactive commands, 'exit' to disconnect.\n")

        # login choreography (skip in interactive mode — a human at OpenSSH won't
        # know to reply to hard-coded expect prompts).
        if not interactive:
            for expect_text in profile.interactive:
                send(expect_text)
                reply = reader.read_line(idle_timeout=300)
                log(f"[{profile.kind}] login prompt {expect_text!r} -> reply {reply!r}")
                if reply is None:
                    return
        # prompt #1
        send(profile.prompt)

        while True:
            line = reader.read_line(idle_timeout=600)
            if line is None:
                log(f"[{profile.kind}] channel closed")
                return
            # Shell line-continuation: keep reading if the buffer has an open
            # single/double quote or ends with a bare backslash. This lets a
            # single logical command that spans multiple lines (e.g. a MIME-
            # wrapped `echo 'A\\nB\\nC' | base64 -d ...`) arrive as one command.
            raw = line
            while _needs_continuation(raw) and _MARKER_TOKEN not in raw:
                if interactive:
                    send("> ")
                more = reader.read_line(idle_timeout=600)
                if more is None:
                    break
                raw = raw + "\n" + more
                log(f"[{profile.kind}] cont: buffered {len(raw)} chars")
            has_marker = _MARKER_TOKEN in raw
            cmd = _MARKER_STRIP.sub("", raw).strip()
            log(f"[{profile.kind}] CMD {cmd!r} (marker={has_marker})")

            if cmd == "":
                if has_marker:
                    send("__CMD_DONE__:0\n")
                send(profile.prompt)
                continue

            # --- interactive built-ins ----------------------------------------
            if interactive and cmd in ("exit", "logout", "quit"):
                send("bye\n")
                return
            # stty -echo / stty echo lets clients toggle echoing mid-session
            if cmd.startswith("stty -echo"):
                reader.echo_on = False
                if has_marker:
                    send("__CMD_DONE__:0\n")
                send(profile.prompt)
                continue
            if cmd.startswith("stty echo") or cmd == "stty sane":
                reader.echo_on = True
                if has_marker:
                    send("__CMD_DONE__:0\n")
                send(profile.prompt)
                continue

            result = engine.handle(cmd, profile.kind)
            for op, text in result.ops:
                if op == "out":
                    send(text)
                elif op == "ask":
                    send(text)
                    pw = reader.read_line(idle_timeout=120)  # consume the password reply
                    log(f"[{profile.kind}]   (sub-prompt reply consumed: {pw!r})")
            if getattr(result, "suppress_prompt", False):
                log(f"[{profile.kind}]   (prompt suppressed - client will time out)")
                continue
            if has_marker:
                send(f"__CMD_DONE__:{result.exit_code}\n")
            send(profile.prompt)
    except Exception as exc:
        log(f"[{profile.kind}] shell error: {exc}")
    finally:
        try:
            chan.close()
        except Exception:
            pass


def handle_exec(chan: paramiko.Channel, cmd: str, profile: Profile, engine: CommandEngine, log):
    """Minimal exec-channel support (rarely used by the engine)."""
    log(f"[{profile.kind}] EXEC {cmd!r}")
    try:
        cleaned = _MARKER_STRIP.sub("", cmd).strip()
        result = engine.handle(cleaned, profile.kind)
        for op, text in result.ops:
            if op == "out":
                chan.sendall(text.encode("utf-8", "replace"))
            elif op == "ask":
                chan.sendall(text.encode("utf-8", "replace"))
        if _MARKER_TOKEN in cmd:
            chan.sendall(f"__CMD_DONE__:{result.exit_code}\n".encode())
        chan.send_exit_status(result.exit_code)
    except Exception as exc:
        log(f"[{profile.kind}] exec error: {exc}")
    finally:
        try:
            chan.close()
        except Exception:
            pass


# ----------------------------------------------------------------------------- #
#  Server bootstrap
# ----------------------------------------------------------------------------- #


def pick_profile(username: str, profiles: Dict[str, Profile], user_map: Dict[str, str],
                 ne_id: str = "", ne_map: Dict[str, str] = None) -> Profile:
    # ne_id wins: both DPA nodes share one username/host/port and differ only here.
    if ne_id and ne_map:
        name = ne_map.get(ne_id) or ne_map.get(ne_id.upper()) or ne_map.get(ne_id.lower())
        if name and name in profiles:
            return profiles[name]
    name = user_map.get((username or "").lower())
    if name is None:
        name = "repo" if (username or "").lower() in DEFAULT_REPO_USERS else "sbc"
    return profiles.get(name, profiles["sbc"])


def serve_connection(client_sock, addr, host_key, profiles, user_map, scenario, fs_root, log,
                     workflow_state=None, ne_map=None, dpa_world=None):
    log(f"[conn] from {addr}")
    try:
        t = paramiko.Transport(client_sock)
        t.local_version = "SSH-2.0-CLICRSim_1.0"
        t.add_server_key(host_key)
        # JSch 0.1.55 (2015) only speaks legacy algorithms; modern paramiko drops
        # most of them by default -> "Algorithm negotiation fail". Offer the overlap
        # (ssh-rsa host key, ecdh-sha2-nistp256 / dh-group14 kex, sha1/sha2 macs) first.
        try:
            so = t.get_security_options()

            def _prefer(supported, wanted):
                picked = tuple(a for a in wanted if a in supported)
                return (picked + tuple(a for a in supported if a not in picked)) if picked else supported

            so.kex = _prefer(so.kex, (
                "ecdh-sha2-nistp256", "ecdh-sha2-nistp384", "ecdh-sha2-nistp521",
                "diffie-hellman-group-exchange-sha256", "diffie-hellman-group14-sha256",
                "diffie-hellman-group14-sha1", "diffie-hellman-group1-sha1"))
            so.ciphers = _prefer(so.ciphers, (
                "aes128-ctr", "aes192-ctr", "aes256-ctr", "aes128-cbc", "aes256-cbc", "3des-cbc"))
            so.digests = _prefer(so.digests, ("hmac-sha2-256", "hmac-sha1"))
            # paramiko 5.x dropped ssh-rsa entirely; the only host-key algorithm both
            # it and JSch 0.1.55 support is ecdsa-sha2-nistp256 (we use an ECDSA host
            # key). Prefer it first.
            so.key_types = _prefer(so.key_types, ("ecdsa-sha2-nistp256", "rsa-sha2-256", "rsa-sha2-512"))
            log(f"[sec] offering kex[0]={so.kex[0]} key_types[0]={so.key_types[0]}")
        except Exception as e:
            log(f"[sec] could not set legacy security options: {e}")
        FakeSFTPServer.ROOT = os.path.abspath(fs_root)
        t.set_subsystem_handler("sftp", paramiko.SFTPServer, FakeSFTPServer)
        server = SimServer()
        try:
            t.start_server(server=server)
        except paramiko.SSHException as e:
            log(f"[conn] SSH negotiation failed: {e}")
            return

        chan = t.accept(30)
        # wait until we learn what was requested
        server.event.wait(10)
        username = server.username or ""
        ne_id = server.ne_id or ""
        profile = pick_profile(username, profiles, user_map, ne_id, ne_map)
        log(f"[conn] user={username!r} ne_id={ne_id!r} -> profile={profile.kind} "
            f"kbd_responses={getattr(server, 'kbd_responses', None)!r}")

        fs = FakeRepoFS(fs_root)
        engine = CommandEngine(scenario=scenario, fake_fs=fs, log=log,
                               workflow_state=workflow_state, dpa_world=dpa_world)

        if server.is_sftp:
            log(f"[conn] sftp subsystem (handled by paramiko)")
            while t.is_active():
                time.sleep(0.2)
            return

        if chan is None:
            # nothing usable
            while t.is_active():
                time.sleep(0.2)
            return

        if server.exec_cmd:
            handle_exec(chan, server.exec_cmd, profile, engine, log)
        else:
            # Interactive detection: OpenSSH / PuTTY clients want char-by-char
            # echo. Java (JSch) and paramiko clients don't.
            client_ver = ""
            try:
                client_ver = t.remote_version or ""
            except Exception:
                pass
            interactive = any(tag in client_ver for tag in
                              ("OpenSSH", "PuTTY", "Win32-OpenSSH", "libssh"))
            log(f"[conn] client_version={client_ver!r} interactive={interactive}")
            handle_shell(chan, profile, engine, log, interactive=interactive)
    except Exception as exc:
        log(f"[conn] error: {exc}")
    finally:
        try:
            t.close()
        except Exception:
            pass


def load_or_make_host_key(path: str, log):
    # ECDSA (nistp256): the only host-key type both paramiko 5.x and JSch 0.1.55 accept.
    if os.path.exists(path):
        try:
            return paramiko.ECDSAKey(filename=path)
        except Exception as e:
            log(f"[key] could not load {path} ({e}); regenerating")
    key = paramiko.ECDSAKey.generate()
    try:
        key.write_private_key_file(path)
        log(f"[key] generated ECDSA host key -> {path}")
    except Exception as e:
        log(f"[key] (in-memory key; could not persist: {e})")
    return key


def build_profiles(scenario_raw: dict, workflow_path: Optional[str], log) -> (Dict[str, Profile], Dict[str, str]):
    profiles = {k: Profile(v.kind, v.prompt, list(v.interactive), v.banner) for k, v in DEFAULT_PROFILES.items()}
    user_map: Dict[str, str] = {}

    # enrich interactive prompts / classification from the workflow YAML, if given
    if workflow_path and yaml and os.path.exists(workflow_path):
        try:
            with open(workflow_path, "r", encoding="utf-8", errors="replace") as fh:
                wf = yaml.safe_load(fh)
            for node_id, node in (wf.get("nodes") or {}).items():
                ssh = (node or {}).get("ssh") or {}
                user = str(ssh.get("user", "")).strip()
                ipr = node.get("interactive_prompts") or []
                prompt_re = node.get("prompt_regex", "")
                kind = "sbc" if ("<" in str(prompt_re) or ipr) else "repo"
                if user and not user.startswith("${"):
                    user_map[user.lower()] = kind
                if kind == "sbc" and ipr:
                    profiles["sbc"].interactive = [regex_to_literal(p.get("expect", "")) for p in ipr if p.get("expect")]
                    log(f"[profiles] sbc interactive prompts from {node_id}: {profiles['sbc'].interactive}")
        except Exception as e:
            log(f"[profiles] workflow parse warning: {e}")

    # scenario overrides win
    sp = (scenario_raw or {}).get("profiles") or {}
    for name, cfg in sp.items():
        base = profiles.get(name, Profile(kind=name, prompt=""))
        profiles[name] = Profile(
            kind=base.kind if base.kind else name,
            prompt=cfg.get("prompt", base.prompt),
            interactive=cfg.get("interactive", base.interactive),
            banner=cfg.get("banner", base.banner),
        )
    for u, n in ((scenario_raw or {}).get("users") or {}).items():
        user_map[u.lower()] = n
    return profiles, user_map


def main(argv=None):
    ap = argparse.ArgumentParser(description="CLI-CR fake SSH/SFTP node simulator")
    ap.add_argument("--host", default="0.0.0.0", help="bind address (default 0.0.0.0)")
    ap.add_argument("--port", type=int, default=2222, help="listen port (default 2222)")
    ap.add_argument("--scenario", help="scenario YAML/JSON (success/fail injection)")
    ap.add_argument("--responses",
                    help="response file: flat 'command => reply' .txt, or a single "
                         "request/response .yaml (see response_file.py)")
    ap.add_argument("--workflow", help="workflow YAML (auto-derive prompts + drive per-step output)")
    ap.add_argument("--fail", action="append", default=[],
                    help="Fail selector 'PHASE_NAME:step_idx' (repeatable, comma-list allowed)")
    ap.add_argument("--fail-global", action="append", default=[], type=int,
                    help="Fail by absolute step index (repeatable)")
    ap.add_argument("--fs-root", default=None, help="backing dir for the fake repo FS (default: ./_fakefs)")
    ap.add_argument("--host-key", default=None, help="RSA host key file (default: ./host_rsa.key)")
    ap.add_argument("--map", action="append", default=[], help="username=profile (repeatable)")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)

    here = os.path.dirname(os.path.abspath(__file__))
    fs_root = args.fs_root or os.path.join(here, "_fakefs")
    host_key_path = args.host_key or os.path.join(here, "host_ecdsa.key")

    def log(*a):
        if args.verbose:
            print(time.strftime("%H:%M:%S"), *a, flush=True)

    # scenario
    scenario_raw = {}
    if args.scenario:
        if not yaml:
            print("PyYAML required for --scenario", file=sys.stderr)
            return 2
        with open(args.scenario, "r", encoding="utf-8") as fh:
            scenario_raw = yaml.safe_load(fh) or {}

    # --responses: flat "command => reply" text, or a single request/response
    # YAML. Converted to the same rule dicts a scenario uses, and checked first.
    if args.responses:
        import response_file
        scenario_raw = response_file.merge(scenario_raw, response_file.load(args.responses))

    scenario = Scenario(scenario_raw)

    profiles, user_map = build_profiles(scenario_raw, args.workflow, log)
    for m in args.map:
        if "=" in m:
            u, n = m.split("=", 1)
            user_map[u.strip().lower()] = n.strip()

    host_key = load_or_make_host_key(host_key_path, log)
    os.makedirs(fs_root, exist_ok=True)

    # DPA/DLU node personality (enabled by a `dpa:` section in the scenario).
    # ne_id -> profile name, so the two DPA nodes are told apart on one port.
    dpa_world = None
    ne_map = {}
    if scenario_raw.get("dpa") is not None:
        from dpa_node import DpaWorld
        dpa_cfg = scenario_raw.get("dpa") or {}
        dpa_world = DpaWorld(dpa_cfg, log=log)
        ne_map = {
            dpa_world.names["node1"]: "dpa_node1",
            dpa_world.names["node2"]: "dpa_node2",
        }
        ne_map.update(scenario_raw.get("ne_ids") or {})

    # workflow-driven simulation (optional)
    workflow_state = None
    if args.workflow:
        wf = Workflow(args.workflow)
        print(wf.summary(), flush=True)
        fs_sel = FailSelector.parse(args.fail, args.fail_global)
        if fs_sel.by_phase or fs_sel.globals_:
            print(f"  fail-injections: phases={fs_sel.by_phase} globals={fs_sel.globals_}", flush=True)
        workflow_state = WorkflowState(wf, fs_sel, log=log)

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((args.host, args.port))
    sock.listen(100)
    print(f"CLI-CR simulator listening on {args.host}:{args.port}", flush=True)
    print(f"  fake repo FS : {fs_root}", flush=True)
    print(f"  scenario     : {args.scenario or '(built-in defaults)'}", flush=True)
    print(f"  user->profile: {user_map or '(default: a1tuj88u->sbc, installer/admin->repo)'}", flush=True)
    print("  Point your workflow nodes' ssh.host/port here (same port OK; profile by username).", flush=True)

    try:
        while True:
            client, addr = sock.accept()
            threading.Thread(
                target=serve_connection,
                args=(client, addr, host_key, profiles, user_map, scenario, fs_root, log),
                kwargs={"workflow_state": workflow_state, "ne_map": ne_map,
                        "dpa_world": dpa_world},
                daemon=True,
            ).start()
    except KeyboardInterrupt:
        print("\nshutting down")
        return 0


if __name__ == "__main__":
    sys.exit(main())
