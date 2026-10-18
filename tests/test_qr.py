"""QR checks use fictional credentials only and never persist QR images."""
import ctypes
import ctypes.util
from io import BytesIO
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from hotspot_qr import qr_png_bytes, wifi_payload
from PIL import Image


def decode_png(png):
    """Decode the rendered image with the installed, independent zbar library."""
    library = ctypes.util.find_library("zbar")
    if not library:
        raise unittest.SkipTest("libzbar is not installed")
    lib = ctypes.CDLL(library)
    for name in ("zbar_image_scanner_create", "zbar_image_create"):
        getattr(lib, name).restype = ctypes.c_void_p
    lib.zbar_image_scanner_set_config.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_int]
    lib.zbar_image_set_format.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
    lib.zbar_image_set_size.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_uint]
    lib.zbar_image_set_data.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ulong, ctypes.c_void_p]
    lib.zbar_scan_image.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    lib.zbar_image_first_symbol.argtypes = [ctypes.c_void_p]
    lib.zbar_image_first_symbol.restype = ctypes.c_void_p
    lib.zbar_symbol_get_data.argtypes = [ctypes.c_void_p]
    lib.zbar_symbol_get_data.restype = ctypes.c_void_p
    lib.zbar_symbol_get_data_length.argtypes = [ctypes.c_void_p]
    lib.zbar_symbol_get_data_length.restype = ctypes.c_uint
    lib.zbar_image_destroy.argtypes = [ctypes.c_void_p]
    lib.zbar_image_scanner_destroy.argtypes = [ctypes.c_void_p]
    scanner, image = lib.zbar_image_scanner_create(), lib.zbar_image_create()
    try:
        lib.zbar_image_scanner_set_config(scanner, 0, 0, 0)
        lib.zbar_image_scanner_set_config(scanner, 64, 0, 1)
        gray = Image.open(BytesIO(png)).convert("L")
        pixels = ctypes.create_string_buffer(gray.tobytes())
        lib.zbar_image_set_format(image, int.from_bytes(b"Y800", "little"))
        lib.zbar_image_set_size(image, gray.width, gray.height)
        lib.zbar_image_set_data(image, pixels, len(gray.tobytes()), None)
        if lib.zbar_scan_image(scanner, image) < 1:
            raise AssertionError("The generated QR image could not be decoded")
        symbol = lib.zbar_image_first_symbol(image)
        return ctypes.string_at(lib.zbar_symbol_get_data(symbol),
                                lib.zbar_symbol_get_data_length(symbol)).decode("utf-8")
    finally:
        lib.zbar_image_destroy(image)
        lib.zbar_image_scanner_destroy(scanner)


def parse_wifi(payload):
    assert payload.startswith("WIFI:")
    fields, field, escaped = [], "", False
    for char in payload[5:]:
        if escaped:
            field += char
            escaped = False
        elif char == "\\":
            escaped = True
        elif char == ";":
            if field:
                fields.append(field)
            field = ""
        else:
            field += char
    return dict(field.split(":", 1) for field in fields)


class QRTests(unittest.TestCase):
    def test_exact_escape(self):
        self.assertEqual(wifi_payload('Mint;,:"\\', 'Demo;,:"\\123', True),
                         'WIFI:T:WPA;S:Mint\\;\\,\\:\\"\\\\;P:Demo\\;\\,\\:\\"\\\\123;H:true;;')

    def test_wifi_roundtrip_ascii(self):
        name, password = 'Demo; network', 'Demo;,:"\\2026'
        payload = wifi_payload(name, password)
        self.assertEqual(parse_wifi(decode_png(qr_png_bytes(payload))),
                         {"T": "WPA", "S": name, "P": password, "H": "false"})

    def test_wifi_roundtrip_unicode_hidden(self):
        name, password = 'شبكة;,:"\\', "DemoPassword2026"
        payload = wifi_payload(name, password, hidden=True)
        self.assertEqual(decode_png(qr_png_bytes(payload)), payload)
        self.assertEqual(parse_wifi(payload)["S"], name)
        self.assertEqual(parse_wifi(payload)["H"], "true")

    def test_url_roundtrip(self):
        payload = "http://192.168.12.1:8765/demo-preview-token/"
        self.assertEqual(decode_png(qr_png_bytes(payload)), payload)

    def test_png_is_crisp_and_has_quiet_zone(self):
        with Image.open(BytesIO(qr_png_bytes("http://192.168.12.1:8765/demo/"))) as image:
            image = image.convert("RGB")
            self.assertEqual(image.width, image.height)
            self.assertLessEqual(image.width, 480)
            self.assertGreaterEqual(image.width, 250)
            self.assertEqual(set(image.getdata()), {(0, 0, 0), (255, 255, 255)})
            self.assertEqual(set(image.crop((0, 0, image.width, 8)).getdata()), {(255, 255, 255)})

    def test_invalid_network_details(self):
        for args in (("", "DemoPassword2026"), ("x" * 33, "DemoPassword2026"),
                     ("ش" * 17, "DemoPassword2026"), ("Demo", "short"),
                     ("Demo\nname", "DemoPassword2026"), ("Demo", "pass\nword")):
            with self.subTest(kind=len(str(args[0]))):
                with self.assertRaises(ValueError):
                    wifi_payload(*args)

    def test_invalid_qr_content(self):
        for value in (None, "", "x" * 2049):
            with self.assertRaises(ValueError):
                qr_png_bytes(value)


if __name__ == "__main__":
    unittest.main()
