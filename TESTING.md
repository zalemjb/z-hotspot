# Testing

## Automated checks

Use the system Python with GTK 3/PyGObject and the project's system dependencies available. On a Debian/Ubuntu/Mint development machine, the additional test and build dependencies can be installed with:

```sh
sudo apt install python3-gi python3-cairo python3-gi-cairo python3-qrcode \
  python3-pil gir1.2-gtk-3.0 gir1.2-ayatanaappindicator3-0.1 libzbar0 \
  xvfb xauth desktop-file-utils dpkg-dev
```

Run the suite from the repository root:

```sh
xvfb-run -a /usr/bin/python3 -m unittest discover -s tests -p 'test_*.py'
```

The suite covers input validation, backend request handling, settings rollback, traffic measurements, GUI state with demo/mocked backends, file-sharing lifecycle, restricted firewall rule construction, QR payloads, and actual local HTTP transfers. Automated network-control tests use mocks rather than starting a hotspot on the host.

Loopback file-transfer checks include downloads and uploads, SHA-256 comparisons, concurrent transfers, path handling, upload cleanup, and mobile/desktop browser User-Agent profiles. A simulated User-Agent verifies server behavior for that profile; it does not verify a physical device or camera QR scanning.

For a manual UI preview without changing network settings:

```sh
python3 hotspot_gui.py --demo
python3 hotspot_gui.py --demo --full
```

## Hardware checks

The known working setup is Linux Mint 22.3 with a compatible Wi-Fi adapter receiving a 2.4 GHz connection and creating a hotspot on the same channel. Broader distribution, driver, and adapter compatibility remains unverified.

When testing on your own hardware:

1. Record the operating system, adapter, driver, frequency, and upstream source. Avoid publishing private identifiers.
2. Unlock controls, start the hotspot, join a second device, and confirm local access and Internet access where expected.
3. Check that device count and traffic change when the second device transfers data.
4. Verify disconnect, block, and unblock using a device you control. Confirm how that device's private/random MAC setting affects the test.
5. Scan the join QR and confirm the offered network is correct.
6. Start file sharing with a test file, scan its QR, and confirm the receiver's saved file matches. Test an upload into a temporary receiving folder.
7. Keep a transfer active while switching views or moving the app to the tray. Then stop sharing and confirm the old URL no longer works.
8. Stop the hotspot and confirm file sharing is also closed. Restore normal connectivity when finished.

Physical iPhone, Samsung, and other laptop transfers should be recorded with their actual OS/browser versions and results before being described as tested. The included loopback tests alone do not establish device compatibility.

## Packaging

```sh
/usr/bin/python3 scripts/build_deb.py
```

Before publishing, check the package metadata and desktop launchers, install the package on a suitable test machine, and verify compact/full launch, administrator authentication, background restore, and removal behavior. Keep generated packages, transfer samples, local settings, and private logs out of source control.
