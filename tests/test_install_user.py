"""User installation stays inside temporary XDG paths; no host install occurs."""

import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("install_user", ROOT / "scripts/install_user.py")
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)


class UserInstallerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="z-hotspot-install-test-")
        self.root = Path(self.temporary.name)
        self.source = self.root / "source"
        self.source.mkdir()
        for name in installer.FILES:
            path = self.source / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("fixture: " + name)
        (self.source / ".env").write_text("PRIVATE_TEST_FIXTURE")
        self.destination = self.root / 'data space % " $ ` \\ and العربية'
        self.python = Path(sys.executable).absolute()

    def tearDown(self):
        self.temporary.cleanup()

    def install(self):
        return installer.install(self.source, self.destination, self.python)

    def test_xdg_location_uses_absolute_values_and_ignores_relative_values(self):
        home = self.root / "home"
        self.assertEqual(installer.data_home({}, home), home / ".local/share")
        self.assertEqual(installer.data_home({"XDG_DATA_HOME": "relative/data"}, home), home / ".local/share")
        self.assertEqual(installer.data_home({"XDG_DATA_HOME": str(self.destination)}, home), self.destination)
        with self.assertRaises(ValueError):
            installer.data_home({"XDG_DATA_HOME": "/tmp/data\nExec=unexpected"}, home)

    def test_only_release_allowlist_is_copied_and_updates_preserve_unrelated_files(self):
        app = self.install()
        self.assertFalse((app / ".env").exists())
        for name in installer.FILES:
            self.assertEqual((app / name).read_bytes(), (self.source / name).read_bytes())
        self.assertEqual((app / "create_ap").stat().st_mode & 0o777, 0o755)
        self.assertEqual((app / installer.MARKER).stat().st_mode & 0o777, 0o600)
        unrelated = app / "my-note.txt"
        unrelated.write_text("keep me")
        (self.source / "hotspot_metrics.py").write_text("new release fixture")
        self.install()
        self.assertEqual((app / "hotspot_metrics.py").read_text(), "new release fixture")
        self.assertEqual(unrelated.read_text(), "keep me")

    def test_desktop_launcher_roundtrips_reserved_characters_without_shell_expansion(self):
        from gi.repository import Gio
        capture = self.root / "launched.json"
        (self.source / "hotspot_gui.py").write_text(
            "import json,sys\nfrom pathlib import Path\n"
            f"Path({str(capture)!r}).write_text(json.dumps({{'argv':sys.argv,'python':sys.executable}}))\n"
        )
        app = self.install()
        entry = self.destination / "applications/z-hotspot.desktop"
        desktop = Gio.DesktopAppInfo.new_from_filename(str(entry))
        self.assertIsNotNone(desktop)
        self.assertTrue(desktop.launch([], None))
        deadline = time.monotonic() + 5
        while not capture.exists() and time.monotonic() < deadline:
            time.sleep(.01)
        recorded = json.loads(capture.read_text())
        self.assertEqual(recorded["argv"], [str(app / "hotspot_gui.py"), "--compact"])
        self.assertEqual(Path(recorded["python"]), self.python)
        text = entry.read_text()
        self.assertIn("[Desktop Action Background]", text)
        self.assertIn("--background", text)
        self.assertNotIn("sudo ", text)
        self.assertNotIn("pkexec ", text)

    def test_foreign_folders_and_launchers_are_not_replaced(self):
        app = self.destination / installer.APP_ID
        app.mkdir(parents=True)
        foreign = app / "original.txt"
        foreign.write_text("keep")
        with self.assertRaisesRegex(ValueError, "unrelated folder"):
            self.install()
        self.assertEqual(foreign.read_text(), "keep")
        foreign.unlink()
        entry = self.destination / "applications/z-hotspot.desktop"
        entry.parent.mkdir()
        entry.write_text("[Desktop Entry]\nName=Another app\n")
        with self.assertRaisesRegex(ValueError, "unrelated launcher"):
            self.install()
        self.assertEqual(entry.read_text(), "[Desktop Entry]\nName=Another app\n")
        self.assertFalse((app / "hotspot_gui.py").exists())

    def test_symlink_install_target_is_rejected_without_writing_outside(self):
        outside = self.root / "outside"
        outside.mkdir()
        self.destination.mkdir()
        (self.destination / installer.APP_ID).symlink_to(outside, target_is_directory=True)
        with self.assertRaises(ValueError):
            self.install()
        self.assertEqual(list(outside.iterdir()), [])

    def test_incomplete_or_symlinked_release_files_are_rejected_before_installing(self):
        selected = self.source / "hotspot_share.py"
        selected.unlink()
        with self.assertRaisesRegex(ValueError, "missing regular file"):
            self.install()
        self.assertFalse(self.destination.exists())
        selected.symlink_to(self.source / "hotspot_metrics.py")
        with self.assertRaisesRegex(ValueError, "missing regular file"):
            self.install()
        self.assertFalse(self.destination.exists())

    def test_uninstall_removes_owned_files_but_preserves_unrelated_content(self):
        app = self.install()
        note = app / "my-note.txt"
        note.write_text("keep")
        unrelated_launcher = self.destination / "applications/other.desktop"
        unrelated_launcher.write_text("keep")
        installer.uninstall(self.destination)
        self.assertEqual(note.read_text(), "keep")
        self.assertEqual(unrelated_launcher.read_text(), "keep")
        self.assertFalse((app / "hotspot_gui.py").exists())
        self.assertFalse((self.destination / "applications/z-hotspot.desktop").exists())

    def test_dependency_check_does_not_create_user_files(self):
        with patch.dict(os.environ, {"XDG_DATA_HOME": str(self.destination)}), \
             patch.object(installer, "missing_dependencies", return_value=[]), \
             patch.object(installer.os, "geteuid", return_value=1000), \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(installer.main(["--check"]), 0)
        self.assertFalse(self.destination.exists())

    def test_missing_dependencies_are_reported_before_any_installation(self):
        errors = io.StringIO()
        with patch.dict(os.environ, {"XDG_DATA_HOME": str(self.destination)}), \
             patch.object(installer, "missing_dependencies", return_value=["command: ip", "Python: qrcode"]), \
             patch.object(installer.os, "geteuid", return_value=1000), \
             contextlib.redirect_stderr(errors):
            self.assertEqual(installer.main([]), 1)
        self.assertIn("Missing 2 requirement(s)", errors.getvalue())
        self.assertFalse(self.destination.exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
