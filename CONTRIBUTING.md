# Contributing

Thank you for helping improve Z Hotspot. Small bug reports, adapter compatibility details, documentation fixes, and focused patches are welcome.

## Before making a change

Search [existing issues](https://github.com/zalemjb/z-hotspot/issues) first. For a larger change, open an issue describing the problem and expected behavior so implementation can be discussed before you invest significant time.

Z Hotspot targets Linux desktops. Compatibility reports from different distributions are welcome: include your distribution/version, desktop environment, relevant package names, Wi-Fi adapter and driver, whether the Internet source is Wi-Fi or Ethernet, and whether the radio is on 2.4 GHz. Do not publish Wi-Fi passwords, transfer links, private QR codes, personal filenames, or unredacted network addresses.

## Development

1. Fork the repository and create a branch for one focused change.
2. Install the system dependencies listed in the README. GTK bindings must be available to the Python interpreter you use.
3. Run `python3 hotspot_gui.py --demo` to inspect the interface without changing your network.
4. Run the relevant checks in [TESTING.md](TESTING.md).
5. Open a pull request that explains the problem, resulting behavior, and how you verified it. Include demo screenshots for visible UI changes.

Keep the normal desktop process unprivileged. Network operations belong in the existing privileged helper and must use validated arguments, avoid shell interpolation, and avoid logging secrets. File sharing must remain opt-in and limited to explicitly selected files and a chosen receiving folder.

Do not use automated tests to change the host network. Hardware tests should be run intentionally on a setup where a temporary disconnect is acceptable. Use documentation to distinguish hardware measurements from mock or loopback tests.

## Licensing

By submitting a contribution, you agree to license original contributions under the project's MIT license. Preserve the separate BSD-2-Clause notices when changing the bundled `create_ap` helper. If you add a dependency or third-party asset, record its source and license.
