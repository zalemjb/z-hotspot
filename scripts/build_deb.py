#!/usr/bin/env python3
"""Build the installable Z Hotspot package without changing the running system.

The historical package ID and installation path are kept so existing installs
upgrade in place. The Debian epoch permits the public v0.1 release to replace
older local 1.x builds.
"""

from __future__ import annotations

import argparse
from pathlib import Path
import re
import shutil
import subprocess
import tempfile


ROOT = Path(__file__).resolve().parents[1]
VERSION = "0.1.0"
DEBIAN_VERSION = "1:0.1.0"
MAINTAINER = "zalemjb <260015058+zalemjb@users.noreply.github.com>"
APP_FILES = (
    "hotspot_gui.py", "hotspot_manager.py", "hotspot_metrics.py",
    "hotspot_share.py", "hotspot_qr.py", "hotspot_background.py",
    "create_ap", "icon.svg", "README.md", "LICENSE", "THIRD_PARTY_NOTICES.md",
    "licenses/create_ap.txt",
)
EXECUTABLES = {"hotspot_gui.py", "hotspot_manager.py", "create_ap"}
DEPENDENCIES = (
    "python3 (>= 3.10), python3-gi, python3-qrcode, gir1.2-gtk-3.0, "
    "gir1.2-ayatanaappindicator3-0.1, hostapd, dnsmasq-base, iw, iptables, "
    "network-manager, pkexec"
)
DESKTOP = """[Desktop Entry]
Version=1.0
Type=Application
Name={name}
Comment={comment}
Exec=/usr/bin/python3 /opt/hotspot-manager/hotspot_gui.py {flag}
Icon=z-hotspot
Terminal=false
Categories=Network;GTK;
Keywords=WiFi;Hotspot;Network;Internet;Files;QR;
StartupNotify=true
StartupWMClass=Hotspot Manager
"""


def copy_file(source: Path, target: Path, *, executable: bool = False) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, target)
    target.chmod(0o755 if executable else 0o644)


def build(output_dir: Path, version: str, debian_version: str) -> Path:
    if not re.fullmatch(r"[0-9]+(?:\.[0-9]+){1,3}", version):
        raise ValueError("Use a numeric release version such as 0.1.0.")
    if not re.fullmatch(r"(?:[0-9]+:)?[0-9][0-9A-Za-z.+:~\-]*", debian_version):
        raise ValueError("Invalid Debian version.")
    missing = [name for name in APP_FILES if not (ROOT / name).is_file()]
    if missing:
        raise FileNotFoundError("Missing release files: " + ", ".join(missing))
    for command in ("dpkg-deb", "desktop-file-validate"):
        if not shutil.which(command):
            raise RuntimeError(f"Install the build dependency containing {command} first.")

    output_dir.mkdir(parents=True, exist_ok=True)
    output = output_dir / f"z-hotspot_{version}_all.deb"
    with tempfile.TemporaryDirectory(prefix="z-hotspot-package-") as temporary:
        package = Path(temporary)
        app = package / "opt/hotspot-manager"
        for name in APP_FILES:
            copy_file(ROOT / name, app / name, executable=name in EXECUTABLES)

        menus = package / "usr/share/applications"
        menus.mkdir(parents=True)
        for filename, name, flag, comment in (
            ("codex-hotspot-manager.desktop", "Z Hotspot", "--compact",
             "Control your hotspot from a compact desktop dashboard"),
            ("codex-hotspot-manager-full.desktop", "Z Hotspot — Full view", "--full",
             "Hotspot settings, connected devices, and local file sharing"),
        ):
            entry = menus / filename
            text = DESKTOP.format(name=name, flag=flag, comment=comment)
            if flag == "--compact":
                text += """Actions=Full;Background;

[Desktop Action Full]
Name=Open full dashboard
Exec=/usr/bin/python3 /opt/hotspot-manager/hotspot_gui.py --full

[Desktop Action Background]
Name=Run in background
Exec=/usr/bin/python3 /opt/hotspot-manager/hotspot_gui.py --background
"""
            entry.write_text(text, encoding="utf-8")
            entry.chmod(0o644)
            subprocess.run(["desktop-file-validate", str(entry)], check=True)

        copy_file(ROOT / "icon.svg", package / "usr/share/icons/hicolor/scalable/apps/z-hotspot.svg")
        documentation = package / "usr/share/doc/mint-hotspot-manager"
        for source, target in (("LICENSE", "copyright"),
                               ("THIRD_PARTY_NOTICES.md", "THIRD_PARTY_NOTICES.md"),
                               ("licenses/create_ap.txt", "create_ap-license.txt")):
            copy_file(ROOT / source, documentation / target)

        control = package / "DEBIAN"
        control.mkdir()
        installed_size = (sum(path.stat().st_size for path in package.rglob("*") if path.is_file()) + 1023) // 1024
        (control / "control").write_text(
            f"Package: mint-hotspot-manager\nVersion: {debian_version}\n"
            "Section: net\nPriority: optional\nArchitecture: all\n"
            f"Installed-Size: {installed_size}\nMaintainer: {MAINTAINER}\n"
            "Homepage: https://github.com/zalemjb/z-hotspot\n"
            f"Depends: {DEPENDENCIES}\n"
            "Description: Z Hotspot desktop Wi-Fi and file-sharing manager\n"
            " Native GTK compact and full dashboards for hotspot control, network\n"
            " traffic, connected devices, Wi-Fi QR codes, and opt-in local file\n"
            " sharing. Includes an optional system-tray background mode.\n",
            encoding="utf-8",
        )
        (control / "control").chmod(0o644)
        for directory in (package, *(path for path in package.rglob("*") if path.is_dir())):
            directory.chmod(0o755)
        subprocess.run(["dpkg-deb", "--root-owner-group", "--build", str(package), str(output)], check=True)
    return output


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=ROOT / "dist")
    parser.add_argument("--version", default=VERSION, help="asset version (default: %(default)s)")
    parser.add_argument("--debian-version", default=DEBIAN_VERSION,
                        help="internal upgrade version (default: %(default)s)")
    args = parser.parse_args()
    print(build(args.output_dir.resolve(), args.version, args.debian_version))


if __name__ == "__main__":
    main()
