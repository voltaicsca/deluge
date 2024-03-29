#
# torrentmanager.py
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
#   The Free Software Foundation, Inc.,
#   51 Franklin Street, Fifth Floor
#   Boston, MA  02110-1301, USA.
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


"""TorrentManager handles Torrent objects"""

import cPickle
import os
import time
import shutil
import operator
import logging
import re

from twisted.internet.task import LoopingCall

from deluge._libtorrent import lt

from deluge.event import *
from deluge.error import *
import deluge.component as component
from deluge.configmanager import ConfigManager, get_config_dir
from deluge.core.authmanager import AUTH_LEVEL_ADMIN
from deluge.core.torrent import Torrent
from deluge.core.torrent import TorrentOptions
import deluge.core.oldstateupgrader
from deluge.common import utf8_encoded

log = logging.getLogger(__name__)

class TorrentState:
    def __init__(self,
            torrent_id=None,
            filename=None,
            total_uploaded=0,
            trackers=None,
            compact=False,
            paused=False,
            save_path=None,
            max_connections=-1,
            max_upload_slots=-1,
            max_upload_speed=-1.0,
            max_download_speed=-1.0,
            prioritize_first_last=False,
            sequential_download=False,
            file_priorities=None,
            queue=None,
            auto_managed=True,
            is_finished=False,
            stop_ratio=2.00,
            stop_at_ratio=False,
            remove_at_ratio=False,
            move_completed=False,
            move_completed_path=None,
            magnet=None,
            time_added=-1,
            last_seen_complete=0.0,   # 0 is the default returned when the info
            owner="",                 # does not exist on lt >= .16
            shared=False
        ):
        self.torrent_id = torrent_id
        self.filename = filename
        self.total_uploaded = total_uploaded
        self.trackers = trackers
        self.queue = queue
        self.is_finished = is_finished
        self.magnet = magnet
        self.time_added = time_added
        self.last_seen_complete = last_seen_complete
        self.owner = owner

        # Options
        self.compact = compact
        self.paused = paused
        self.save_path = save_path
        self.max_connections = max_connections
        self.max_upload_slots = max_upload_slots
        self.max_upload_speed = max_upload_speed
        self.max_download_speed = max_download_speed
        self.prioritize_first_last = prioritize_first_last
        self.sequential_download = sequential_download
        self.file_priorities = file_priorities
        self.auto_managed = auto_managed
        self.stop_ratio = stop_ratio
        self.stop_at_ratio = stop_at_ratio
        self.remove_at_ratio = remove_at_ratio
        self.move_completed = move_completed
        self.move_completed_path = move_completed_path
        self.shared = shared

class TorrentManagerState:
    def __init__(self):
        self.torrents = []

