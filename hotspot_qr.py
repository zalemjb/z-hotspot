"""On-demand QR codes for joining this hotspot and opening a local file share.

Nothing is written to disk until the user chooses Save PNG. Wi-Fi credentials
are encoded in the image only and are never put in labels or the clipboard.
"""

from io import BytesIO
import os


def _wifi_escape(value):
    return "".join("\\" + char if char in '\\;,:"' else char for char in value)


def wifi_payload(ssid, password, hidden=False):
    """Return the standard WPA/WPA2 Wi-Fi QR payload, including UTF-8 names."""
    if not isinstance(ssid, str) or not ssid or len(ssid.encode("utf-8")) > 32:
        raise ValueError("The network name must contain 1 to 32 UTF-8 bytes.")
    if not isinstance(password, str) or not 8 <= len(password) <= 63:
        raise ValueError("The Wi-Fi password must contain 8 to 63 characters.")
    if any(ord(char) < 32 or ord(char) == 127 for char in ssid + password):
        raise ValueError("The network details must not contain control characters.")
    return (f"WIFI:T:WPA;S:{_wifi_escape(ssid)};P:{_wifi_escape(password)};"
            f"H:{'true' if hidden else 'false'};;")


def qr_png_bytes(payload):
    """Render an opaque black-and-white PNG with a four-module quiet zone."""
    if not isinstance(payload, str) or not payload or len(payload.encode("utf-8")) > 2048:
        raise ValueError("The QR content is empty or too long.")
    try:
        import qrcode
        from qrcode.constants import ERROR_CORRECT_M
    except ImportError as exc:
        raise RuntimeError("Install python3-qrcode to display QR codes.") from exc
    qr = qrcode.QRCode(error_correction=ERROR_CORRECT_M, box_size=8, border=4)
    qr.add_data(payload.encode("utf-8"), optimize=0)
    qr.make(fit=True)
    # Integer-sized modules avoid blur when phone cameras read the code.
    qr.box_size = max(2, min(10, 480 // (qr.modules_count + 8)))
    output = BytesIO()
    qr.make_image(fill_color="black", back_color="white").save(output, format="PNG")
    return output.getvalue()


def show_qr_dialog(parent, title, payload, subtitle="", sensitive=False):
    """Show a modal QR; sensitive payloads have no clipboard action.

    The caller must invoke this only after a user action. Generation is kept in
    memory, and Save PNG opens the normal file picker before writing a file.
    """
    import gi
    gi.require_version("Gtk", "3.0")
    gi.require_version("GdkPixbuf", "2.0")
    from gi.repository import Gtk, Gdk, GdkPixbuf

    try:
        png = qr_png_bytes(payload)
        loader = GdkPixbuf.PixbufLoader.new_with_type("png")
        loader.write(png)
        loader.close()
        pixbuf = loader.get_pixbuf()
    except Exception:
        # Never surface an encoder exception that could contain a credential.
        message = Gtk.MessageDialog(transient_for=parent, modal=True,
                                    message_type=Gtk.MessageType.ERROR,
                                    buttons=Gtk.ButtonsType.CLOSE,
                                    text="Could not create the QR code")
        message.format_secondary_text("Check the saved network details and that python3-qrcode is installed.")
        message.run()
        message.destroy()
        return

    dialog = Gtk.Dialog(title=title, transient_for=parent, modal=True)
    dialog.set_resizable(False)
    dialog.set_position(Gtk.WindowPosition.CENTER_ON_PARENT)
    dialog.add_button("Close", Gtk.ResponseType.CLOSE)
    save_response, copy_response = 101, 102
    dialog.add_button("Save PNG…", save_response)
    if not sensitive:
        dialog.add_button("Copy link", copy_response)
    content = dialog.get_content_area()
    content.set_spacing(12)
    content.set_border_width(20)
    caption = Gtk.Label(label=subtitle)
    caption.set_line_wrap(True)
    caption.set_max_width_chars(52)
    caption.set_justify(Gtk.Justification.CENTER)
    content.pack_start(caption, False, False, 0)
    image = Gtk.Image.new_from_pixbuf(pixbuf)
    image.set_halign(Gtk.Align.CENTER)
    content.pack_start(image, True, True, 0)
    note = Gtk.Label(label="Scan with your phone's Camera or QR scanner.")
    note.set_line_wrap(True)
    note.set_justify(Gtk.Justification.CENTER)
    content.pack_start(note, False, False, 0)
    feedback = Gtk.Label()
    feedback.set_line_wrap(True)
    feedback.set_max_width_chars(52)
    content.pack_start(feedback, False, False, 0)
    dialog.show_all()
    try:
        while True:
            response = dialog.run()
            if response == copy_response and not sensitive:
                Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD).set_text(payload, -1)
                feedback.set_text("Link copied.")
            elif response == save_response:
                chooser = Gtk.FileChooserDialog(title="Save QR image", transient_for=dialog,
                                                action=Gtk.FileChooserAction.SAVE)
                chooser.add_buttons("Cancel", Gtk.ResponseType.CANCEL,
                                    "Save", Gtk.ResponseType.ACCEPT)
                chooser.set_do_overwrite_confirmation(True)
                chooser.set_current_name("hotspot-join-qr.png" if sensitive else "hotspot-files-qr.png")
                file_filter = Gtk.FileFilter()
                file_filter.set_name("PNG image")
                file_filter.add_pattern("*.png")
                chooser.add_filter(file_filter)
                result = chooser.run()
                target = chooser.get_filename() if result == Gtk.ResponseType.ACCEPT else None
                chooser.destroy()
                if target:
                    try:
                        descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                        with os.fdopen(descriptor, "wb") as output:
                            os.fchmod(output.fileno(), 0o600)
                            output.write(png)
                        feedback.set_text("QR image saved.")
                    except OSError:
                        feedback.set_text("Could not save the image. Choose a writable folder.")
            else:
                break
    finally:
        dialog.destroy()
