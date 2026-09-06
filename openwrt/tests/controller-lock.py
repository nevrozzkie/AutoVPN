#!/usr/bin/env python3
"""Real kernel flock tests; all commands/configuration are isolated fixtures."""
import fcntl
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(sys.platform == "linux" and shutil.which("flock"), "requires Linux flock")
class ControllerLockTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="autovpn-lock-test-")
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.lock = self.directory / "controller.lock"
        self.children = []
        self.addCleanup(self.stop_children)
        self.script("uci", "#!/bin/sh\nexit 0\n")
        self.script("ucode", f"""#!{sys.executable}
import os, signal, sys
from pathlib import Path
Path(os.environ['TEST_PID']).write_text(str(os.getpid()))
if os.environ.get('TEST_HOLD') == '1':
    signal.pause()
sys.exit(int(os.environ.get('TEST_EXIT', '0')))
""")
        source = (ROOT / "files/usr/sbin/autovpnctl").read_text()
        source = source.replace("/var/lock/autovpn-controller.lock", str(self.lock))
        source = source.replace("/usr/libexec/autovpn/credential.sh", str(ROOT / "files/usr/libexec/autovpn/credential.sh"))
        source = source.replace("/usr/bin/ucode", str(self.directory / "ucode"))
        source = source.replace("CREDENTIAL_FILE=/etc/autovpn/credentials", "CREDENTIAL_FILE=" + str(self.directory / "credentials"))
        self.script("ctl", source)

    def script(self, name, source):
        target = self.directory / name
        target.write_text(source)
        target.chmod(0o700)

    def stop_children(self):
        for process in self.children:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=3)
            for stream in (process.stdin, process.stdout, process.stderr):
                if stream:
                    stream.close()

    def start(self, command="maintenance", **extra):
        env = dict(os.environ, PATH=str(self.directory) + ":" + os.environ["PATH"],
                   TEST_PID=str(self.directory / "helper.pid"), **extra)
        # The optional shell override verifies BusyBox ash as well as /bin/sh.
        shell = os.environ.get("AUTOVPN_TEST_SHELL", "/bin/sh")
        args = [shell] + (["sh"] if Path(shell).name == "busybox" else [])
        process = subprocess.Popen(args + [str(self.directory / "ctl"), command, "nonce-0123456789"],
                                   env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.children.append(process)
        return process

    def locked(self):
        with self.lock.open("a") as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
        return False

    def until(self, predicate):
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.02)
        self.fail("fixture did not reach expected state")

    def test_normal_and_failed_commands_release_without_replacing_inode(self):
        self.lock.write_text("123456\n")  # obsolete legacy PID, not a live owner
        inode = self.lock.stat().st_ino
        for status in (0, 1):
            process = self.start(TEST_EXIT=str(status))
            process.communicate(timeout=4)
            self.assertEqual(process.returncode, status)
            self.assertFalse(self.locked())
            self.assertEqual(self.lock.stat().st_ino, inode)
            self.assertEqual(self.lock.read_text(), "0\n")

    def test_contender_preserves_live_legacy_lock_and_pid(self):
        # Same flock API and inode as the old BusyBox daemon, without creating
        # an actual orphan daemon on the build host.
        self.lock.write_text("123456\n")
        with self.lock.open("r+") as holder:
            fcntl.flock(holder, fcntl.LOCK_EX)
            process = self.start()
            _, stderr = process.communicate(timeout=4)
            self.assertEqual(process.returncode, 75)
            self.assertIn(b'"code":"busy"', stderr)
            self.assertEqual(self.lock.read_text(), "123456\n")
            self.assertFalse((self.directory / "helper.pid").exists())

    def test_credential_cleanup_survives_function_return(self):
        credential = b"avrt_router123." + b"s" * 43 + b"\n"
        process = self.start("set-credential")
        stdout, stderr = process.communicate(credential, timeout=4)
        self.assertEqual(process.returncode, 0, stderr)
        self.assertEqual((self.directory / "credentials").read_bytes(), credential)
        self.assertNotIn(credential.strip(), stdout + stderr)
        self.assertEqual(list(self.directory.glob("credentials.new.*")), [])
        self.assertFalse(self.locked())

    def test_signals_release_shell_owned_lock(self):
        for sig in (signal.SIGHUP, signal.SIGINT, signal.SIGTERM, signal.SIGKILL):
            with self.subTest(signal=sig):
                # Block the shell itself in read, not a child helper.
                process = self.start("set-credential")
                self.until(self.locked)
                process.send_signal(sig)
                process.wait(timeout=4)
                self.until(lambda: not self.locked())
                retry = self.start()
                retry.communicate(timeout=4)
                self.assertEqual(retry.returncode, 0)

    def test_killed_parent_cannot_unlock_live_helper(self):
        process = self.start(TEST_HOLD="1")
        pid_file = self.directory / "helper.pid"
        self.until(pid_file.exists)
        helper_pid = int(pid_file.read_text())
        try:
            process.kill()
            process.wait(timeout=4)
            self.assertTrue(self.locked(), "live helper must retain exclusive ownership")
            contender = self.start()
            contender.communicate(timeout=4)
            self.assertEqual(contender.returncode, 75)
        finally:
            os.kill(helper_pid, signal.SIGKILL)
        self.until(lambda: not self.locked())
        retry = self.start()
        retry.communicate(timeout=4)
        self.assertEqual(retry.returncode, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
