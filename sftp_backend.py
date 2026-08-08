"""
sftp_backend.py — a real paramiko SFTP subsystem backed by a temp directory.

Used for `type: sftp` nodes (repo_server_sftp / Mode B), which the Java engine
drives with a genuine JSch `sftp` channel: put / get / lstat / mkdir, honouring
overwrite / create_dirs.

The SAME backing directory is shared with the shell command engine (see
command_engine.CommandEngine), so a file "uploaded" by an in-shell `sftp put`
on the SBC node can later be `get`-fetched here and round-trips byte-for-byte —
which is what keeps node/repo/local checksums consistent.

Remote absolute paths (e.g. /repo-server/config/.../Signaling_Activity.xml) are
mapped under <root>/<path>.  This is the classic paramiko StubSFTPServer pattern,
hardened a little.
"""

from __future__ import annotations

import errno
import os
import threading

import paramiko


class FakeRepoFS:
    """Thin helper around the backing root dir, shared with the command engine."""

    def __init__(self, root: str):
        self.root = os.path.abspath(root)
        os.makedirs(self.root, exist_ok=True)
        self._lock = threading.Lock()

    def _real(self, remote_path: str) -> str:
        # normalise a posix-style absolute remote path into the local root
        p = remote_path.replace("\\", "/")
        p = p.lstrip("/")
        real = os.path.normpath(os.path.join(self.root, p))
        # contain within root
        if not real.startswith(self.root):
            real = self.root
        return real

    def put_bytes(self, remote_path: str, data: bytes) -> None:
        real = self._real(remote_path)
        with self._lock:
            os.makedirs(os.path.dirname(real), exist_ok=True)
            with open(real, "wb") as fh:
                fh.write(data)

    def exists(self, remote_path: str) -> bool:
        return os.path.exists(self._real(remote_path))


class _SFTPHandle(paramiko.SFTPHandle):
    def __init__(self, flags=0):
        super().__init__(flags)
        self.readfile = None
        self.writefile = None

    def stat(self):
        try:
            return paramiko.SFTPAttributes.from_stat(os.fstat(self.readfile.fileno()))
        except OSError as e:
            return paramiko.SFTPServer.convert_errno(e.errno)

    def chattr(self, attr):
        return paramiko.SFTP_OK


class FakeSFTPServer(paramiko.SFTPServerInterface):
    """Maps the SFTP namespace onto FakeRepoFS.root.  One instance per channel."""

    # class attribute set by the server bootstrap before set_subsystem_handler
    ROOT = None  # type: ignore

    def __init__(self, server, *args, **kwargs):
        super().__init__(server, *args, **kwargs)
        self.root = self.ROOT

    # -- path mapping ------------------------------------------------------- #
    def _realpath(self, path):
        p = self.canonicalize(path)
        p = p.replace("\\", "/").lstrip("/")
        real = os.path.normpath(os.path.join(self.root, p))
        if not real.startswith(self.root):
            real = self.root
        return real

    def canonicalize(self, path):
        # keep posix semantics; default paramiko canonicalize is fine
        if not path or path == ".":
            return "/"
        if not path.startswith("/"):
            path = "/" + path
        return os.path.normpath(path).replace("\\", "/")

    # -- operations --------------------------------------------------------- #
    def list_folder(self, path):
        real = self._realpath(path)
        try:
            out = []
            for fname in os.listdir(real):
                attr = paramiko.SFTPAttributes.from_stat(os.stat(os.path.join(real, fname)))
                attr.filename = fname
                out.append(attr)
            return out
        except OSError as e:
            return paramiko.SFTPServer.convert_errno(e.errno)

    def stat(self, path):
        try:
            return paramiko.SFTPAttributes.from_stat(os.stat(self._realpath(path)))
        except OSError as e:
            return paramiko.SFTPServer.convert_errno(e.errno)

    def lstat(self, path):
        try:
            return paramiko.SFTPAttributes.from_stat(os.lstat(self._realpath(path)))
        except OSError as e:
            return paramiko.SFTPServer.convert_errno(e.errno)

    def open(self, path, flags, attr):
        real = self._realpath(path)
        try:
            if flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_APPEND):
                os.makedirs(os.path.dirname(real), exist_ok=True)
            binflags = flags | getattr(os, "O_BINARY", 0)
            fd = os.open(real, binflags, 0o644)
        except OSError as e:
            return paramiko.SFTPServer.convert_errno(e.errno)
        if flags & os.O_WRONLY:
            mode = "ab" if (flags & os.O_APPEND) else "wb"
        elif flags & os.O_RDWR:
            mode = "a+b" if (flags & os.O_APPEND) else "r+b"
        else:
            mode = "rb"
        try:
            f = os.fdopen(fd, mode)
        except OSError as e:
            return paramiko.SFTPServer.convert_errno(e.errno)
        h = _SFTPHandle(flags)
        h.filename = real
        h.readfile = f
        h.writefile = f
        return h

    def remove(self, path):
        try:
            os.remove(self._realpath(path))
            return paramiko.SFTP_OK
        except OSError as e:
            return paramiko.SFTPServer.convert_errno(e.errno)

    def rename(self, oldpath, newpath):
        try:
            os.rename(self._realpath(oldpath), self._realpath(newpath))
            return paramiko.SFTP_OK
        except OSError as e:
            return paramiko.SFTPServer.convert_errno(e.errno)

    def mkdir(self, path, attr):
        try:
            os.makedirs(self._realpath(path), exist_ok=True)
            return paramiko.SFTP_OK
        except OSError as e:
            return paramiko.SFTPServer.convert_errno(e.errno)

    def rmdir(self, path):
        try:
            os.rmdir(self._realpath(path))
            return paramiko.SFTP_OK
        except OSError as e:
            return paramiko.SFTPServer.convert_errno(e.errno)

    def chattr(self, path, attr):
        return paramiko.SFTP_OK  # accept and ignore (no real perms in the fake FS)
