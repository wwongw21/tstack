"""Offline process tests for the real PR watcher's lifetime lock."""
import concurrent.futures
import errno
import importlib.machinery
import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

SCRIPT = Path(__file__).resolve().parents[1] / "skills" / "babysit-pr" / "scripts" / "pr-wait"
CHILD = """
import importlib.machinery, importlib.util, os, pathlib, sys
loader = importlib.machinery.SourceFileLoader('pr_wait_test_child', sys.argv[1])
spec = importlib.util.spec_from_loader(loader.name, loader)
module = importlib.util.module_from_spec(spec)
loader.exec_module(module)
module.STATE_DIR = pathlib.Path(sys.argv[2])
handle = module.lock('example/project', int(sys.argv[3]))
print('locked', flush=True)
if sys.argv[4] == 'hold':
    sys.stdin.readline()
elif sys.argv[4] == 'exit':
    os._exit(7)
handle.close()
"""


def load_module():
    loader = importlib.machinery.SourceFileLoader("pr_wait_test", str(SCRIPT))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


class WatcherLockTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="tstack-lock-test-")
        self.addCleanup(self.directory.cleanup)
        self.state = Path(self.directory.name)
        self.children = []
        self.addCleanup(self.close_children)

    def close_children(self):
        for child in self.children:
            if child.poll() is None:
                child.communicate("\n", timeout=5)
            if child.stdin and not child.stdin.closed:
                child.stdin.close()
            if child.stdout:
                child.stdout.close()
            if child.stderr:
                child.stderr.close()

    def args(self, pr=1, mode="once"):
        return [sys.executable, "-B", "-c", CHILD, str(SCRIPT), str(self.state), str(pr), mode]

    def hold(self, pr=1):
        child = subprocess.Popen(self.args(pr, "hold"), stdin=subprocess.PIPE,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.children.append(child)
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as reader:
            line = reader.submit(child.stdout.readline).result(timeout=10)
        self.assertEqual(line.strip(), "locked")
        self.assertIsNone(child.poll())
        return child

    def once(self, pr=1, mode="once"):
        return subprocess.run(self.args(pr, mode), capture_output=True, text=True, timeout=10)

    def record(self):
        with open(self.state / "example__project__1.lock", "rb", buffering=0) as handle:
            return handle.read(64)

    def test_help_runs_without_gh_or_network(self):
        environment = os.environ.copy()
        environment["PATH"] = str(self.state)
        result = subprocess.run([sys.executable, "-B", str(SCRIPT), "--help"],
                                env=environment, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Watch a GitHub PR", result.stdout)

    def test_second_process_is_rejected_without_changing_owner_record(self):
        owner = self.hold()
        previous = self.record()
        self.assertEqual(previous.decode(), str(owner.pid))
        rejected = self.once()
        self.assertEqual(rejected.returncode, 2, rejected.stderr)
        self.assertIn(f"pid {owner.pid} already watches example/project#1", rejected.stdout)
        self.assertEqual(self.record(), previous)
        self.assertIsNone(owner.poll())

    def test_lock_releases_on_normal_close(self):
        owner = self.hold()
        owner.communicate("\n", timeout=5)
        self.assertEqual(owner.returncode, 0)
        result = self.once()
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_distinct_pull_requests_can_be_watched_together(self):
        self.hold(1)
        result = self.once(2)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_operating_system_releases_lock_after_process_exit(self):
        exited = self.once(mode="exit")
        self.assertEqual(exited.returncode, 7)
        result = self.once()
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_record_write_failure_closes_handle_and_releases_lock(self):
        module = load_module()
        module.STATE_DIR = self.state
        original_open = open
        opened = []

        class WriteFailure:
            def __init__(self, handle):
                self.handle = handle
            def __getattr__(self, name):
                return getattr(self.handle, name)
            def write(self, _data):
                raise OSError(errno.ENOSPC, "synthetic disk full")

        def failing_open(*args, **kwargs):
            handle = original_open(*args, **kwargs)
            opened.append(handle)
            return WriteFailure(handle)

        with mock.patch.object(module, "open", failing_open, create=True):
            with self.assertRaisesRegex(OSError, "synthetic disk full"):
                module.lock("example/project", 1)
        self.assertTrue(opened[0].closed)
        self.assertEqual(self.once().returncode, 0)

    def test_unexpected_lock_error_is_not_reported_as_another_watcher(self):
        module = load_module()
        module.STATE_DIR = self.state
        with mock.patch.object(module, "_lock_handle", side_effect=OSError(errno.EIO, "synthetic I/O error")):
            with self.assertRaisesRegex(OSError, "synthetic I/O error"):
                module.lock("example/project", 1)
        self.assertEqual(self.once().returncode, 0)


if __name__ == "__main__":
    unittest.main()
