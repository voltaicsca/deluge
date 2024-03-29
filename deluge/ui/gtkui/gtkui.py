#
# gtkui.py
#
# Copyright (C) 2007-2009 Andrew Resch <andrewresch@gmail.com>
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

import gobject
gobject.set_prgname("deluge")

# Install the twisted reactor
from twisted.internet import gtk2reactor
reactor = gtk2reactor.install()

import gtk
import sys
import logging

log = logging.getLogger(__name__)

try:
    from setproctitle import setproctitle, getproctitle
except ImportError:
    setproctitle = lambda t: None
    getproctitle = lambda: None

import deluge.component as component
from deluge.ui.client import client
from mainwindow import MainWindow
from menubar import MenuBar
from toolbar import ToolBar
from torrentview import TorrentView
from torrentdetails import TorrentDetails
from sidebar import SideBar
from filtertreeview import FilterTreeView
from preferences import Preferences
from systemtray import SystemTray
from statusbar import StatusBar
from pluginmanager import PluginManager
from ipcinterface import IPCInterface
from deluge.ui.tracker_icons import TrackerIcons
from queuedtorrents import QueuedTorrents
from addtorrentdialog import AddTorrentDialog
from deluge.ui.sessionproxy import SessionProxy
import dialogs
import common

import deluge.configmanager
import deluge.common
import deluge.error

from deluge.ui.ui import _UI

class Gtk(_UI):

    help = """Starts the Deluge GTK+ interface"""

    def __init__(self):
        super(Gtk, self).__init__("gtk")

    def start(self):
        super(Gtk, self).start()
        GtkUI(self.args)

def start():
    Gtk().start()

DEFAULT_PREFS = {
    "classic_mode": True,
    "interactive_add": True,
    "focus_add_dialog": True,
    "enable_system_tray": True,
    "close_to_tray": False,
    "start_in_tray": False,
    "enable_appindicator": False,
    "lock_tray": False,
    "tray_password": "",
    "check_new_releases": True,
    "default_load_path": None,
    "window_maximized": False,
    "window_x_pos": 0,
    "window_y_pos": 0,
    "window_width": 640,
    "window_height": 480,
    "pref_dialog_width": None,
    "pref_dialog_height": None,
    "window_pane_position": -1,
    "tray_download_speed_list" : [5.0, 10.0, 30.0, 80.0, 300.0],
    "tray_upload_speed_list" : [5.0, 10.0, 30.0, 80.0, 300.0],
    "connection_limit_list": [50, 100, 200, 300, 500],
    "enabled_plugins": [],
    "show_connection_manager_on_start": True,
    "autoconnect": False,
    "autoconnect_host_id": None,
    "autostart_localhost": False,
    "autoadd_queued": False,
    "choose_directory_dialog_path": deluge.common.get_default_download_dir(),
    "show_new_releases": True,
    "signal_port": 40000,
    "ntf_tray_blink": True,
    "ntf_sound": False,
    "ntf_sound_path": deluge.common.get_default_download_dir(),
    "ntf_popup": False,
    "ntf_email": False,
    "ntf_email_add": "",
    "ntf_username": "",
    "ntf_pass": "",
    "ntf_server": "",
    "ntf_security": None,
    "signal_port": 40000,
    "show_sidebar": True,
    "show_toolbar": True,
    "show_statusbar": True,
    "sidebar_show_zero": False,
    "sidebar_show_trackers": True,
    "sidebar_position": 170,
    "show_rate_in_title": False,
    "createtorrent.trackers": [],
    "show_piecesbar": False,
    "pieces_color_missing": [65535, 0, 0],
    "pieces_color_waiting": [4874, 56494, 0],
    "pieces_color_downloading": [65535, 55255, 0],
    "pieces_color_completed": [4883, 26985, 56540],
}

