# Z Hotspot

**My first project · Vibe Coding · v0.1**

Your connection. Your control.

My first public project, vibe-coded with AI assistance: a native Linux desktop app for sharing your connection, managing connected devices, and moving files over your local hotspot. Built with Python and GTK 3, with a compact dashboard and a full control center.

[العربية](README.ar.md) · [Download](https://github.com/zalemjb/z-hotspot/releases/latest) · [Report a bug](https://github.com/zalemjb/z-hotspot/issues/new?template=bug_report.yml)

![Z Hotspot overview](docs/images/overview.png)

## What it does

- **Quick controls:** start, stop, refresh, and scan a QR code to join the Wi-Fi.
- **Live overview:** network name, device count, source signal, incoming traffic, hotspot traffic, and connection details.
- **Device management:** IP and MAC addresses, reported names, signal, transfer rates, connection duration, disconnect, block, and unblock.
- **Local file sharing:** choose files to offer, scan a transfer QR, and download through a browser. Optional uploads go to a folder you choose.
- **Network settings:** change the name and password, generate a password, choose an Internet source, hide the SSID, and isolate clients.
- **Background mode:** keep the app in the system tray and reopen its compact or full view when needed.

| Compact dashboard | Share files |
| --- | --- |
| ![Compact dashboard](docs/images/compact.png) | ![File sharing](docs/images/files.png) |

Screenshots use demo data. The desktop interface is in English.

## Install on Linux Mint

Download **`z-hotspot_0.1.0_all.deb`** from [Releases](https://github.com/zalemjb/z-hotspot/releases/latest), open it, and install it with your package installer. Then open **Z Hotspot** from the application menu. Use **Z Hotspot — Full view** for the larger interface. The same release also provides **`Z-Hotspot-v0.1.zip`** for running from source.

To install from a terminal in the download folder:

```sh
sudo apt install ./z-hotspot_0.1.0_all.deb
```

The package declares its dependencies, including Python 3, GTK 3/PyGObject, QR support, Ayatana AppIndicator, NetworkManager, hostapd, dnsmasq, iw, iptables, and pkexec.

Click **Unlock controls** when you want to manage the network. Linux shows its normal administrator authentication window. The graphical app runs as your normal user; a separate helper performs the network changes.

### Wi-Fi requirements

Z Hotspot has been tested on **Linux Mint 22.3** with a Wi-Fi adapter that supports a managed connection and an access point at the same time. For that single-adapter setup, connect the computer to **2.4 GHz Wi-Fi** first; the hotspot follows the same channel. Ethernet can also be selected as the Internet source. If the radio is still connected to Wi-Fi, that connection must use 2.4 GHz.

Adapter and driver support vary. This release does not promise 5 GHz operation, support for every adapter, or testing on other Linux distributions.

## Everyday use

1. Open **Z Hotspot** and unlock its controls.
2. Set your network name, password, and Internet source in **Network settings**.
3. Start the hotspot. On a phone, scan **Join QR**, or join through its Wi-Fi settings.
4. Use **Full view** for Overview, Connected devices, Share files, and Network settings.
5. Select **Background** to keep the app in the system tray. Use its tray menu to reopen a view or control the hotspot.

Quitting the app stops its file-sharing server but leaves a running hotspot on. Select **Stop** before quitting when you also want to stop broadcasting. Background mode keeps the app running; it does not enable automatic launch at login or start sharing after a reboot.

### Send files to an iPhone, Android phone, or laptop

1. Connect the receiving device to the hotspot.
2. Open **Full view → Share files**, choose your files, and select **Start sharing**.
3. Scan **Transfer QR** on the phone, or copy the link into a browser on the other computer.
4. Open a file and confirm its download. On an iPhone, use Safari Downloads or **Files → Downloads** to find it. A photo or video can then be saved to Photos through the Share menu.
5. To receive files on this computer, enable receiving, choose a destination folder, and use the upload chooser on the transfer page.

The receiving person chooses what to download. Z Hotspot cannot silently save a file into another device. Transfers use the local network; they do not need Internet access once the hotspot is active. Keep the app running while a transfer is in progress.

Only explicitly selected files are offered. Receiving is off until enabled; uploads use unique filenames and do not overwrite existing files. The upload limit is **2 GiB per file**. Each sharing session creates a fresh private link, and the server listens only on the hotspot address. Treat the link as access to the files you selected. Local sharing uses HTTP, so use it with people you trust on your hotspot.

**Verification:** automated tests cover local HTTP downloads and uploads, matching file hashes, simultaneous transfers, and simulated Safari/Chrome browser profiles. These are not tests on physical iPhones or Samsung phones. See [TESTING.md](TESTING.md) for the scope and commands.

## Reading the numbers

- **Incoming from the Internet source** includes this computer's traffic and forwarded hotspot traffic. It is a current transfer rate, not a speed test.
- **Hotspot download/upload** are from the connected devices' perspective.
- **Wi-Fi link rates** are negotiated radio rates. Estimated link use is not the percentage of your Internet plan.
- **Device names** come from DHCP reports. They are not verified identities or phone models. Private/randomized MAC addresses can make the same device appear as a new device.
- **Disconnect** allows a device to reconnect. **Block** denies its current MAC address until you unblock it; a changed MAC address is a different identity.

## Run from source

Install the system dependencies, then keep the Python modules and bundled `create_ap` helper together:

```sh
sudo apt install python3 python3-gi python3-qrcode gir1.2-gtk-3.0 \
  gir1.2-ayatanaappindicator3-0.1 network-manager hostapd dnsmasq-base iw iptables pkexec
```

From the extracted source directory:

```sh
python3 hotspot_gui.py           # compact view
python3 hotspot_gui.py --full    # full view
python3 hotspot_gui.py --demo    # sample data; no network changes
```

Use Python with the system GTK bindings available. Do not run the entire GUI with `sudo`; the app requests the network privileges it needs.

Build the Debian package with:

```sh
python3 scripts/build_deb.py
```

See [CONTRIBUTING.md](CONTRIBUTING.md) for development and [CHANGELOG.md](CHANGELOG.md) for release notes.

## Privacy and security

The app does not need an online account or a cloud service. Settings and blocked MAC addresses are stored in a root-only local settings file. Wi-Fi QR codes contain the network password: show them only to people you want to connect. A QR stays in memory unless you explicitly save it.

For reporting a security issue, see [SECURITY.md](SECURITY.md). Before sharing logs or screenshots, remove passwords, transfer links, QR codes, MAC/IP addresses, and filenames you do not want to publish.

## License and credits

Original Z Hotspot code is [MIT licensed](LICENSE), copyright © 2026 [zalem.j (zalemjb)](https://github.com/zalemjb).

The bundled `create_ap` helper comes from [linux-wifi-hotspot v5.0.0](https://github.com/lakinduakash/linux-wifi-hotspot/releases/tag/v5.0.0) and remains under the **BSD 2-Clause** license. Its original notices and local changes are recorded in [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md). The project uses separately installed open-source system libraries and tools; their licenses remain their own.

Made by **@zalem.j** · GitHub: [@zalemjb](https://github.com/zalemjb)
