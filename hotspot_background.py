"""System-tray lifecycle for Z Hotspot's unprivileged desktop application."""

from pathlib import Path

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gio, GLib, Gtk


def _has_status_notifier_watcher():
    """Do not hide the only window unless the desktop can display its tray icon."""
    try:
        bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        result = bus.call_sync("org.freedesktop.DBus", "/org/freedesktop/DBus",
                               "org.freedesktop.DBus", "NameHasOwner",
                               GLib.Variant("(s)", ("org.kde.StatusNotifierWatcher",)),
                               GLib.VariantType.new("(b)"), Gio.DBusCallFlags.NONE,
                               1000, None)
        return result.unpack()[0] is True
    except (GLib.Error, AttributeError, TypeError):
        return False


class _IndicatorTray:
    def __init__(self, menu):
        gi.require_version("AyatanaAppIndicator3", "0.1")
        from gi.repository import AyatanaAppIndicator3 as Indicator
        self._api = Indicator
        icon = str(Path(__file__).resolve().with_name("icon.svg"))
        self._indicator = Indicator.Indicator.new(
            "z-hotspot", icon, Indicator.IndicatorCategory.APPLICATION_STATUS)
        self._indicator.set_title("Z Hotspot")
        self._indicator.set_menu(menu)

    def show(self):
        self._indicator.set_status(self._api.IndicatorStatus.ACTIVE)

    def hide(self):
        self._indicator.set_status(self._api.IndicatorStatus.PASSIVE)

    def set_status(self, text):
        self._indicator.set_title("Z Hotspot · " + text)