class GtkUI(object):
    def __init__(self, args):
        self.daemon_bps = (0,0,0)
        # Setup signals
        try:
            import gnome.ui
            import gnome
            self.gnome_prog = gnome.init("Deluge", deluge.common.get_version())
            self.gnome_client = gnome.ui.master_client()
            def on_die(*args):
                reactor.stop()
            self.gnome_client.connect("die", on_die)
            log.debug("GNOME session 'die' handler registered!")
        except Exception, e:
            log.warning("Unable to register a 'die' handler with the GNOME session manager: %s", e)

        if deluge.common.windows_check():
            from win32api import SetConsoleCtrlHandler
            from win32con import CTRL_CLOSE_EVENT
            from win32con import CTRL_SHUTDOWN_EVENT
            def win_handler(ctrl_type):
                log.debug("ctrl_type: %s", ctrl_type)
                if ctrl_type in (CTRL_CLOSE_EVENT, CTRL_SHUTDOWN_EVENT):
                    reactor.stop()
                    return 1
            SetConsoleCtrlHandler(win_handler)

        # Set process name again to fix gtk issue
        setproctitle(getproctitle())

        # Attempt to register a magnet URI handler with gconf, but do not overwrite
        # if already set by another program.
        common.associate_magnet_links(False)

        # Make sure gtkui.conf has at least the defaults set
        self.config = deluge.configmanager.ConfigManager("gtkui.conf", DEFAULT_PREFS)

        # We need to check on exit if it was started in classic mode to ensure we
        # shutdown the daemon.
        self.started_in_classic = self.config["classic_mode"]

        # Start the IPC Interface before anything else.. Just in case we are
        # already running.
        self.queuedtorrents = QueuedTorrents()
        self.ipcinterface = IPCInterface(args)

        # Initialize gdk threading
        gtk.gdk.threads_init()


        # We make sure that the UI components start once we get a core URI
        client.set_disconnect_callback(self.__on_disconnect)

        self.trackericons = TrackerIcons()
        self.sessionproxy = SessionProxy()
        # Initialize various components of the gtkui
        self.mainwindow = MainWindow()
        self.menubar = MenuBar()
        self.toolbar = ToolBar()
        self.torrentview = TorrentView()
        self.torrentdetails = TorrentDetails()
        self.sidebar = SideBar()
        self.filtertreeview = FilterTreeView()
        self.preferences = Preferences()
        self.systemtray = SystemTray()
        self.statusbar = StatusBar()
        self.addtorrentdialog = AddTorrentDialog()

        # Initalize the plugins
        self.plugins = PluginManager()

        # Late import because of setting up translations
        from connectionmanager import ConnectionManager
        # Show the connection manager
        self.connectionmanager = ConnectionManager()

        from twisted.internet.task import LoopingCall
        rpc_stats = LoopingCall(self.print_rpc_stats)
        rpc_stats.start(10)

        reactor.callWhenRunning(self._on_reactor_start)

        # Initialize gdk threading
        gtk.gdk.threads_enter()
        reactor.run()
        self.shutdown()
        gtk.gdk.threads_leave()

    def shutdown(self, *args, **kwargs):
        log.debug("gtkui shutting down..")

        component.stop()

        # Process any pending gtk events since the mainloop has been quit
        if not deluge.common.windows_check():
            while gtk.events_pending() and reactor.running:
                reactor.doIteration(0)

        # Shutdown all components
        component.shutdown()

        # Make sure the config is saved.
        self.config.save()

    def print_rpc_stats(self):
        import time
        try:
            recv = client.get_bytes_recv()
            sent = client.get_bytes_sent()
        except AttributeError:
            return

        log.debug("sent: %s recv: %s", deluge.common.fsize(sent), deluge.common.fsize(recv))
        t = time.time()
        delta_time = t - self.daemon_bps[0]
        delta_sent = sent - self.daemon_bps[1]
        delta_recv = recv - self.daemon_bps[2]

        sent_rate = deluge.common.fspeed(float(delta_sent) / float(delta_time))
        recv_rate = deluge.common.fspeed(float(delta_recv) / float(delta_time))
        log.debug("sent rate: %s recv rate: %s", sent_rate, recv_rate)
        self.daemon_bps = (t, sent, recv)

    def _on_reactor_start(self):
        log.debug("_on_reactor_start")
        self.mainwindow.first_show()

        if self.config["classic_mode"]:

            def on_dialog_response(response):
                if response != gtk.RESPONSE_YES:
                    # The user does not want to turn Classic Mode off, so just quit
                    self.mainwindow.quit()
                    return
                # Turning off classic_mode
                self.config["classic_mode"] = False
                self.__start_non_classic()

            try:
                client.start_classic_mode()
            except deluge.error.DaemonRunningError:
                d = dialogs.YesNoDialog(
                    _("Turn off Classic Mode?"),
                    _("It appears that a Deluge daemon process (deluged) is already running.\n\n\
You will either need to stop the daemon or turn off Classic Mode to continue.")).run()

                self.started_in_classic = False
                d.addCallback(on_dialog_response)
            except Exception, e:
                import traceback
                tb = sys.exc_info()
                ed = dialogs.ErrorDialog(
                    _("Error Starting Core"),
                    _("There was an error starting the core component which is required to run Deluge in Classic Mode.\n\n\
Please see the details below for more information."), details=traceback.format_exc(tb[2])).run()
                def on_ed_response(response):
                    d = dialogs.YesNoDialog(
                        _("Turn off Classic Mode?"),
                        _("Since there was an error starting in Classic Mode would you like to continue by turning it off?")).run()
                    self.started_in_classic = False
                    d.addCallback(on_dialog_response)
                ed.addCallback(on_ed_response)
            else:
                component.start()
                return

        else:
            self.__start_non_classic()

    def __start_non_classic(self):
            # Autoconnect to a host
            if self.config["autoconnect"]:

                def update_connection_manager():
                    if not self.connectionmanager.running:
                        return
                    self.connectionmanager.builder.get_object("button_refresh").emit("clicked")

                def close_connection_manager():
                    if not self.connectionmanager.running:
                        return
                    self.connectionmanager.builder.get_object("button_close").emit("clicked")

                for host_config in self.connectionmanager.config["hosts"]:
                    hostid, host, port, user, passwd = host_config
                    if hostid == self.config["autoconnect_host_id"]:
                        try_connect = True
                        # Check to see if we need to start the localhost daemon
                        if self.config["autostart_localhost"] and host in ("localhost", "127.0.0.1"):
                            log.debug("Autostarting localhost:%s", host)
                            try_connect = client.start_daemon(
                                port, deluge.configmanager.get_config_dir()
                            )
                            log.debug("Localhost started: %s", try_connect)
                            if not try_connect:
                                dialogs.ErrorDialog(
                                    _("Error Starting Daemon"),
                                    _("There was an error starting the daemon "
                                      "process.  Try running it from a console "
                                      "to see if there is an error.")
                                ).run()

                            # Daemon Started, let's update it's info
                            reactor.callLater(0.5, update_connection_manager)

                        def on_connect(connector):
                            component.start()
                            reactor.callLater(0.2, update_connection_manager)
                            reactor.callLater(0.5, close_connection_manager)

                        def on_connect_fail(reason, try_counter,
                                            host, port, user, passwd):
                            if not try_counter:
                                return

                            if reason.check(deluge.error.AuthenticationRequired,
                                            deluge.error.BadLoginError):
                                log.debug("PasswordRequired exception")
                                dialog = dialogs.AuthenticationDialog(
                                    reason.value.message, reason.value.username
                                )
                                def dialog_finished(response_id, host, port):
                                    if response_id == gtk.RESPONSE_OK:
                                        reactor.callLater(
                                            0.5, do_connect, try_counter-1,
                                            host, port, dialog.get_username(),
                                            dialog.get_password())
                                dialog.run().addCallback(dialog_finished,
                                                         host, port)
                                return

                            log.info("Connection to host failed..")
                            log.info("Retrying connection.. Retries left: "
                                     "%s", try_counter)
                            reactor.callLater(0.5, update_connection_manager)
                            reactor.callLater(0.5, do_connect, try_counter-1,
                                              host, port, user, passwd)

                        def do_connect(try_counter, host, port, user, passwd):
                            log.debug("Trying to connect to %s@%s:%s",
                                      user, host, port)
                            d = client.connect(host, port, user, passwd)
                            d.addCallback(on_connect)
                            d.addErrback(on_connect_fail, try_counter,
                                         host, port, user, passwd)

                        if try_connect:
                            reactor.callLater(
                                0.5, do_connect, 6, host, port, user, passwd
                            )
                        break

            if self.config["show_connection_manager_on_start"]:
                # XXX: We need to call a simulate() here, but this could be a bug in twisted
                try:
                    reactor._simulate()
                except AttributeError:
                    # twisted < 12
                    reactor.simulate()
                self.connectionmanager.show()


    def __on_disconnect(self):
        """
        Called when disconnected from the daemon.  We basically just stop all
        the components here.
        """
        component.stop()
