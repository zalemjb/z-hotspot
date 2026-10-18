#!/usr/bin/env python3
"""Install Z Hotspot in this Linux user's application menu without sudo."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[1]
APP_ID = "z-hotspot"
MARKER = ".z-hotspot-install.json"
DESKTOP_MARKER = "X-Z-Hotspot-Install=user-v1"
FILES = (
    "hotspot_gui.py", "hotspot_manager.py", "hotspot_metrics.py",
    "hotspot_share.py", "hotspot_qr.py", "hotspot_background.py",
    "create_ap", "icon.svg", "README.md", "README.ar.md", "LICENSE",
    "THIRD_PARTY_NOTICES.md", "licenses/create_ap.txt",
)
EXECUTABLES = {"hotspot_gui.py", "hotspot_manager.py", "create_ap"}
LAUNCHERS = ("z-hotspot.desktop", "z-hotspot-full.desktop")
COMMANDS = ("bash", "getopt", "ps", "sysctl", "stdbuf", "which", "ip",
            "iw", "nmcli", "hostapd", "hostapd_cli", "dnsmasq", "iptables", "pkexec")


def data_home(environ=None, home=None) -> Path:
    """Use the XDG user data location; relative environment values are invalid."""
    environ = os.environ if environ is None else environ
    home = Path.home() if home is None else Path(home)
    value = environ.get("XDG_DATA_HOME", "")
    result = Path(value) if value and Path(value).is_absolute() else home / ".local/share"
    if any(ord(character) < 32 or ord(character) == 127 for character in str(result)):
        raise ValueError("The installation path must not contain control characters.")
    return result


def missing_dependencies(python: Path) -> list[str]:
    """Inspect requirements without installing packages or opening the GUI."""
    missing = []
    search_path = os.pathsep.join((os.environ.get("PATH", ""), "/usr/sbin", "/sbin"))
    for command in COMMANDS:
        if not shutil.which(command, path=search_path):
            missing.append(f"command: {command}")
    probe = r'''
import json
missing = []
try:
    import gi
except ImportError:
    missing.append("Python: PyGObject (gi), GTK 3, Ayatana AppIndicator")
else:
    for name, version in (("Gtk", "3.0"), ("AyatanaAppIndicator3", "0.1")):
        try:
            gi.require_version(name, version)
            getattr(__import__("gi.repository", fromlist=[name]), name)
        except (ImportError, ValueError):
            missing.append("Python/GI: " + name + " " + version)
for name, label in (("qrcode", "qrcode"), ("PIL", "Pillow")):
    try:
        __import__(name)
    except ImportError:
        missing.append("Python: " + label)
print(json.dumps(missing))
'''
    try:
        result = subprocess.run([str(python), "-c", probe], capture_output=True,
                                text=True, timeout=20, check=False)
        if result.returncode:
            missing.append("Python: could not load GTK/QR dependencies with this interpreter")
        else:
            missing.extend(json.loads(result.stdout))
    except (OSError, subprocess.TimeoutExpired, ValueError, TypeError):
        missing.append("Python: could not check GTK/QR dependencies with this interpreter")
    return missing


def desktop_argument(value: str) -> str:
    """Quote one Exec argument according to the freedesktop desktop-entry spec."""
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ValueError("Launcher arguments must not contain control characters.")
    value = value.replace("%", "%%")
    quoted = "".join("\\" + character if character in '\\"`$' else character for character in value)
    # Desktop string unescaping happens before Exec argument unquoting.
    return '"' + quoted.replace("\\", "\\\\") + '"'


def desktop_text(app: Path, python: Path, full=False) -> str:
    command = desktop_argument(str(python)) + " " + desktop_argument(str(app / "hotspot_gui.py"))
    icon = str(app / "icon.svg").replace("\\", "\\\\")
    text = (
        "[Desktop Entry]\nVersion=1.0\nType=Application\n"
        f"Name={'Z Hotspot — Full view' if full else 'Z Hotspot'}\n"
        "Comment=Hotspot controls, connected devices, and local file sharing\n"
        f"Exec={command} {'--full' if full else '--compact'}\nIcon={icon}\n"
        "Terminal=false\nCategories=Network;GTK;\n"
        "Keywords=WiFi;Hotspot;Network;Internet;Files;QR;\n"
        "StartupNotify=true\nStartupWMClass=Hotspot Manager\n"
        f"{DESKTOP_MARKER}\n"
    )
    if not full:
        text += (
            "Actions=Full;Background;\n\n[Desktop Action Full]\n"
            f"Name=Open full dashboard\nExec={command} --full\n\n"
            "[Desktop Action Background]\nName=Run in background\n"
            f"Exec={command} --background\n"
        )
    return text


def _ensure_directory(path: Path) -> None:
    if path.is_symlink():
        raise ValueError(f"Choose a regular installation directory: {path}")
    path.mkdir(parents=True, exist_ok=True)
    if not path.is_dir():
        raise ValueError(f"This path is not a directory: {path}")


def _owned_install(app: Path) -> bool:
    marker = app / MARKER
    if marker.is_symlink() or not marker.is_file():
        return False
    try:
        value = json.loads(marker.read_text(encoding="utf-8"))
        return value.get("application") == APP_ID and value.get("schema") == 1 and value.get("files") == list(FILES)
    except (OSError, ValueError, AttributeError):
        return False


def _check_targets(destination: Path) -> tuple[Path, Path]:
    app, menus = destination / APP_ID, destination / "applications"
    if app.is_symlink() or (app.exists() and not app.is_dir()):
        raise ValueError(f"The application path is not a regular directory: {app}")
    if app.exists() and any(app.iterdir()) and not _owned_install(app):
        raise ValueError(f"Refusing to replace an unrelated folder: {app}")
    if (app / "licenses").is_symlink():
        raise ValueError("The installed licenses directory must not be a symlink.")
    if menus.is_symlink() or (menus.exists() and not menus.is_dir()):
        raise ValueError(f"The application-menu path is not a regular directory: {menus}")
    for filename in LAUNCHERS:
        entry = menus / filename
        if entry.is_symlink() or (entry.exists() and (not entry.is_file() or DESKTOP_MARKER not in entry.read_text(encoding="utf-8"))):
            raise ValueError(f"Refusing to replace an unrelated launcher: {entry}")
    return app, menus


def _write_atomic(path: Path, data: bytes, mode: int) -> None:
    _ensure_directory(path.parent)
    descriptor, temporary = tempfile.mkstemp(prefix=".z-hotspot-", dir=path.parent)
    temporary = Path(temporary)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            os.fchmod(stream.fileno(), mode)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def install(source: Path, destination: Path, python: Path) -> Path:
    """Copy only release files and register menu entries at an explicit location."""
    source, destination, python = Path(source), Path(destination), Path(python)
    if not destination.is_absolute() or not python.is_absolute():
        raise ValueError("Installation and Python paths must be absolute.")
    if "=" in str(python):
        raise ValueError("The Python interpreter path must not contain '=' for desktop launchers.")
    if any(ord(character) < 32 or ord(character) == 127 for character in str(destination)):
        raise ValueError("The installation path must not contain control characters.")
    app, menus = _check_targets(destination)
    payloads = {}
    for filename in FILES:
        path = source / filename
        if path.is_symlink() or not path.is_file():
            raise ValueError(f"Extract the complete source release first; missing regular file: {filename}")
        payloads[filename] = path.read_bytes()
    entries = {filename: desktop_text(app, python, full=index == 1).encode("utf-8")
               for index, filename in enumerate(LAUNCHERS)}
    _ensure_directory(destination)
    _ensure_directory(app)
    for filename, payload in payloads.items():
        _write_atomic(app / filename, payload, 0o755 if filename in EXECUTABLES else 0o644)
    manifest = {"application": APP_ID, "schema": 1, "files": list(FILES), "python": str(python)}
    _write_atomic(app / MARKER, (json.dumps(manifest, indent=2) + "\n").encode("utf-8"), 0o600)
    for filename, payload in entries.items():
        _write_atomic(menus / filename, payload, 0o644)
    return app


def uninstall(destination: Path) -> None:
    """Remove this install's known files while preserving unrelated content."""
    destination = Path(destination)
    app, menus = _check_targets(destination)
    if not _owned_install(app):
        raise ValueError(f"No recognized Z Hotspot user installation at {app}")
    for filename in LAUNCHERS:
        entry = menus / filename
        if entry.is_file() and DESKTOP_MARKER in entry.read_text(encoding="utf-8"):
            entry.unlink()
    for filename in FILES:
        path = app / filename
        if path.is_file() or path.is_symlink():
            path.unlink()
    (app / MARKER).unlink()
    for directory in (app / "licenses", app):
        try:
            directory.rmdir()
        except OSError:
            pass


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--check", action="store_true", help="check dependencies without installing")
    group.add_argument("--uninstall", action="store_true", help="remove this user's source installation")
    args = parser.parse_args(argv)
    if sys.platform != "linux":
        parser.error("Z Hotspot requires Linux.")
    if os.geteuid() == 0:
        parser.error("Run this installer as your normal desktop user, without sudo.")
    try:
        destination = data_home()
        if args.uninstall:
            uninstall(destination)
            print("Removed the Z Hotspot user application and menu entries.")
            print("Network settings and any running hotspot were left in place.")
            return 0
        python = Path(sys.executable).absolute()
        missing = missing_dependencies(python)
        if missing:
            print(f"Missing {len(missing)} requirement(s) for {python}:", file=sys.stderr)
            for item in missing:
                print(f"  - {item}", file=sys.stderr)
            print("Install these with your Linux distribution's package manager, then try again. See README.md.", file=sys.stderr)
            return 1
        if args.check:
            print("All required tools and Python libraries are available.")
            return 0
        app = install(ROOT, destination, python)
        print(f"Installed Z Hotspot in {app}")
        print("Open Z Hotspot or Z Hotspot — Full view from your application menu.")
        return 0
    except (OSError, ValueError) as error:
        print(f"Installation could not finish: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
