"""Functional self-test: boots clicr_sim and drives it like the JSch client would."""
import io
import re
import subprocess
import sys
import time
import os

import paramiko

HERE = os.path.dirname(os.path.abspath(__file__))
PORT = 2233
FAILS = []


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else f"  <-- {detail}"))
    if not cond:
        FAILS.append(name)


def read_until(chan, pattern, timeout=15):
    buf = ""
    end = time.time() + timeout
    pat = re.compile(pattern, re.S)
    while time.time() < end:
        if chan.recv_ready():
            buf += chan.recv(65536).decode("utf-8", "replace")
            if pat.search(buf):
                return buf
        else:
            time.sleep(0.05)
    return buf  # timed out; return what we have


PROMPT = r"#\s$"  # SBC prompt tail "# "


def main():
    srv = subprocess.Popen(
        [sys.executable, os.path.join(HERE, "clicr_sim.py"), "--port", str(PORT), "-v"],
        cwd=HERE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    time.sleep(2.0)
    try:
        # ---- SHELL (sbc profile) ----
        t = paramiko.Transport(("127.0.0.1", PORT))
        t.connect(username="a1tuj88u", password="x")
        chan = t.open_session()
        chan.get_pty()
        chan.invoke_shell()

        out = read_until(chan, r"acknowledge message of the day:")
        check("login: MOTD prompt emitted", "acknowledge message of the day:" in out, out[-120:])
        chan.sendall(b"\n")
        out = read_until(chan, r"Command line editor")
        check("login: editor prompt emitted", "Command line editor" in out, out[-120:])
        chan.sendall(b"\n")
        out = read_until(chan, PROMPT)
        check("login: shell prompt #1", re.search(PROMPT, out) is not None, out[-120:])

        # stty -echo (first thing the engine sends)
        chan.sendall(b"stty -echo\n")
        out = read_until(chan, PROMPT)
        check("stty -echo -> prompt", re.search(PROMPT, out) is not None, out[-120:])

        # marker command (no step prompt_regex case): sha256sum with sentinel
        chan.sendall(b"sha256sum /storage/Signaling_SBC-1_2026-06-29_10-00-00.xml | awk '{print $1}'; echo __CMD_DONE__:$?\n")
        out = read_until(chan, r"__CMD_DONE__:\d")
        out += read_until(chan, PROMPT)
        m = re.search(r"\b([0-9a-f]{64})\b", out)
        check("sha256: 64-hex emitted", m is not None, out[-160:])
        check("sha256: marker __CMD_DONE__:0", "__CMD_DONE__:0" in out, out[-160:])
        node_hash = m.group(1) if m else None

        # same logical file from repo side must match (deterministic checksum)
        t2 = paramiko.Transport(("127.0.0.1", PORT))
        t2.connect(username="installer", password="x")
        c2 = t2.open_session(); c2.get_pty(); c2.invoke_shell()
        read_until(c2, r"[#$]\s$")  # repo prompt
        c2.sendall(b"stty -echo\n"); read_until(c2, r"[#$]\s$")
        c2.sendall(b"sha256sum /repo-server/x/Signaling_Activity.xml | awk '{print $1}'; echo __CMD_DONE__:$?\n")
        o2 = read_until(c2, r"__CMD_DONE__:\d")
        m2 = re.search(r"\b([0-9a-f]{64})\b", o2)
        repo_hash = m2.group(1) if m2 else None
        check("checksum node==repo (same logical file)", node_hash and node_hash == repo_hash,
              f"node={node_hash} repo={repo_hash}")
        t2.close()

        # prompt-completion command (no marker): alarm_check
        chan.sendall(b"alarm_check --action health\n")
        out = read_until(chan, PROMPT)
        check("alarm_check -> 'Alarm health check passed'", "Alarm health check passed" in out, out[-160:])

        # in-shell sftp: must emit password: then 100%
        chan.sendall(b'printf "put /storage/Signaling_SBC-1_x.xml /repo-server/c/Signaling_Activity.xml\\nbye\\n" | sftp -o StrictHostKeyChecking=no installer@10.0.0.9\n')
        out = read_until(chan, r"password:")
        check("in-shell sftp: 'password:' prompt", "password:" in out, out[-160:])
        chan.sendall(b"secret\r")  # password reply (CR-terminated like expect_reply)
        out = read_until(chan, r"100%")
        out += read_until(chan, PROMPT)
        check("in-shell sftp: '100%' progress + prompt", "100%" in out, out[-200:])

        t.close()

        # ---- SFTP subsystem (Mode B) ----
        t3 = paramiko.Transport(("127.0.0.1", PORT))
        t3.connect(username="installer", password="x")
        sftp = paramiko.SFTPClient.from_transport(t3)
        payload = b"hello-clicr-sim-roundtrip\n"
        local_in = os.path.join(HERE, "_t_in.bin")
        local_out = os.path.join(HERE, "_t_out.bin")
        with open(local_in, "wb") as f:
            f.write(payload)
        sftp.put(local_in, "/repo-server/config/roundtrip/test.bin")
        sftp.get("/repo-server/config/roundtrip/test.bin", local_out)
        with open(local_out, "rb") as f:
            got = f.read()
        check("sftp subsystem: put/get round-trips bytes", got == payload, f"{got!r}")
        sftp.close(); t3.close()
        for p in (local_in, local_out):
            try: os.remove(p)
            except OSError: pass

    finally:
        srv.terminate()
        try:
            srv.wait(timeout=5)
        except Exception:
            srv.kill()

    print("\n" + ("ALL PASSED" if not FAILS else f"{len(FAILS)} FAILED: {FAILS}"))
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
