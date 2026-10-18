# Third-party notices

The MIT license in this repository covers original Z Hotspot code. It does not replace the license of the bundled `create_ap` helper or separately installed dependencies.

## create_ap

Source: [linux-wifi-hotspot v5.0.0](https://github.com/lakinduakash/linux-wifi-hotspot/releases/tag/v5.0.0)

Original package SHA-256: `efb70cd67293d6da4ab935e4639eb05a3459f3f37ec1484bd701b0ff4c30330b`

Local changes:

- Preserve whitespace and backslashes when reading credentials from standard input.
- Load a root-owned `deny_mac_file` before AP activation through `MINT_HOTSPOT_DENY_FILE`.

The original copyright spelling is preserved below.

```text
Copyright: 2013 oblique
           2021 lakinduaksh

BSD 2-Clause License

Redistribution and use in source and binary forms, with or without
modification, are permitted provided that the following conditions are met:

* Redistributions of source code must retain the above copyright notice, this
  list of conditions and the following disclaimer.

* Redistributions in binary form must reproduce the above copyright notice,
  this list of conditions and the following disclaimer in the documentation
  and/or other materials provided with the distribution.

THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE
DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE LIABLE
FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL
DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR
SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER
CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY,
OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE
OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
```

## System dependencies

Z Hotspot uses the Python standard library and separately installed packages including GTK/PyGObject, python3-qrcode, Ayatana AppIndicator, NetworkManager, hostapd, dnsmasq, iw, iptables, and polkit/pkexec. These components retain their own licenses and copyright notices. They are installed as system dependencies rather than relicensed as part of Z Hotspot.
