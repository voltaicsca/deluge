#
# systemtray.py
#
# Copyright (C) 2007, 2008 Andrew Resch <andrewresch@gmail.com>
#
# Deluge is free software.
#
# You may redistribute it and/or modify it under the terms of the
# GNU General Public License, as published by the Free Software
# Foundation; either version 3 of the License, or (at your option)
# any later version.
#
# deluge is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.
# See the GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with deluge.    If not, write to:
# 	The Free Software Foundation, Inc.,
# 	51 Franklin Street, Fifth Floor
# 	Boston, MA  02110-1301, USA.
#
#    In addition, as a special exception, the copyright holders give
#    permission to link the code of portions of this program with the OpenSSL
#    library.
#    You must obey the GNU General Public License in all respects for all of
#    the code used other than OpenSSL. If you modify file(s) with this
#    exception, you may extend this exception to your version of the file(s),
#    but you are not obligated to do so. If you do not wish to do so, delete
#    this exception statement from your version. If you delete this exception
#    statement from all source files in the program, then also delete it here.
#
#

try:
    import appindicator
except ImportError:
    appindicator = None

import os
import gtk
import logging

import deluge.component as component
from deluge.ui.client import client
import deluge.common
from deluge.configmanager import ConfigManager
import common

log = logging.getLogger(__name__)