class BackgroundController:
    """One tray icon and one background poller shared by both window sizes.

    Creating the controller or entering background never requests administrator
    authentication. Start/stop remain unavailable until the user unlocks the app.
    """

    def __init__(self, application, get_bridge, get_windows, show_mode, quit_app,
                 *, tray_factory=None, watcher_available=None):
        self.application = application
        self._get_bridge = get_bridge
        self._get_windows = get_windows
        self._show_mode = show_mode
        self._quit_app = quit_app
        self._tray_factory = tray_factory or _IndicatorTray
        self._watcher_available = watcher_available or _has_status_notifier_watcher
        self._tray = None
        self.enabled = False
        self._held = False
        self._closed = False
        self._request_pending = False
        self._timer = None
        self._snapshot = {}
        self._setting_toggle = False
        self.menu = Gtk.Menu()
        self.status_item = Gtk.MenuItem(label="Controls locked")
        self.status_item.set_sensitive(False)
        self.menu.append(self.status_item)
        self.menu.append(Gtk.SeparatorMenuItem())
        for label, mode in (("Show compact view", "compact"), ("Show full view", "full")):
            item = Gtk.MenuItem(label=label)
            item.connect("activate", lambda _item, target=mode: self._show_mode(target))
            self.menu.append(item)
        self.menu.append(Gtk.SeparatorMenuItem())
        self.start_item = Gtk.MenuItem(label="Start hotspot")
        self.start_item.connect("activate", lambda *_: self._perform("start"))
        self.menu.append(self.start_item)
        self.stop_item = Gtk.MenuItem(label="Stop hotspot")
        self.stop_item.connect("activate", lambda *_: self._perform("stop"))
        self.menu.append(self.stop_item)
        self.keep_item = Gtk.CheckMenuItem(label="Keep running when window closes")
        self.keep_item.connect("toggled", self._keep_toggled)
        self.menu.append(self.keep_item)
        self.menu.append(Gtk.SeparatorMenuItem())
        quit_item = Gtk.MenuItem(label="Quit Z Hotspot")
        quit_item.connect("activate", lambda *_: self._quit_app())
        self.menu.append(quit_item)
        self.menu.show_all()
        self.update()

    def _windows(self):
        return list(self._get_windows().values())

    def _visible_window(self):
        return next((window for window in self._windows() if window.get_visible()), None)

    def _ready(self):
        bridge = self._get_bridge()
        return bool(bridge and bridge.connected and not self._request_pending
                    and not any(getattr(window, flag, False)
                                for window in self._windows()
                                for flag in ("_busy", "_connecting", "_refresh_pending")))

    def _set_keep(self, enabled):
        self._setting_toggle = True
        self.keep_item.set_active(enabled)
        self._setting_toggle = False

    def _show_error(self, message):
        parent = self._visible_window()
        dialog = Gtk.MessageDialog(transient_for=parent, modal=True,
                                   message_type=Gtk.MessageType.INFO,
                                   buttons=Gtk.ButtonsType.OK,
                                   text="Background mode is unavailable")
        dialog.format_secondary_text(message)
        dialog.run()
        dialog.destroy()

    def enter(self):
        """Enable close-to-tray and hide only after a usable tray is present."""
        if self._closed:
            return False
        if not self._watcher_available():
            self._show_error("Your desktop is not displaying system tray icons. "
                             "Keep this window open, or enable the system tray applet and try again.")
            return False
        try:
            if self._tray is None:
                self._tray = self._tray_factory(self.menu)
            self._tray.show()
        except (ImportError, ValueError, GLib.Error):
            self._show_error("Install the desktop integration package "
                             "gir1.2-ayatanaappindicator3-0.1, then open Z Hotspot again.")
            return False
        self.enabled = True
        self._set_keep(True)
        if not self._held:
            self.application.hold()
            self._held = True
        if self._timer is None:
            self._timer = GLib.timeout_add_seconds(4, self._tick)
        self.update()
        for window in self._windows():
            window.hide()
        return True

    def _keep_toggled(self, *_):
        if self._setting_toggle or self._closed:
            return
        if self.keep_item.get_active():
            if not self.enter():
                self._set_keep(False)
        else:
            self.disable()

    def disable(self):
        """Turning off close-to-tray first restores a window if all are hidden."""
        if self._closed:
            return
        if not self._visible_window():
            self._show_mode("compact")
        self.enabled = False
        self._set_keep(False)
        if self._tray:
            self._tray.hide()
        if self._timer is not None:
            GLib.source_remove(self._timer)
            self._timer = None
        if self._held:
            self.application.release()
            self._held = False

    def observe(self, payload):
        if isinstance(payload, dict) and isinstance(payload.get("snapshot"), dict):
            self._snapshot = payload["snapshot"]
        self.update()

    def update(self):
        if self._closed:
            return
        bridge = self._get_bridge()
        connected = bool(bridge and bridge.connected)
        running = bool(self._snapshot.get("running"))
        if not connected:
            status = "Controls locked · open app to unlock"
        elif running:
            status = "Online · %s devices" % len(self._snapshot.get("clients", []))
        else:
            status = str(self._snapshot.get("status") or "Offline")
        self.status_item.set_label(status)
        ready = self._ready()
        self.start_item.set_sensitive(ready and not running)
        self.stop_item.set_sensitive(ready and (running or str(self._snapshot.get("status", "")).lower() == "needs restart"))
        if self._tray:
            self._tray.set_status(status)

    def _tick(self):
        if self._closed or not self.enabled:
            self._timer = None
            return False
        # If the panel disappears, restore the app instead of stranding it.
        if not self._watcher_available():
            self._timer = None
            self.disable()
            self._show_error("The system tray stopped displaying icons. Z Hotspot has reopened so you can still control it.")
            return False
        self.update()
        if self._visible_window() is None and self._ready():
            self._request("snapshot")
        return True

    def _perform(self, action):
        if self._closed or not self._ready():
            return
        if action not in ("start", "stop"):
            return
        self._request(action)

    def _request(self, action):
        bridge = self._get_bridge()
        if not bridge or not bridge.connected or self._request_pending:
            return
        self._request_pending = True
        self.update()

        def completed(result, error):
            self._request_pending = False
            if self._closed:
                return
            if error:
                self.status_item.set_label("Could not refresh · open app to check")
                # Action failures need a visible explanation; background polling
                # failures stay quiet and retry on the next scheduled sample.
                if action != "snapshot":
                    self._show_mode("compact")
                    window = self._visible_window()
                    if window and hasattr(window, "_message"):
                        window._message("The hotspot action failed. Refresh the app to check its status.", error=True)
            else:
                self.observe(result)
                # The bridge observer refreshes hidden windows. Tray actions
                # also need to refresh a window that is currently on screen.
                for window in self._windows():
                    if window.get_visible() and not window._closing:
                        window._receive(result)
            self.update()

        try:
            bridge.request(action, {}, completed)
        except Exception:
            completed(None, True)

    def close(self):
        if self._closed:
            return
        self._closed = True
        self.enabled = False
        if self._timer is not None:
            GLib.source_remove(self._timer)
            self._timer = None
        if self._tray:
            self._tray.hide()
        if self._held:
            self.application.release()
            self._held = False