class TorrentManager(component.Component):
    """
    TorrentManager contains a list of torrents in the current libtorrent
    session.  This object is also responsible for saving the state of the
    session for use on restart.
    """

    def __init__(self):
        component.Component.__init__(self, "TorrentManager", interval=5,
                                     depend=["CorePluginManager"])
        log.debug("TorrentManager init..")
        # Set the libtorrent session
        self.session = component.get("Core").session
        # Set the alertmanager
        self.alerts = component.get("AlertManager")
        # Get the core config
        self.config = ConfigManager("core.conf")

        # Make sure the state folder has been created
        if not os.path.exists(os.path.join(get_config_dir(), "state")):
            os.makedirs(os.path.join(get_config_dir(), "state"))

        # Create the torrents dict { torrent_id: Torrent }
        self.torrents = {}
        self.last_seen_complete_loop = None
        self.queued_torrents = set()

        # This is a list of torrent_id when we shutdown the torrentmanager.
        # We use this list to determine if all active torrents have been paused
        # and that their resume data has been written.
        self.shutdown_torrent_pause_list = []

        # self.num_resume_data used to save resume_data in bulk
        self.num_resume_data = 0

        # Keeps track of resume data that needs to be saved to disk
        self.resume_data = {}

        # Register set functions
        self.config.register_set_function("max_connections_per_torrent",
            self.on_set_max_connections_per_torrent)
        self.config.register_set_function("max_upload_slots_per_torrent",
            self.on_set_max_upload_slots_per_torrent)
        self.config.register_set_function("max_upload_speed_per_torrent",
            self.on_set_max_upload_speed_per_torrent)
        self.config.register_set_function("max_download_speed_per_torrent",
            self.on_set_max_download_speed_per_torrent)

        # Register alert functions
        self.alerts.register_handler("torrent_finished_alert",
            self.on_alert_torrent_finished)
        self.alerts.register_handler("torrent_paused_alert",
            self.on_alert_torrent_paused)
        self.alerts.register_handler("torrent_checked_alert",
            self.on_alert_torrent_checked)
        self.alerts.register_handler("tracker_reply_alert",
            self.on_alert_tracker_reply)
        self.alerts.register_handler("tracker_announce_alert",
            self.on_alert_tracker_announce)
        self.alerts.register_handler("tracker_warning_alert",
            self.on_alert_tracker_warning)
        self.alerts.register_handler("tracker_error_alert",
            self.on_alert_tracker_error)
        self.alerts.register_handler("storage_moved_alert",
            self.on_alert_storage_moved)
        self.alerts.register_handler("torrent_resumed_alert",
            self.on_alert_torrent_resumed)
        self.alerts.register_handler("state_changed_alert",
            self.on_alert_state_changed)
        self.alerts.register_handler("save_resume_data_alert",
            self.on_alert_save_resume_data)
        self.alerts.register_handler("save_resume_data_failed_alert",
            self.on_alert_save_resume_data_failed)
        self.alerts.register_handler("file_renamed_alert",
            self.on_alert_file_renamed)
        self.alerts.register_handler("metadata_received_alert",
            self.on_alert_metadata_received)
        self.alerts.register_handler("file_error_alert",
            self.on_alert_file_error)
        self.alerts.register_handler("file_completed_alert",
            self.on_alert_file_completed)

    def start(self):
        # Get the pluginmanager reference
        self.plugins = component.get("CorePluginManager")

        # Run the old state upgrader before loading state
        deluge.core.oldstateupgrader.OldStateUpgrader()

        # Try to load the state from file
        self.load_state()

        # Save the state every 5 minutes
        self.save_state_timer = LoopingCall(self.save_state)
        self.save_state_timer.start(200, False)
        self.save_resume_data_timer = LoopingCall(self.save_resume_data)
        self.save_resume_data_timer.start(190)

        if self.last_seen_complete_loop:
            self.last_seen_complete_loop.start(60)

    def stop(self):
        # Stop timers
        if self.save_state_timer.running:
            self.save_state_timer.stop()

        if self.save_resume_data_timer.running:
            self.save_resume_data_timer.stop()

        if self.last_seen_complete_loop:
            self.last_seen_complete_loop.stop()

        # Save state on shutdown
        self.save_state()

        # Make another list just to make sure all paused torrents will be
        # passed to self.save_resume_data(). With
        # self.shutdown_torrent_pause_list it is possible to have a case when
        # torrent_id is removed from it in self.on_alert_torrent_paused()
        # before we call self.save_resume_data() here.
        save_resume_data_list = []
        for key in self.torrents:
            # Stop the status cleanup LoopingCall here
            self.torrents[key].prev_status_cleanup_loop.stop()
            if not self.torrents[key].handle.is_paused():
                # We set auto_managed false to prevent lt from resuming the torrent
                self.torrents[key].handle.auto_managed(False)
                self.torrents[key].handle.pause()
                self.shutdown_torrent_pause_list.append(key)
                save_resume_data_list.append(key)

        self.save_resume_data(save_resume_data_list)

        # We have to wait for all torrents to pause and write their resume data
        wait = True
        while wait:
            if self.shutdown_torrent_pause_list:
                wait = True
            else:
                wait = False
                for torrent in self.torrents.values():
                    if torrent.waiting_on_resume_data:
                        wait = True
                        break

            time.sleep(0.01)
            # Wait for all alerts
            self.alerts.handle_alerts(True)

    def update(self):
        for torrent_id, torrent in self.torrents.items():
            if torrent.options["stop_at_ratio"] and torrent.state not in (
                                "Checking", "Allocating", "Paused", "Queued"):
                # If the global setting is set, but the per-torrent isn't..
                # Just skip to the next torrent.
                # This is so that a user can turn-off the stop at ratio option
                # on a per-torrent basis
                if not torrent.options["stop_at_ratio"]:
                    continue
                if torrent.get_ratio() >= torrent.options["stop_ratio"] and torrent.is_finished:
                    if torrent.options["remove_at_ratio"]:
                        self.remove(torrent_id)
                        break
                    if not torrent.handle.is_paused():
                        torrent.pause()

    def __getitem__(self, torrent_id):
        """Return the Torrent with torrent_id"""
        return self.torrents[torrent_id]

    def get_torrent_list(self):
        """Returns a list of torrent_ids"""
        torrent_ids = self.torrents.keys()
        if component.get("RPCServer").get_session_auth_level() == AUTH_LEVEL_ADMIN:
            return torrent_ids

        current_user = component.get("RPCServer").get_session_user()
        for torrent_id in torrent_ids[:]:
            torrent_status = self[torrent_id].get_status(["owner", "shared"])
            if torrent_status["owner"] != current_user and torrent_status["shared"] == False:
                torrent_ids.pop(torrent_ids.index(torrent_id))
        return torrent_ids

    def get_torrent_info_from_file(self, filepath):
        """Returns a torrent_info for the file specified or None"""
        torrent_info = None
        # Get the torrent data from the torrent file
        try:
            log.debug("Attempting to create torrent_info from %s", filepath)
            _file = open(filepath, "rb")
            torrent_info = lt.torrent_info(lt.bdecode(_file.read()))
            _file.close()
        except (IOError, RuntimeError), e:
            log.warning("Unable to open %s: %s", filepath, e)

        return torrent_info

    def legacy_get_resume_data_from_file(self, torrent_id):
        """Returns an entry with the resume data or None"""
        fastresume = ""
        try:
            _file = open(os.path.join(get_config_dir(), "state",
                                      torrent_id + ".fastresume"), "rb")
            fastresume = _file.read()
            _file.close()
        except IOError, e:
            log.debug("Unable to load .fastresume: %s", e)

        return str(fastresume)

    def legacy_delete_resume_data(self, torrent_id):
        """Deletes the .fastresume file"""
        path = os.path.join(get_config_dir(), "state",
                            torrent_id + ".fastresume")
        log.debug("Deleting fastresume file: %s", path)
        try:
            os.remove(path)
        except Exception, e:
            log.warning("Unable to delete the fastresume file: %s", e)

    def add(self, torrent_info=None, state=None, options=None, save_state=True,
            filedump=None, filename=None, magnet=None, resume_data=None, owner=None):
        """Add a torrent to the manager and returns it's torrent_id"""

        if owner is None:
            owner = component.get("RPCServer").get_session_user()
            if not owner:
                owner = "localclient"

        if torrent_info is None and state is None and filedump is None and magnet is None:
            log.debug("You must specify a valid torrent_info, torrent state or magnet.")
            return

        log.debug("torrentmanager.add")
        add_torrent_params = {}

        if filedump is not None:
            try:
                torrent_info = lt.torrent_info(lt.bdecode(filedump))
            except Exception, e:
                log.error("Unable to decode torrent file!: %s", e)
                # XXX: Probably should raise an exception here..
                return

        if torrent_info is None and state:
            # We have no torrent_info so we need to add the torrent with information
            # from the state object.

            # Populate the options dict from state
            options = TorrentOptions()
            options["max_connections"] = state.max_connections
            options["max_upload_slots"] = state.max_upload_slots
            options["max_upload_speed"] = state.max_upload_speed
            options["max_download_speed"] = state.max_download_speed
            options["prioritize_first_last_pieces"] = state.prioritize_first_last
            options["sequential_download"] = state.sequential_download
            options["file_priorities"] = state.file_priorities
            options["compact_allocation"] = state.compact
            options["download_location"] = state.save_path
            options["auto_managed"] = state.auto_managed
            options["stop_at_ratio"] = state.stop_at_ratio
            options["stop_ratio"] = state.stop_ratio
            options["remove_at_ratio"] = state.remove_at_ratio
            options["move_completed"] = state.move_completed
            options["move_completed_path"] = state.move_completed_path
            options["add_paused"] = state.paused
            options["shared"] = state.shared

            ti = self.get_torrent_info_from_file(
                    os.path.join(get_config_dir(),
                                    "state", state.torrent_id + ".torrent"))
            if ti:
                add_torrent_params["ti"] = ti
            elif state.magnet:
                magnet = state.magnet
            else:
                log.error("Unable to add torrent!")
                return

            # Handle legacy case with storing resume data in individual files
            # for each torrent
            if resume_data is None:
                resume_data = self.legacy_get_resume_data_from_file(state.torrent_id)
                self.legacy_delete_resume_data(state.torrent_id)

            add_torrent_params["resume_data"] = resume_data
        else:
            # We have a torrent_info object or magnet uri so we're not loading from state.
            if torrent_info:
                add_torrent_id = str(torrent_info.info_hash())
                if add_torrent_id in self.get_torrent_list():
                    # Torrent already exists just append any extra trackers.
                    log.debug("Torrent (%s) exists, checking for trackers to add...", add_torrent_id)
                    add_torrent_trackers = []
                    for value in torrent_info.trackers():
                        tracker = {}
                        tracker["url"] = value.url
                        tracker["tier"] = value.tier
                        add_torrent_trackers.append(tracker)

                    torrent_trackers = {}
                    tracker_list = []
                    for tracker in  self[add_torrent_id].get_status(["trackers"])["trackers"]:
                        torrent_trackers[(tracker["url"])] = tracker
                        tracker_list.append(tracker)

                    added_tracker = False
                    for tracker in add_torrent_trackers:
                        if tracker['url'] not in torrent_trackers:
                            tracker_list.append(tracker)
                            added_tracker = True

                    if added_tracker:
                        self[add_torrent_id].set_trackers(tracker_list)
                    return

            # Check if options is None and load defaults
            if options == None:
                options = TorrentOptions()
            else:
                o = TorrentOptions()
                o.update(options)
                options = o

            # Check for renamed files and if so, rename them in the torrent_info
            # before adding to the session.
            if options["mapped_files"]:
                for index, fname in options["mapped_files"].items():
                    fname = deluge.core.torrent.sanitize_filepath(fname)
                    log.debug("renaming file index %s to %s", index, fname)
                    torrent_info.rename_file(index, utf8_encoded(fname))

            add_torrent_params["ti"] = torrent_info

        #log.info("Adding torrent: %s", filename)
        log.debug("options: %s", options)

        # Set the right storage_mode
        if options["compact_allocation"]:
            storage_mode = lt.storage_mode_t(2)
        else:
            storage_mode = lt.storage_mode_t(1)

        # Fill in the rest of the add_torrent_params dictionary
        add_torrent_params["save_path"] = utf8_encoded(options["download_location"])
        add_torrent_params["storage_mode"] = storage_mode
        add_torrent_params["paused"] = True
        add_torrent_params["auto_managed"] = False
        add_torrent_params["duplicate_is_error"] = True

        # We need to pause the AlertManager momentarily to prevent alerts
        # for this torrent being generated before a Torrent object is created.
        component.pause("AlertManager")

        handle = None
        try:
            if magnet:
                handle = lt.add_magnet_uri(self.session, utf8_encoded(magnet), add_torrent_params)
            else:
                handle = self.session.add_torrent(add_torrent_params)
        except RuntimeError, e:
            log.warning("Error adding torrent: %s", e)

        if not handle or not handle.is_valid():
            log.debug("torrent handle is invalid!")
            # The torrent was not added to the session
            component.resume("AlertManager")
            return

        log.debug("handle id: %s", str(handle.info_hash()))
        # Set auto_managed to False because the torrent is paused
        handle.auto_managed(False)
        # Create a Torrent object
        owner = state.owner if state else (
            owner if owner else component.get("RPCServer").get_session_user()
        )
        account_exists = component.get("AuthManager").has_account(owner)
        if not account_exists:
            owner = 'localclient'
        torrent = Torrent(handle, options, state, filename, magnet, owner)
        # Add the torrent object to the dictionary
        self.torrents[torrent.torrent_id] = torrent
        if self.config["queue_new_to_top"]:
            handle.queue_position_top()

        component.resume("AlertManager")

        # Resume the torrent if needed
        if not options["add_paused"]:
            torrent.resume()

        # Add to queued torrents set
        self.queued_torrents.add(torrent.torrent_id)

        # Write the .torrent file to the state directory
        if filedump:
            try:
                save_file = open(os.path.join(get_config_dir(), "state",
                        torrent.torrent_id + ".torrent"),
                        "wb")
                save_file.write(filedump)
                save_file.close()
            except IOError, e:
                log.warning("Unable to save torrent file: %s", e)

            # If the user has requested a copy of the torrent be saved elsewhere
            # we need to do that.
            if self.config["copy_torrent_file"] and filename is not None:
                try:
                    save_file = open(
                        os.path.join(self.config["torrentfiles_location"], filename),
                        "wb")
                    save_file.write(filedump)
                    save_file.close()
                except IOError, e:
                    log.warning("Unable to save torrent file: %s", e)

        if save_state:
            # Save the session state
            self.save_state()

        # Emit torrent_added signal
        from_state = state is not None
        component.get("EventManager").emit(
            TorrentAddedEvent(torrent.torrent_id, from_state)
        )
        log.info("Torrent %s from user \"%s\" %s",
                 torrent.get_status(["name"])["name"],
                 torrent.get_status(["owner"])["owner"],
                 (from_state and "loaded" or "added"))
        return torrent.torrent_id

    def load_torrent(self, torrent_id):
        """Load a torrent file from state and return it's torrent info"""
        filedump = None
        # Get the torrent data from the torrent file
        try:
            log.debug("Attempting to open %s for add.", torrent_id)
            _file = open(
                os.path.join(
                    get_config_dir(), "state", torrent_id + ".torrent"),
                        "rb")
            filedump = lt.bdecode(_file.read())
            _file.close()
        except (IOError, RuntimeError), e:
            log.warning("Unable to open %s: %s", torrent_id, e)
            return False

        return filedump

    def remove(self, torrent_id, remove_data=False):
        """
        Remove a torrent from the session.

        :param torrent_id: the torrent to remove
        :type torrent_id: string
        :param remove_data: if True, remove the downloaded data
        :type remove_data: bool

        :returns: True if removed successfully, False if not
        :rtype: bool

        :raises InvalidTorrentError: if the torrent_id is not in the session

        """

        if torrent_id not in self.torrents:
            raise InvalidTorrentError("torrent_id not in session")

        torrent_name = self.torrents[torrent_id].get_status(["name"])["name"]

        # Emit the signal to the clients
        component.get("EventManager").emit(PreTorrentRemovedEvent(torrent_id))

        try:
            self.session.remove_torrent(self.torrents[torrent_id].handle,
                1 if remove_data else 0)
        except (RuntimeError, KeyError), e:
            log.warning("Error removing torrent: %s", e)
            return False

        # Remove fastresume data if it is exists
        resume_data = self.load_resume_data_file()
        resume_data.pop(torrent_id, None)
        self.save_resume_data_file(resume_data)

        # Remove the .torrent file in the state
        self.torrents[torrent_id].delete_torrentfile()

        # Remove the torrent file from the user specified directory
        filename = self.torrents[torrent_id].filename
        if self.config["copy_torrent_file"] \
            and self.config["del_copy_torrent_file"] \
            and filename:
            try:
                users_torrent_file = os.path.join(
                    self.config["torrentfiles_location"],
                    filename)
                log.info("Delete user's torrent file: %s",
                    users_torrent_file)
                os.remove(users_torrent_file)
            except Exception, e:
                log.warning("Unable to remove copy torrent file: %s", e)

        # Stop the looping call
        self.torrents[torrent_id].prev_status_cleanup_loop.stop()

        # Remove from set if it wasn't finished
        if not self.torrents[torrent_id].is_finished:
            try:
                self.queued_torrents.remove(torrent_id)
            except KeyError:
                log.debug("%s isn't in queued torrents set?", torrent_id)

        # Remove the torrent from deluge's session
        try:
            del self.torrents[torrent_id]
        except (KeyError, ValueError):
            return False

        # Save the session state
        self.save_state()

        # Emit the signal to the clients
        component.get("EventManager").emit(TorrentRemovedEvent(torrent_id))
        log.info("Torrent %s removed by user: %s", torrent_name,
                 component.get("RPCServer").get_session_user())
        return True

    def load_state(self):
        """Load the state of the TorrentManager from the torrents.state file"""
        state = TorrentManagerState()

        try:
            log.debug("Opening torrent state file for load.")
            state_file = open(
                os.path.join(get_config_dir(), "state", "torrents.state"), "rb")
            state = cPickle.load(state_file)
            state_file.close()
        except (EOFError, IOError, Exception, cPickle.UnpicklingError), e:
            log.warning("Unable to load state file: %s", e)

        # Try to use an old state
        try:
            state_tmp = TorrentState()
            if dir(state.torrents[0]) != dir(state_tmp):
                for attr in (set(dir(state_tmp)) - set(dir(state.torrents[0]))):
                    for s in state.torrents:
                        setattr(s, attr, getattr(state_tmp, attr, None))
        except Exception, e:
            log.warning("Unable to update state file to a compatible version: %s", e)

        # Reorder the state.torrents list to add torrents in the correct queue
        # order.
        state.torrents.sort(key=operator.attrgetter("queue"))

        resume_data = self.load_resume_data_file()

        for torrent_state in state.torrents:
            try:
                self.add(state=torrent_state, save_state=False,
                         resume_data=resume_data.get(torrent_state.torrent_id))
            except AttributeError, e:
                log.error("Torrent state file is either corrupt or incompatible! %s", e)
                break


        if lt.version_minor < 16:
            log.debug("libtorrent version is lower than 0.16. Start looping "
                      "callback to calculate last_seen_complete info.")
            def calculate_last_seen_complete():
                for torrent in self.torrents.values():
                    torrent.calculate_last_seen_complete()
            self.last_seen_complete_loop = LoopingCall(
                calculate_last_seen_complete
            )

        component.get("EventManager").emit(SessionStartedEvent())

    def save_state(self):
        """Save the state of the TorrentManager to the torrents.state file"""
        state = TorrentManagerState()
        # Create the state for each Torrent and append to the list
        for torrent in self.torrents.values():
            paused = False
            if torrent.state == "Paused":
                paused = True

            torrent_state = TorrentState(
                torrent.torrent_id,
                torrent.filename,
                torrent.get_status(["total_uploaded"])["total_uploaded"],
                torrent.trackers,
                torrent.options["compact_allocation"],
                paused,
                torrent.options["download_location"],
                torrent.options["max_connections"],
                torrent.options["max_upload_slots"],
                torrent.options["max_upload_speed"],
                torrent.options["max_download_speed"],
                torrent.options["prioritize_first_last_pieces"],
                torrent.options["sequential_download"],
                torrent.options["file_priorities"],
                torrent.get_queue_position(),
                torrent.options["auto_managed"],
                torrent.is_finished,
                torrent.options["stop_ratio"],
                torrent.options["stop_at_ratio"],
                torrent.options["remove_at_ratio"],
                torrent.options["move_completed"],
                torrent.options["move_completed_path"],
                torrent.magnet,
                torrent.time_added,
                torrent.get_last_seen_complete(),
                torrent.owner,
                torrent.options["shared"]
            )
            state.torrents.append(torrent_state)

        # Pickle the TorrentManagerState object
        try:
            log.debug("Saving torrent state file.")
            state_file = open(os.path.join(get_config_dir(),
                              "state", "torrents.state.new"), "wb")
            cPickle.dump(state, state_file)
            state_file.flush()
            os.fsync(state_file.fileno())
            state_file.close()
        except IOError, e:
            log.warning("Unable to save state file: %s", e)
            return True

        # We have to move the 'torrents.state.new' file to 'torrents.state'
        try:
            shutil.move(
                os.path.join(get_config_dir(), "state", "torrents.state.new"),
                os.path.join(get_config_dir(), "state", "torrents.state"))
        except IOError:
            log.warning("Unable to save state file.")
            return True

        # We return True so that the timer thread will continue
        return True

    def save_resume_data(self, torrent_ids=None):
        """
        Saves resume data for list of torrent_ids or for all torrents if
        torrent_ids is None
        """

        if torrent_ids is None:
            torrent_ids = self.torrents.keys()

        for torrent_id in torrent_ids:
            self.torrents[torrent_id].save_resume_data()

        self.num_resume_data = len(torrent_ids)

    def load_resume_data_file(self):
        resume_data = {}
        try:
            log.debug("Opening torrents fastresume file for load.")
            fastresume_file = open(os.path.join(get_config_dir(), "state",
                                                "torrents.fastresume"), "rb")
            resume_data = lt.bdecode(fastresume_file.read())
            fastresume_file.close()
        except (EOFError, IOError, Exception), e:
            log.warning("Unable to load fastresume file: %s", e)

        # If the libtorrent bdecode doesn't happen properly, it will return None
        # so we need to make sure we return a {}
        if resume_data is None:
            return {}

        return resume_data

    def save_resume_data_file(self, resume_data=None):
        """
        Saves the resume data file with the contents of self.resume_data.  If
        `resume_data` is None, then we grab the resume_data from the file on
        disk, else, we update `resume_data` with self.resume_data and save
        that to disk.

        :param resume_data: the current resume_data, this will be loaded from disk if not provided
        :type resume_data: dict

        """
        # Check to see if we're waiting on more resume data
        if self.num_resume_data or not self.resume_data:
            return

        path = os.path.join(get_config_dir(), "state", "torrents.fastresume")

        # First step is to load the existing file and update the dictionary
        if resume_data is None:
            resume_data = self.load_resume_data_file()

        resume_data.update(self.resume_data)
        self.resume_data = {}

        try:
            log.debug("Saving fastresume file: %s", path)
            fastresume_file = open(path, "wb")
            fastresume_file.write(lt.bencode(resume_data))
            fastresume_file.flush()
            os.fsync(fastresume_file.fileno())
            fastresume_file.close()
        except IOError:
            log.warning("Error trying to save fastresume file")

    def remove_empty_folders(self, torrent_id, folder):
        """
        Recursively removes folders but only if they are empty.
        Cleans up after libtorrent folder renames.

        """
        if torrent_id not in self.torrents:
            raise InvalidTorrentError("torrent_id is not in session")

        info = self.torrents[torrent_id].get_status(['save_path'])
        # Regex removes leading slashes that causes join function to ignore save_path
        folder_full_path = os.path.join(info['save_path'], re.sub("^/*", "", folder))
        folder_full_path = os.path.normpath(folder_full_path)

        try:
            if not os.listdir(folder_full_path):
                os.removedirs(folder_full_path)
                log.debug("Removed Empty Folder %s", folder_full_path)
            else:
                for root, dirs, files in os.walk(folder_full_path, topdown=False):
                    for name in dirs:
                        try:
                            os.removedirs(os.path.join(root, name))
                            log.debug("Removed Empty Folder %s", os.path.join(root, name))
                        except OSError as (errno, strerror):
                            from errno import ENOTEMPTY
                            if errno == ENOTEMPTY:
                                # Error raised if folder is not empty
                                log.debug("%s", strerror)

        except OSError as (errno, strerror):
            log.debug("Cannot Remove Folder: %s (ErrNo %s)", strerror, errno)

    def get_queue_position(self, torrent_id):
        """Get queue position of torrent"""
        return self.torrents[torrent_id].get_queue_position()

    def queue_top(self, torrent_id):
        """Queue torrent to top"""
        if self.torrents[torrent_id].get_queue_position() == 0:
            return False

        self.torrents[torrent_id].handle.queue_position_top()
        return True

    def queue_up(self, torrent_id):
        """Queue torrent up one position"""
        if self.torrents[torrent_id].get_queue_position() == 0:
            return False

        self.torrents[torrent_id].handle.queue_position_up()
        return True

    def queue_down(self, torrent_id):
        """Queue torrent down one position"""
        if self.torrents[torrent_id].get_queue_position() == (len(self.queued_torrents) - 1):
            return False

        self.torrents[torrent_id].handle.queue_position_down()
        return True

    def queue_bottom(self, torrent_id):
        """Queue torrent to bottom"""
        if self.torrents[torrent_id].get_queue_position() == (len(self.queued_torrents) - 1):
            return False

        self.torrents[torrent_id].handle.queue_position_bottom()
        return True

    def on_set_max_connections_per_torrent(self, key, value):
        """Sets the per-torrent connection limit"""
        log.debug("max_connections_per_torrent set to %s..", value)
        for key in self.torrents.keys():
            self.torrents[key].set_max_connections(value)

    def on_set_max_upload_slots_per_torrent(self, key, value):
        """Sets the per-torrent upload slot limit"""
        log.debug("max_upload_slots_per_torrent set to %s..", value)
        for key in self.torrents.keys():
            self.torrents[key].set_max_upload_slots(value)

    def on_set_max_upload_speed_per_torrent(self, key, value):
        log.debug("max_upload_speed_per_torrent set to %s..", value)
        for key in self.torrents.keys():
            self.torrents[key].set_max_upload_speed(value)

    def on_set_max_download_speed_per_torrent(self, key, value):
        log.debug("max_download_speed_per_torrent set to %s..", value)
        for key in self.torrents.keys():
            self.torrents[key].set_max_download_speed(value)

    ## Alert handlers ##
    def on_alert_torrent_finished(self, alert):
        log.debug("on_alert_torrent_finished")
        try:
            torrent = self.torrents[str(alert.handle.info_hash())]
            torrent_id = str(alert.handle.info_hash())
        except:
            return
        log.debug("%s is finished..", torrent_id)

        # Get the total_download and if it's 0, do not move.. It's likely
        # that the torrent wasn't downloaded, but just added.
        total_download = torrent.get_status(["total_payload_download"])["total_payload_download"]

        # Move completed download to completed folder if needed
        if not torrent.is_finished and total_download:
            move_path = None

            if torrent.options["move_completed"]:
                move_path = torrent.options["move_completed_path"]
                if torrent.options["download_location"] != move_path:
                    torrent.move_storage(move_path)

            component.get("EventManager").emit(TorrentFinishedEvent(torrent_id))

        torrent.is_finished = True
        torrent.update_state()

        # Torrent is no longer part of the queue
        try:
            self.queued_torrents.remove(torrent_id)
        except KeyError:
            # Sometimes libtorrent fires a TorrentFinishedEvent twice
            log.debug("%s isn't in queued torrents set?", torrent_id)

        # Only save resume data if it was actually downloaded something. Helps
        # on startup with big queues with lots of seeding torrents. Libtorrent
        # emits alert_torrent_finished for them, but there seems like nothing
        # worth really to save in resume data, we just read it up in
        # self.load_state().
        if total_download:
            self.save_resume_data((torrent_id, ))

    def on_alert_torrent_paused(self, alert):
        log.debug("on_alert_torrent_paused")
        try:
            torrent = self.torrents[str(alert.handle.info_hash())]
            torrent_id = str(alert.handle.info_hash())
        except:
            return
        # Set the torrent state
        old_state = torrent.state
        torrent.update_state()
        if torrent.state != old_state:
            component.get("EventManager").emit(TorrentStateChangedEvent(torrent_id, torrent.state))

        # Don't save resume data for each torrent after self.stop() was called.
        # We save resume data in bulk in self.stop() in this case.
        if self.save_resume_data_timer.running:
            # Write the fastresume file
            self.save_resume_data((torrent_id, ))

        if torrent_id in self.shutdown_torrent_pause_list:
            self.shutdown_torrent_pause_list.remove(torrent_id)

    def on_alert_torrent_checked(self, alert):
        log.debug("on_alert_torrent_checked")
        try:
            torrent = self.torrents[str(alert.handle.info_hash())]
        except:
            return

        # Check to see if we're forcing a recheck and set it back to paused
        # if necessary
        if torrent.forcing_recheck:
            torrent.forcing_recheck = False
            if torrent.forcing_recheck_paused:
                torrent.handle.pause()

        # Set the torrent state
        torrent.update_state()

    def on_alert_tracker_reply(self, alert):
        log.debug("on_alert_tracker_reply: %s", alert.message().decode("utf8"))
        try:
            torrent = self.torrents[str(alert.handle.info_hash())]
        except:
            return

        # Set the tracker status for the torrent
        if alert.message() != "Got peers from DHT":
            torrent.set_tracker_status(_("Announce OK"))

        # Check to see if we got any peer information from the tracker
        if alert.handle.status().num_complete == -1 or \
            alert.handle.status().num_incomplete == -1:
            # We didn't get peer information, so lets send a scrape request
            torrent.scrape_tracker()

    def on_alert_tracker_announce(self, alert):
        log.debug("on_alert_tracker_announce")
        try:
            torrent = self.torrents[str(alert.handle.info_hash())]
        except:
            return

        # Set the tracker status for the torrent
        torrent.set_tracker_status(_("Announce Sent"))

    def on_alert_tracker_warning(self, alert):
        log.debug("on_alert_tracker_warning")
        try:
            torrent = self.torrents[str(alert.handle.info_hash())]
        except:
            return
        tracker_status = '%s: %s' % (_("Warning"), str(alert.message()))
        # Set the tracker status for the torrent
        torrent.set_tracker_status(tracker_status)

    def on_alert_tracker_error(self, alert):
        log.debug("on_alert_tracker_error")
        try:
            torrent = self.torrents[str(alert.handle.info_hash())]
        except:
            return
        tracker_status = "%s: %s" % (_("Error"), alert.msg)
        torrent.set_tracker_status(tracker_status)

    def on_alert_storage_moved(self, alert):
        log.debug("on_alert_storage_moved")
        try:
            torrent = self.torrents[str(alert.handle.info_hash())]
        except:
            return
        torrent.set_save_path(os.path.normpath(alert.handle.save_path()))
        torrent.set_move_completed(False)

    def on_alert_torrent_resumed(self, alert):
        log.debug("on_alert_torrent_resumed")
        try:
            torrent = self.torrents[str(alert.handle.info_hash())]
            torrent_id = str(alert.handle.info_hash())
        except:
            return
        old_state = torrent.state
        torrent.update_state()
        if torrent.state != old_state:
            # We need to emit a TorrentStateChangedEvent too
            component.get("EventManager").emit(TorrentStateChangedEvent(torrent_id, torrent.state))
        component.get("EventManager").emit(TorrentResumedEvent(torrent_id))

    def on_alert_state_changed(self, alert):
        log.debug("on_alert_state_changed")
        try:
            torrent_id = str(alert.handle.info_hash())
            torrent = self.torrents[torrent_id]
        except:
            return

        old_state = torrent.state
        torrent.update_state()

        # Torrent may need to download data after checking.
        if torrent.state in ('Checking', 'Checking Resume Data', 'Downloading'):
            torrent.is_finished = False
            self.queued_torrents.add(torrent_id)

        # Only emit a state changed event if the state has actually changed
        if torrent.state != old_state:
            component.get("EventManager").emit(TorrentStateChangedEvent(torrent_id, torrent.state))

    def on_alert_save_resume_data(self, alert):
        log.debug("on_alert_save_resume_data")
        try:
            torrent_id = str(alert.handle.info_hash())
            torrent = self.torrents[torrent_id]
        except:
            return

        # Libtorrent in add_torrent() expects resume_data to be bencoded
        self.resume_data[torrent_id] = lt.bencode(alert.resume_data)
        self.num_resume_data -= 1

        torrent.waiting_on_resume_data = False

        self.save_resume_data_file()

    def on_alert_save_resume_data_failed(self, alert):
        log.debug("on_alert_save_resume_data_failed: %s", alert.message())
        try:
            torrent = self.torrents[str(alert.handle.info_hash())]
        except:
            return

        self.num_resume_data -= 1
        torrent.waiting_on_resume_data = False

        self.save_resume_data_file()


    def on_alert_file_renamed(self, alert):
        log.debug("on_alert_file_renamed")
        log.debug("index: %s name: %s", alert.index, alert.name.decode("utf8"))
        try:
            torrent = self.torrents[str(alert.handle.info_hash())]
            torrent_id = str(alert.handle.info_hash())
        except:
            return

        # We need to see if this file index is in a waiting_on_folder list
        folder_rename = False
        for i, wait_on_folder in enumerate(torrent.waiting_on_folder_rename):
            if alert.index in wait_on_folder[2]:
                folder_rename = True
                if len(wait_on_folder[2]) == 1:
                    # This is the last alert we were waiting for, time to send signal
                    component.get("EventManager").emit(TorrentFolderRenamedEvent(torrent_id, wait_on_folder[0], wait_on_folder[1]))
                    # Empty folders are removed after libtorrent folder renames
                    self.remove_empty_folders(torrent_id, wait_on_folder[0])
                    del torrent.waiting_on_folder_rename[i]
                    self.save_resume_data((torrent_id,))
                    break
                # This isn't the last file to be renamed in this folder, so just
                # remove the index and continue
                torrent.waiting_on_folder_rename[i][2].remove(alert.index)

        if not folder_rename:
            # This is just a regular file rename so send the signal
            component.get("EventManager").emit(TorrentFileRenamedEvent(torrent_id, alert.index, alert.name))
            self.save_resume_data((torrent_id,))

    def on_alert_metadata_received(self, alert):
        log.debug("on_alert_metadata_received")
        try:
            torrent = self.torrents[str(alert.handle.info_hash())]
        except:
            return
        torrent.write_torrentfile()

    def on_alert_file_error(self, alert):
        log.debug("on_alert_file_error: %s", alert.message())
        try:
            torrent = self.torrents[str(alert.handle.info_hash())]
        except:
            return
        torrent.update_state()

    def on_alert_file_completed(self, alert):
        log.debug("file_completed_alert: %s", alert.message())
        try:
            torrent_id = str(alert.handle.info_hash())
        except:
            return
        component.get("EventManager").emit(
            TorrentFileCompletedEvent(torrent_id, alert.index))
