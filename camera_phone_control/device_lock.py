"""Per-USB-device, nonblocking locks shared by local dashboard/runner processes."""

import errno
import fcntl
import hashlib
import os
import stat
import uuid
from contextlib import contextmanager
from pathlib import Path


class DeviceBusy(RuntimeError):
    def __init__(self, owner="controller"):
        self.owner = owner if owner in {"manual", "automation"} else "controller"
        super().__init__("Device busy. No input sent.")


class NoAutomation(RuntimeError):
    pass


class DeviceLocks:
    def __init__(self, directory=None):
        # Stable across terminal/server working directories and TMPDIR settings.
        self.directory = Path(directory or f"/tmp/camera-phone-control-{os.getuid()}")

    def _open(self, serial, suffix=".lock"):
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = self.directory.lstat()
        if (
            not stat.S_ISDIR(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_mode & 0o077
        ):
            raise OSError("Device lock directory must be private to the current user.")
        name = hashlib.sha256(serial.encode("utf-8")).hexdigest() + suffix
        return os.open(
            self.directory / name, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600
        )

    @staticmethod
    def _try_lock(fd):
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError as exc:
            if exc.errno not in (errno.EACCES, errno.EAGAIN):
                raise
            return False

    @contextmanager
    def _guard(self, serial):
        # Serialize short metadata operations, never commands or waits. A status
        # probe must not briefly look like another manual claimant to a requester.
        fd = self._open(serial, ".guard")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            os.close(fd)

    def _acquire(self, serial, owner):
        with self._guard(serial):
            fd = self._open(serial)
            try:
                if not self._try_lock(fd):
                    raise DeviceBusy(
                        os.pread(fd, 32, 0).decode("ascii", errors="ignore")
                    )
                os.ftruncate(fd, 0)
                os.write(fd, owner.encode("ascii"))
                return fd
            except BaseException:
                os.close(fd)
                raise

    def owner(self, serial):
        with self._guard(serial):
            fd = self._open(serial)
            try:
                if self._try_lock(fd):
                    return None
                return DeviceBusy(
                    os.pread(fd, 32, 0).decode("ascii", errors="ignore")
                ).owner
            finally:
                os.close(fd)

    def takeover_signal(self, serial):
        with self._guard(serial):
            fd = self._open(serial, ".takeover")
            try:
                pending = not self._try_lock(fd)
                return pending, os.pread(fd, 64, 0)
            finally:
                os.close(fd)

    def request_takeover(self, serial):
        """Signal only a currently automation-owned device; never send input."""
        with self._guard(serial):
            device_fd = self._open(serial)
            try:
                if self._try_lock(device_fd):
                    raise NoAutomation("No automation currently owns this device.")
                owner = os.pread(device_fd, 32, 0).decode("ascii", errors="ignore")
                if owner != "automation":
                    raise DeviceBusy(owner)
            finally:
                os.close(device_fd)
            fd = self._open(serial, ".takeover")
            try:
                if not self._try_lock(fd):
                    raise DeviceBusy("manual")
                # Retain a generation even after withdrawal/expiry so a running script
                # cannot miss a short-lived request and silently resume later.
                os.ftruncate(fd, 0)
                os.write(fd, uuid.uuid4().hex.encode("ascii"))
                return ManualClaim(self, serial, fd)
            except BaseException:
                os.close(fd)
                raise

    @contextmanager
    def hold(self, serial, owner):
        if owner not in {"manual", "automation"}:
            raise ValueError("Invalid device lock owner.")
        ownership = None
        if owner == "automation":
            pending, generation = self.takeover_signal(serial)
            if pending:
                raise DeviceBusy("manual")
            ownership = AutomationOwnership(self, serial, generation)
        fd = self._acquire(serial, owner)
        try:
            if ownership and ownership.takeover_requested():
                raise DeviceBusy("manual")
            if owner == "manual" and self.takeover_signal(serial)[0]:
                raise DeviceBusy("manual")
            yield ownership
        finally:
            os.close(fd)  # Closing the descriptor always releases the OS lock.
        # Never unlink lock files; processes may already have opened their inodes.


class AutomationOwnership:
    def __init__(self, locks, serial, generation):
        self.locks, self.serial, self.generation = locks, serial, generation

    def takeover_requested(self):
        pending, generation = self.locks.takeover_signal(self.serial)
        return pending or generation != self.generation


class ManualClaim:
    def __init__(self, locks, serial, priority_fd):
        self.locks, self.serial = locks, serial
        self.priority_fd = priority_fd
        self.device_fd = None

    @property
    def acquired(self):
        return self.device_fd is not None

    def try_acquire(self):
        if self.priority_fd is None:
            raise RuntimeError("Manual ownership was released.")
        if not self.acquired:
            try:
                self.device_fd = self.locks._acquire(self.serial, "manual")
            except DeviceBusy:
                return False
        return True

    def close(self):
        try:
            if self.device_fd is not None:
                os.close(self.device_fd)
                self.device_fd = None
        finally:
            if self.priority_fd is not None:
                os.close(self.priority_fd)
                self.priority_fd = None
