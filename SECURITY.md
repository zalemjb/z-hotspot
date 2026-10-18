# Security policy

## Supported version

Security fixes target the latest published release. This is an early project; response times and hardware coverage are not guaranteed.

## Reporting a vulnerability

Please use the repository's [private vulnerability reporting page](https://github.com/zalemjb/z-hotspot/security/advisories/new) if it is available. Include the affected version, expected and actual behavior, and minimal reproduction steps. Use artificial files and test credentials wherever possible.

If private reporting is unavailable, open a minimal issue asking the maintainer to enable it. Do not post exploit details, passwords, QR codes, transfer tokens, sensitive logs, or personal files in a public issue.

## Boundaries worth reviewing

- The GTK application runs as the current user; a separate root helper accepts a limited set of network-control requests.
- File sharing is opt-in, offers explicitly selected regular files, and accepts uploads only into a chosen directory when receiving is enabled.
- The sharing server listens on the hotspot address and uses a fresh random URL token. The URL is a bearer secret, not a user login.
- Local transfers use HTTP. Do not treat file sharing as an end-to-end encrypted service.
- MAC blocking applies to the reported MAC address. It does not identify a person and cannot prevent a device from changing its address.
- Wi-Fi join QR codes contain the saved Wi-Fi password.

These design choices are documented behavior, not a claim that the software has undergone an independent security audit.
