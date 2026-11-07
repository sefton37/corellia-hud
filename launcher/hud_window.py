#!/usr/bin/env python3
"""Frameless, transparent GTK3 + WebKit2 window showing the HUD page, plus ONE reusable detail window.

The HUD column is display-only and never navigates: every stat on the page is a link with
target="corellia-hud-detail", WebKit reports it as a NEW_WINDOW_ACTION, and detail_policy.allowed() decides
whether it opens (or re-targets) the single DetailWindow. Anything else is ignored.

The GNOME Shell extension (extension/corellia-hud-pin@corellia.local) finds the column by WM_CLASS / title
and pins it behind everything; it finds the detail window by its own WM_CLASS / title and places it beside
the column, above other windows.
"""
import os
import sys

import gi

gi.require_version('Gtk', '3.0')
gi.require_version('Gdk', '3.0')  # else Gdk resolves to 4.0 (imported before Gtk) and clashes
gi.require_version('WebKit2', '4.1')
from gi.repository import GLib, Gdk, Gtk, WebKit2  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import detail_policy  # noqa: E402

WM_CLASS = "corellia-hud"
TITLE = "Corellia HUD"
WM_CLASS_DETAIL = "corellia-hud-detail"
TITLE_DETAIL = "Corellia detail"
URL = "http://127.0.0.1:8670/"
WIDTH = 380
HEIGHT = 1000  # the extension resizes to the monitor work area
DETAIL_WIDTH = 560
DETAIL_MAX_HEIGHT = 900
RETRY_SECONDS = 5  # the aggregator may not be up yet at login
CLOSE_HANDLER = "hudDetail"  # detail.js posts "close" here (window.close() is refused for a window WebKit did not script-open)


def _quiet_settings(view):
    settings = view.get_settings()
    settings.set_enable_developer_extras(False)
    settings.set_enable_javascript(True)
    settings.set_enable_write_console_messages_to_stdout(False)
    view.connect("context-menu", lambda *a: True)  # no right-click menu


class DetailWindow(Gtk.Window):
    """The one floating, frameless detail window. Not pinned, not sticky, not kept below: it takes focus
    (so Esc works) and the extension raises it beside the column. Closing it never quits the process."""

    def __init__(self, on_closed):
        super().__init__(title=TITLE_DETAIL)
        self.set_wmclass(WM_CLASS_DETAIL, WM_CLASS_DETAIL)
        self.set_role(WM_CLASS_DETAIL)
        self.set_decorated(False)
        self.set_resizable(True)
        self.set_skip_taskbar_hint(True)
        self.set_skip_pager_hint(True)
        self.set_accept_focus(True)
        self.set_default_size(DETAIL_WIDTH, self._height())

        content = WebKit2.UserContentManager()
        content.register_script_message_handler(CLOSE_HANDLER)
        content.connect("script-message-received::" + CLOSE_HANDLER, lambda *a: self.destroy())
        self.view = WebKit2.WebView.new_with_user_content_manager(content)
        self.view.set_background_color(Gdk.RGBA(0.07, 0.04, 0.13, 1))  # no white flash before the page paints
        _quiet_settings(self.view)
        self.view.connect("decide-policy", self._only_detail_urls)
        self.add(self.view)
        self.connect("key-press-event", self._on_key)
        self.connect("destroy", lambda *a: on_closed())

    @staticmethod
    def _height():
        monitor = Gdk.Display.get_default().get_primary_monitor()
        area = monitor.get_workarea() if monitor else None
        return min((area.height - 24) if area else DETAIL_MAX_HEIGHT, DETAIL_MAX_HEIGHT)

    def show_uri(self, uri):
        self.view.load_uri(uri)
        self.show_all()
        self.present()

    def _on_key(self, _widget, event):
        if event.keyval == Gdk.KEY_Escape:
            self.destroy()
            return True
        return False

    def _only_detail_urls(self, view, decision, decision_type):
        # The window only ever shows the aggregator's detail page; a second window request is not honoured.
        if decision_type == WebKit2.PolicyDecisionType.NEW_WINDOW_ACTION:
            decision.ignore()
            return True
        if decision_type == WebKit2.PolicyDecisionType.NAVIGATION_ACTION:
            uri = decision.get_navigation_action().get_request().get_uri()
            if not detail_policy.allowed(uri):
                decision.ignore()
                return True
        return False


class HudWindow(Gtk.Window):
    def __init__(self):
        super().__init__(title=TITLE)
        self.detail = None
        self.set_wmclass(WM_CLASS, WM_CLASS)
        self.set_role(WM_CLASS)
        self.set_decorated(False)
        self.set_resizable(True)
        self.set_skip_taskbar_hint(True)
        self.set_skip_pager_hint(True)
        self.set_keep_below(True)
        self.set_accept_focus(False)
        self.stick()
        self.set_default_size(WIDTH, HEIGHT)
        visual = self.get_screen().get_rgba_visual()
        if visual is not None:
            self.set_visual(visual)
        self.set_app_paintable(True)

        self.view = WebKit2.WebView()
        self.view.set_background_color(Gdk.RGBA(0, 0, 0, 0))
        _quiet_settings(self.view)
        self.view.connect("load-failed", self._retry)
        self.view.connect("decide-policy", self._only_hud_url)
        self.add(self.view)
        self.view.load_uri(URL)
        self.connect("destroy", Gtk.main_quit)

    def _retry(self, *_):
        GLib.timeout_add_seconds(RETRY_SECONDS, lambda: self.view.load_uri(URL) or False)
        return True

    def _open_detail(self, uri):
        """Open the detail window, or re-target the one that is already open."""
        if self.detail is None:
            # On Wayland GTK3 derives the window's app id from the program name when the window is mapped
            # (set_wmclass is ignored), so the name is swapped just for this show: the detail window gets
            # app id WM_CLASS_DETAIL and the extension does not pin it as a second column.
            GLib.set_prgname(WM_CLASS_DETAIL)
            try:
                self.detail = DetailWindow(lambda: setattr(self, "detail", None))
                self.detail.show_uri(uri)
            finally:
                GLib.set_prgname(WM_CLASS)
        else:
            self.detail.show_uri(uri)

    def _only_hud_url(self, view, decision, decision_type):
        if decision_type == WebKit2.PolicyDecisionType.NEW_WINDOW_ACTION:
            uri = decision.get_navigation_action().get_request().get_uri()
            decision.ignore()
            if detail_policy.allowed(uri):
                self._open_detail(uri)
            return True
        if decision_type == WebKit2.PolicyDecisionType.NAVIGATION_ACTION:
            uri = decision.get_navigation_action().get_request().get_uri()
            if uri == URL:
                return False                       # the initial load and the retry
            decision.ignore()                      # the pinned column never navigates
            if detail_policy.allowed(uri):         # e.g. a link without a target: still a detail request
                self._open_detail(uri)
            return True
        return False


def main():
    GLib.set_prgname(WM_CLASS)
    GLib.set_application_name(TITLE)
    win = HudWindow()
    win.show_all()
    Gtk.main()


if __name__ == "__main__":
    main()