class SystemTray(component.Component):
    def __init__(self):
        component.Component.__init__(self, "SystemTray", interval=4)
        self.window = component.get("MainWindow")
        self.config = ConfigManager("gtkui.conf")
        # List of widgets that need to be hidden when not connected to a host
        self.hide_widget_list = [
            "menuitem_add_torrent",
            "menuitem_pause_all",
            "menuitem_resume_all",
            "menuitem_download_limit",
            "menuitem_upload_limit",
            "menuitem_quitdaemon",
            "separatormenuitem1",
            "separatormenuitem2",
            "separatormenuitem3",
            "separatormenuitem4"
        ]
        self.config.register_set_function("enable_system_tray", self.on_enable_system_tray_set)
        # bit of a hack to prevent function from doing something on startup
        self.__enabled_set_once = False
        self.config.register_set_function("enable_appindicator", self.on_enable_appindicator_set)

        self.max_download_speed = -1.0
        self.download_rate = 0.0
        self.max_upload_speed = -1.0
        self.upload_rate = 0.0

        self.config_value_changed_dict = {
            "max_download_speed": self._on_max_download_speed,
            "max_upload_speed": self._on_max_upload_speed
        }

    def enable(self):
        """Enables the system tray icon."""
        self.builder = gtk.Builder()
        self.builder.add_from_file(deluge.common.resource_filename(
            "deluge.ui.gtkui", os.path.join("glade", "tray_menu.ui"))
        )

        self.builder.connect_signals({
            "on_menuitem_show_deluge_activate": self.on_menuitem_show_deluge_activate,
            "on_menuitem_add_torrent_activate": self.on_menuitem_add_torrent_activate,
            "on_menuitem_pause_all_activate": self.on_menuitem_pause_all_activate,
            "on_menuitem_resume_all_activate": self.on_menuitem_resume_all_activate,
            "on_menuitem_quit_activate": self.on_menuitem_quit_activate,
            "on_menuitem_quitdaemon_activate": self.on_menuitem_quitdaemon_activate
        })

        self.tray_menu = self.builder.get_object("tray_menu")

        if appindicator and self.config["enable_appindicator"]:
            log.debug("Enabling the Application Indicator..")
            self.indicator = appindicator.Indicator (
                "deluge", "deluge", appindicator.CATEGORY_APPLICATION_STATUS)
            try:
                self.indicator.set_property ("title", _("Deluge"))
            except TypeError:
                # Catch 'title' property error for previous appindicator versions
                pass
            # Pass the menu to the Application Indicator
            self.indicator.set_menu(self.tray_menu)

            # Make sure the status of the Show Window MenuItem is correct
            self._sig_win_hide = self.window.window.connect("hide", self._on_window_hide)
            self._sig_win_show = self.window.window.connect("show", self._on_window_show)
            if self.window.visible():
                self.builder.get_object("menuitem_show_deluge").set_active(True)
            else:
                self.builder.get_object("menuitem_show_deluge").set_active(False)

            # Show the Application Indicator
            self.indicator.set_status(appindicator.STATUS_ACTIVE)

        else:
            log.debug("Enabling the system tray icon..")
            if deluge.common.windows_check() or deluge.common.osx_check():
                self.tray = gtk.status_icon_new_from_pixbuf(common.get_logo(32))
            else:
                try:
                    self.tray = gtk.status_icon_new_from_icon_name("deluge")
                except:
                    log.warning("Update PyGTK to 2.10 or greater for SystemTray..")
                    return

            self.tray.connect("activate", self.on_tray_clicked)
            self.tray.connect("popup-menu", self.on_tray_popup)

            # For some reason these icons do not display in appindicator
            self.builder.get_object("download-limit-image").set_from_file(
                deluge.common.get_pixmap("downloading16.png"))
            self.builder.get_object("upload-limit-image").set_from_file(
                deluge.common.get_pixmap("seeding16.png"))

        client.register_event_handler("ConfigValueChangedEvent", self.config_value_changed)
        if client.connected():
            # We're connected so we need to get some values from the core
            self.__start()
        else:
            # Hide menu widgets because we're not connected to a host.
            for widget in self.hide_widget_list:
                self.builder.get_object(widget).hide()

    def __start(self):
        if self.config["enable_system_tray"]:

            if self.config["classic_mode"]:
                self.hide_widget_list.remove("menuitem_quitdaemon")
                self.hide_widget_list.remove("separatormenuitem4")
                self.builder.get_object("menuitem_quitdaemon").hide()
                self.builder.get_object("separatormenuitem4").hide()

            # These do not work with appindicator currently and can crash Deluge.
            # Related to Launchpad bug #608219
            if appindicator and self.config["enable_appindicator"]:
                self.hide_widget_list.remove("menuitem_download_limit")
                self.hide_widget_list.remove("menuitem_upload_limit")
                self.hide_widget_list.remove("separatormenuitem3")
                self.builder.get_object("menuitem_download_limit").hide()
                self.builder.get_object("menuitem_upload_limit").hide()
                self.builder.get_object("separatormenuitem3").hide()

            # Show widgets in the hide list because we've connected to a host
            for widget in self.hide_widget_list:
                self.builder.get_object(widget).show()

            # Build the bandwidth speed limit menus
            self.build_tray_bwsetsubmenu()

            # Get some config values
            client.core.get_config_value(
                "max_download_speed").addCallback(self._on_max_download_speed)
            client.core.get_config_value(
                "max_upload_speed").addCallback(self._on_max_upload_speed)
            self.send_status_request()

    def start(self):
        self.__start()

    def stop(self):
        if self.config["enable_system_tray"] and not self.config["enable_appindicator"]:
            try:
                # Hide widgets in hide list because we're not connected to a host
                for widget in self.hide_widget_list:
                    self.builder.get_object(widget).hide()
            except Exception, e:
                log.debug("Unable to hide system tray menu widgets: %s", e)

            self.tray.set_tooltip(_("Deluge") + "\n" + _("Not Connected..."))

    def shutdown(self):
        if self.config["enable_system_tray"]:
            if appindicator and self.config["enable_appindicator"]:
                self.indicator.set_status(appindicator.STATUS_PASSIVE)
            else:
                self.tray.set_visible(False)

    def send_status_request(self):
        client.core.get_session_status([
            "payload_upload_rate",
            "payload_download_rate"]).addCallback(self._on_get_session_status)

    def config_value_changed(self, key, value):
        """This is called when we received a config_value_changed signal from
        the core."""

        if key in self.config_value_changed_dict.keys():
            self.config_value_changed_dict[key](value)

    def _on_max_download_speed(self, max_download_speed):
        if self.max_download_speed != max_download_speed:
            self.max_download_speed = max_download_speed
            self.build_tray_bwsetsubmenu()

    def _on_max_upload_speed(self, max_upload_speed):
        if self.max_upload_speed != max_upload_speed:
            self.max_upload_speed = max_upload_speed
            self.build_tray_bwsetsubmenu()

    def _on_get_session_status(self, status):
        self.download_rate = deluge.common.fsize(status["payload_download_rate"])
        self.upload_rate = deluge.common.fsize(status["payload_upload_rate"])

    def update(self):
        if not self.config["enable_system_tray"]:
            return

        # Tool tip text not available for appindicator
        if appindicator and self.config["enable_appindicator"]:
            return

        # Set the tool tip text
        max_download_speed = self.max_download_speed
        max_upload_speed = self.max_upload_speed

        if max_download_speed == -1:
            max_download_speed = _("Unlimited")
        else:
            max_download_speed = "%s %s" % (max_download_speed, _("KiB/s"))
        if max_upload_speed == -1:
            max_upload_speed = _("Unlimited")
        else:
            max_upload_speed = "%s %s" % (max_upload_speed, _("KiB/s"))

        msg = '%s\n%s: %s (%s)\n%s: %s (%s)' % (\
            _("Deluge"), _("Down"), self.download_rate, \
            max_download_speed, _("Up"), self.upload_rate, max_upload_speed)

        # Set the tooltip
        self.tray.set_tooltip(msg)

        self.send_status_request()

    def build_tray_bwsetsubmenu(self):
        # Create the Download speed list sub-menu
        submenu_bwdownset = common.build_menu_radio_list(
                self.config["tray_download_speed_list"], self.tray_setbwdown,
                self.max_download_speed,
                     _("KiB/s"), show_notset=True, show_other=True)

        # Create the Upload speed list sub-menu
        submenu_bwupset = common.build_menu_radio_list(
                self.config["tray_upload_speed_list"], self.tray_setbwup,
                self.max_upload_speed,
                _("KiB/s"), show_notset=True, show_other=True)

        # Add the sub-menus to the tray menu
        self.builder.get_object("menuitem_download_limit").set_submenu(
            submenu_bwdownset)
        self.builder.get_object("menuitem_upload_limit").set_submenu(
            submenu_bwupset)

        # Show the sub-menus for all to see
        submenu_bwdownset.show_all()
        submenu_bwupset.show_all()

        # Re-set the menu to partly work around Launchpad bug #608219
        if appindicator and self.config["enable_appindicator"]:
            self.indicator.set_menu(self.tray_menu)

    def disable(self,invert_app_ind_conf=False):
        """Disables the system tray icon or appindicator."""
        try:
            if invert_app_ind_conf:
                app_ind_conf = not self.config["enable_appindicator"]
            else:
                app_ind_conf = self.config["enable_appindicator"]
            if appindicator and app_ind_conf:
                if hasattr(self, "_sig_win_hide"):
                    self.window.window.disconnect(self._sig_win_hide)
                    self.window.window.disconnect(self._sig_win_show)
                    log.debug("Disabling the application indicator..")

                self.indicator.set_status(appindicator.STATUS_PASSIVE)
                del self.indicator
            else:
                log.debug("Disabling the system tray icon..")
                self.tray.set_visible(False)
                del self.tray
            del self.builder
            del self.tray_menu
        except Exception, e:
            log.debug("Unable to disable system tray: %s", e)

    def blink(self, value):
        try:
            self.tray.set_blinking(value)
        except AttributeError:
            # If self.tray is not defined then ignore. This happens when the
            # tray icon is not being used.
            pass

    def on_enable_system_tray_set(self, key, value):
        """Called whenever the 'enable_system_tray' config key is modified"""
        if value:
            self.enable()
        else:
            self.disable()

    def on_enable_appindicator_set(self, key, value):
        """Called whenever the 'enable_appindicator' config key is modified"""
        if self.__enabled_set_once:
            self.disable(True)
            self.enable()
        self.__enabled_set_once = True

    def on_tray_clicked(self, icon):
        """Called when the tray icon is left clicked."""
        self.blink(False)

        if self.window.active():
            self.window.hide()
        else:
            if self.config["lock_tray"]:
                self.unlock_tray()
            else:
                self.window.present()

    def on_tray_popup(self, status_icon, button, activate_time):
        """Called when the tray icon is right clicked."""
        self.blink(False)

        if self.window.visible():
            self.builder.get_object("menuitem_show_deluge").set_active(True)
        else:
            self.builder.get_object("menuitem_show_deluge").set_active(False)

        popup_function = gtk.status_icon_position_menu
        if deluge.common.windows_check():
            popup_function = None
        self.tray_menu.popup(None, None, popup_function,
            button, activate_time, status_icon)

    def on_menuitem_show_deluge_activate(self, menuitem):
        log.debug("on_menuitem_show_deluge_activate")
        if menuitem.get_active() and not self.window.visible():
            if self.config["lock_tray"]:
                self.unlock_tray()
            else:
                self.window.present()
        elif not menuitem.get_active() and self.window.visible():
            self.window.hide()

    def on_menuitem_add_torrent_activate(self, menuitem):
        log.debug("on_menuitem_add_torrent_activate")
        component.get("AddTorrentDialog").show()

    def on_menuitem_pause_all_activate(self, menuitem):
        log.debug("on_menuitem_pause_all_activate")
        client.core.pause_all_torrents()

    def on_menuitem_resume_all_activate(self, menuitem):
        log.debug("on_menuitem_resume_all_activate")
        client.core.resume_all_torrents()

    def on_menuitem_quit_activate(self, menuitem):
        log.debug("on_menuitem_quit_activate")
        if self.config["lock_tray"] and not self.window.visible():
            self.unlock_tray()

        self.window.quit()

    def on_menuitem_quitdaemon_activate(self, menuitem):
        log.debug("on_menuitem_quitdaemon_activate")
        if self.config["lock_tray"] and not self.window.visible():
            self.unlock_tray()

        self.window.quit(shutdown=True)

    def tray_setbwdown(self, widget, data=None):
        self.setbwlimit(widget, _("Set Maximum Download Speed"), "max_download_speed",
            "tray_download_speed_list", self.max_download_speed, "downloading.svg")

    def _on_window_hide(self, widget, data=None):
        """_on_window_hide - update the menuitem's status"""
        log.debug("_on_window_hide")
        self.builder.get_object("menuitem_show_deluge").set_active(False)

    def _on_window_show(self, widget, data=None):
        """_on_window_show - update the menuitem's status"""
        log.debug("_on_window_show")
        self.builder.get_object("menuitem_show_deluge").set_active(True)

    def tray_setbwup(self, widget, data=None):
        self.setbwlimit(widget, _("Set Maximum Upload Speed"), "max_upload_speed",
            "tray_upload_speed_list", self.max_upload_speed, "seeding.svg")

    def setbwlimit(self, widget, string, core_key, ui_key, default, image):
        """Sets the bandwidth limit based on the user selection."""
        value = widget.get_children()[0].get_text().rstrip(" " + _("KiB/s"))
        if value == _("Unlimited"):
            value = -1

        if value == _("Other..."):
            value = common.show_other_dialog(string, _("KiB/s"), None, image, default)
            if value == None:
                return

        # Set the config in the core
        client.core.set_config({core_key: value})

        self.build_tray_bwsetsubmenu()

    def unlock_tray(self, is_showing_dlg=[False]):
        try:
            from hashlib import sha1 as sha_hash
        except ImportError:
            from sha import new as sha_hash

        log.debug("Show tray lock dialog")

        if is_showing_dlg[0]:
            return
        is_showing_dlg[0] = True

        entered_pass = gtk.Entry(25)
        entered_pass.set_activates_default(True)
        entered_pass.set_width_chars(25)
        entered_pass.set_visibility(False)

        tray_lock = gtk.Dialog(title="", parent=self.window.window,
            buttons=(gtk.STOCK_CANCEL, gtk.RESPONSE_CANCEL, gtk.STOCK_OK,
            gtk.RESPONSE_OK))
        tray_lock.set_default_response(gtk.RESPONSE_OK)
        tray_lock.set_has_separator(False)

        tray_lock.set_border_width(5)

        hbox = gtk.HBox(spacing=5)

        image = gtk.image_new_from_file(deluge.common.get_pixmap("lock48.png"))
        image.set_alignment(0.5, 0.0)
        hbox.pack_start(image, False)

        vbox = gtk.VBox(spacing=5)
        hbox.pack_start(vbox, False)

        label = gtk.Label("<b><big>%s</big></b>" % _("Deluge is password protected!"))
        label.set_use_markup(True)
        label.set_alignment(0.0, 0.5)
        label.set_line_wrap(True)
        vbox.pack_start(label, False)

        tlabel = gtk.Label("<i>%s</i>" % _("Enter your password to continue"))
        tlabel.set_use_markup(True)
        tlabel.set_alignment(0.0, 0.5)
        tlabel.set_line_wrap(True)
        vbox.pack_start(tlabel, False)

        vbox.pack_start(entered_pass)

        tray_lock.vbox.pack_start(hbox)

        def on_response(dialog, response_id):
            if response_id == gtk.RESPONSE_OK:
                if self.config["tray_password"] == sha_hash(entered_pass.get_text()).hexdigest():
                    self.window.present()

            tray_lock.destroy()
            is_showing_dlg[0] = False

        tray_lock.connect("response", on_response)
        tray_lock.show_all()
