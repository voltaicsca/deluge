#
# torrent.py
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

"""Internal Torrent class"""

import os
import time
import logging
from urllib import unquote
from urlparse import urlparse

from deluge._libtorrent import lt

import deluge.common
import deluge.component as component
from deluge.configmanager import ConfigManager, get_config_dir
from deluge.event import *

TORRENT_STATE = deluge.common.TORRENT_STATE

log = logging.getLogger(__name__)

def sanitize_filepath(filepath, folder=False):
    """
    Returns a sanitized filepath to pass to libotorrent rename_file().
    The filepath will have backslashes substituted along with whitespace
    padding and duplicate slashes stripped. If `folder` is True a trailing
    slash is appended to the returned filepath.
    """
    def clean_filename(filename):
        filename = filename.strip()
        if filename.replace('.', '') == '':
            return ''
        return filename

    if '\\' in filepath or '/' in filepath:
        folderpath = filepath.replace('\\', '/').split('/')
        folderpath = [clean_filename(x) for x in folderpath]
        newfilepath = '/'.join(filter(None, folderpath))
    else:
        newfilepath = clean_filename(filepath)

    if folder is True:
        return newfilepath + '/'
    else:
        return newfilepath

class TorrentOptions(dict):
    def __init__(self):
        config = ConfigManager("core.conf").config
        options_conf_map = {
            "max_connections": "max_connections_per_torrent",
            "max_upload_slots": "max_upload_slots_per_torrent",
            "max_upload_speed": "max_upload_speed_per_torrent",
            "max_download_speed": "max_download_speed_per_torrent",
            "prioritize_first_last_pieces": "prioritize_first_last_pieces",
            "sequential_download": "sequential_download",
            "compact_allocation": "compact_allocation",
            "download_location": "download_location",
            "auto_managed": "auto_managed",
            "stop_at_ratio": "stop_seed_at_ratio",
            "stop_ratio": "stop_seed_ratio",
            "remove_at_ratio": "remove_seed_at_ratio",
            "move_completed": "move_completed",
            "move_completed_path": "move_completed_path",
            "add_paused": "add_paused",
            "shared": "shared"
        }
        for opt_k, conf_k in options_conf_map.iteritems():
            self[opt_k] = config[conf_k]
        self["file_priorities"] = []
        self["mapped_files"] = {}

class Torrent(object):
    """Torrent holds information about torrents added to the libtorrent session.
    """
    def __init__(self, handle, options, state=None, filename=None, magnet=None, owner=None):
        log.debug("Creating torrent object %s", str(handle.info_hash()))
        # Get the core config
        self.config = ConfigManager("core.conf")

        self.rpcserver = component.get("RPCServer")

        # This dict holds previous status dicts returned for this torrent
        # We use this to return dicts that only contain changes from the previous
        # {session_id: status_dict, ...}
        self.prev_status = {}
        from twisted.internet.task import LoopingCall
        self.prev_status_cleanup_loop = LoopingCall(self.cleanup_prev_status)
        self.prev_status_cleanup_loop.start(10)

        # Set the libtorrent handle
        self.handle = handle
        # Set the torrent_id for this torrent
        self.torrent_id = str(handle.info_hash())

        # Let's us know if we're waiting on a lt alert
        self.waiting_on_resume_data = False

        # Keep a list of file indexes we're waiting for file_rename alerts on
        # This also includes the old_folder and new_folder to know what signal to send
        # This is so we can send one folder_renamed signal instead of multiple
        # file_renamed signals.
        # [(old_folder, new_folder, [*indexes]), ...]
        self.waiting_on_folder_rename = []

        # We store the filename just in case we need to make a copy of the torrentfile
        if not filename:
            # If no filename was provided, then just use the infohash
            filename = self.torrent_id

        self.filename = filename

        # Store the magnet uri used to add this torrent if available
        self.magnet = magnet

        # Holds status info so that we don't need to keep getting it from lt
        self.status = self.handle.status()

        try:
            self.torrent_info = self.handle.get_torrent_info()
        except RuntimeError:
            self.torrent_info = None

        # Default total_uploaded to 0, this may be changed by the state
        self.total_uploaded = 0

        # Set the default options
        self.options = TorrentOptions()
        self.options.update(options)

        # We need to keep track if the torrent is finished in the state to prevent
        # some weird things on state load.
        self.is_finished = False

        # Load values from state if we have it
        if state:
            # This is for saving the total uploaded between sessions
            self.total_uploaded = state.total_uploaded
            # Set the trackers
            self.set_trackers(state.trackers)
            # Set the filename
            self.filename = state.filename
            self.is_finished = state.is_finished
        else:
            # Tracker list
            self.trackers = []
            # Create a list of trackers
            for value in self.handle.trackers():
                if lt.version_minor < 15:
                    tracker = {}
                    tracker["url"] = value.url
                    tracker["tier"] = value.tier
                else:
                    tracker = value
                self.trackers.append(tracker)

        # Various torrent options
        self.handle.resolve_countries(True)

        self.set_options(self.options)

        # Status message holds error info about the torrent
        self.statusmsg = "OK"

        # The torrents state
        self.update_state()

        # The tracker status
        self.tracker_status = ""

        # This gets updated when get_tracker_host is called
        self.tracker_host = None

        if state:
            self.time_added = state.time_added
        else:
            self.time_added = time.time()

        # Keep track of the owner
        if state:
            self.owner = state.owner
        else:
            self.owner = owner

        # Keep track of last seen complete
        if state:
            self._last_seen_complete = state.last_seen_complete or 0.0
        else:
            self._last_seen_complete = 0.0

        # Keep track if we're forcing a recheck of the torrent so that we can
        # re-pause it after its done if necessary
        self.forcing_recheck = False
        self.forcing_recheck_paused = False

        log.debug("Torrent object created.")

    ## Options methods ##
    def set_options(self, options):
        OPTIONS_FUNCS = {
            # Functions used for setting options
            "auto_managed": self.set_auto_managed,
            "download_location": self.set_save_path,
            "file_priorities": self.set_file_priorities,
            "max_connections": self.handle.set_max_connections,
            "max_download_speed": self.set_max_download_speed,
            "max_upload_slots": self.handle.set_max_uploads,
            "max_upload_speed": self.set_max_upload_speed,
            "prioritize_first_last_pieces": self.set_prioritize_first_last,
            "sequential_download": self.set_sequential_download

        }
        for (key, value) in options.items():
            if OPTIONS_FUNCS.has_key(key):
                OPTIONS_FUNCS[key](value)
        self.options.update(options)

    def get_options(self):
        return self.options

    def get_name(self):
        if self.handle.has_metadata():
            name = self.torrent_info.file_at(0).path.split("/", 1)[0]
            if not name:
                name = self.torrent_info.name()
            try:
                return name.decode("utf8", "ignore")
            except UnicodeDecodeError:
                return name
        elif self.magnet:
            try:
                keys = dict([k.split('=') for k in self.magnet.split('?')[-1].split('&')])
                name = keys.get('dn')
                if not name:
                    return self.torrent_id
                name = unquote(name).replace('+', ' ')
                try:
                    return name.decode("utf8", "ignore")
                except UnicodeDecodeError:
                    return name
            except:
                pass
        return self.torrent_id

    def set_owner(self, account):
        self.owner = account

    def set_max_connections(self, max_connections):
        self.options["max_connections"] = int(max_connections)
        self.handle.set_max_connections(max_connections)

    def set_max_upload_slots(self, max_slots):
        self.options["max_upload_slots"] = int(max_slots)
        self.handle.set_max_uploads(max_slots)

    def set_max_upload_speed(self, m_up_speed):
        self.options["max_upload_speed"] = m_up_speed
        if m_up_speed < 0:
            v = -1
        else:
            v = int(m_up_speed * 1024)

        self.handle.set_upload_limit(v)

    def set_max_download_speed(self, m_down_speed):
        self.options["max_download_speed"] = m_down_speed
        if m_down_speed < 0:
            v = -1
        else:
            v = int(m_down_speed * 1024)
        self.handle.set_download_limit(v)

    def set_prioritize_first_last(self, prioritize):
        self.options["prioritize_first_last_pieces"] = prioritize
        if self.handle.has_metadata():
            if self.options["compact_allocation"]:
                log.debug("Setting first/last priority with compact "
                          "allocation does not work!")
                return

            paths = {}
            ti = self.handle.get_torrent_info()
            for n in range(ti.num_pieces()):
                slices = ti.map_block(n, 0, ti.piece_size(n))
                for slice in slices:
                    fe = ti.file_at(slice.file_index)
                    paths.setdefault(fe.path, []).append(n)

            priorities = self.handle.piece_priorities()
            for pieces in paths.itervalues():
                two_percent = 2*100/len(pieces)
                for piece in pieces[:two_percent]+pieces[-two_percent:]:
                    priorities[piece] = prioritize and 7 or 1
            self.handle.prioritize_pieces(priorities)

    def set_sequential_download(self, set_sequencial):
        self.options["sequential_download"] = set_sequencial
        self.handle.set_sequential_download(set_sequencial)

    def set_auto_managed(self, auto_managed):
        self.options["auto_managed"] = auto_managed
        if not (self.handle.is_paused() and not self.handle.is_auto_managed()):
            self.handle.auto_managed(auto_managed)
            self.update_state()

    def set_stop_ratio(self, stop_ratio):
        self.options["stop_ratio"] = stop_ratio

    def set_stop_at_ratio(self, stop_at_ratio):
        self.options["stop_at_ratio"] = stop_at_ratio

    def set_remove_at_ratio(self, remove_at_ratio):
        self.options["remove_at_ratio"] = remove_at_ratio

    def set_move_completed(self, move_completed):
        self.options["move_completed"] = move_completed

    def set_move_completed_path(self, move_completed_path):
        self.options["move_completed_path"] = move_completed_path

    def set_file_priorities(self, file_priorities):
        if len(file_priorities) != len(self.get_files()):
            log.debug("file_priorities len != num_files")
            self.options["file_priorities"] = self.handle.file_priorities()
            return

        if self.options["compact_allocation"]:
            log.debug("setting file priority with compact allocation does not work!")
            self.options["file_priorities"] = self.handle.file_priorities()
            return

        log.debug("setting %s's file priorities: %s", self.torrent_id, file_priorities)

        self.handle.prioritize_files(file_priorities)

        if 0 in self.options["file_priorities"]:
            # We have previously marked a file 'Do Not Download'
            # Check to see if we have changed any 0's to >0 and change state accordingly
            for index, priority in enumerate(self.options["file_priorities"]):
                if priority == 0 and file_priorities[index] > 0:
                    # We have a changed 'Do Not Download' to a download priority
                    self.is_finished = False
                    self.update_state()
                    break

        self.options["file_priorities"] = self.handle.file_priorities()
        if self.options["file_priorities"] != list(file_priorities):
            log.warning("File priorities were not set for this torrent")

        # Set the first/last priorities if needed
        self.set_prioritize_first_last(self.options["prioritize_first_last_pieces"])

    def set_trackers(self, trackers):
        """Sets trackers"""
        if trackers == None:
            trackers = []
            for value in self.handle.trackers():
                tracker = {}
                tracker["url"] = value.url
                tracker["tier"] = value.tier
                trackers.append(tracker)
            self.trackers = trackers
            self.tracker_host = None
            return

        log.debug("Setting trackers for %s: %s", self.torrent_id, trackers)
        tracker_list = []

        for tracker in trackers:
            new_entry = lt.announce_entry(str(tracker["url"]))
            new_entry.tier = tracker["tier"]
            tracker_list.append(new_entry)
        self.handle.replace_trackers(tracker_list)

        # Print out the trackers
        #for t in self.handle.trackers():
        #    log.debug("tier: %s tracker: %s", t["tier"], t["url"])
        # Set the tracker list in the torrent object
        self.trackers = trackers
        if len(trackers) > 0:
            # Force a re-announce if there is at least 1 tracker
            self.force_reannounce()

        self.tracker_host = None

    ### End Options methods ###

    def set_save_path(self, save_path):
        self.options["download_location"] = save_path

    def set_tracker_status(self, status):
        """Sets the tracker status"""
        self.tracker_status = self.get_tracker_host() + ": " + status

    def update_state(self):
        """Updates the state based on what libtorrent's state for the torrent is"""
        # Set the initial state based on the lt state
        LTSTATE = deluge.common.LT_TORRENT_STATE
        ltstate = int(self.handle.status().state)

        # Set self.state to the ltstate right away just incase we don't hit some
        # of the logic below
        if ltstate in LTSTATE:
            self.state = LTSTATE[ltstate]
        else:
            self.state = str(ltstate)

        log.debug("set_state_based_on_ltstate: %s", deluge.common.LT_TORRENT_STATE[ltstate])
        log.debug("session.is_paused: %s", component.get("Core").session.is_paused())

        # First we check for an error from libtorrent, and set the state to that
        # if any occurred.
        if len(self.handle.status().error) > 0:
            # This is an error'd torrent
            self.state = "Error"
            self.set_status_message(self.handle.status().error)
            if self.handle.is_paused():
                self.handle.auto_managed(False)
            return

        if ltstate == LTSTATE["Queued"] or ltstate == LTSTATE["Checking"]:
            if self.handle.is_paused():
                self.state = "Paused"
            else:
                self.state = "Checking"
            return
        elif ltstate == LTSTATE["Downloading"] or ltstate == LTSTATE["Downloading Metadata"]:
            self.state = "Downloading"
        elif ltstate == LTSTATE["Finished"] or ltstate == LTSTATE["Seeding"]:
            self.state = "Seeding"
        elif ltstate == LTSTATE["Allocating"]:
            self.state = "Allocating"

        if self.handle.is_paused() and self.handle.is_auto_managed() and not component.get("Core").session.is_paused():
            self.state = "Queued"
        elif component.get("Core").session.is_paused() or (self.handle.is_paused() and not self.handle.is_auto_managed()):
            self.state = "Paused"

    def set_state(self, state):
        """Accepts state strings, ie, "Paused", "Seeding", etc."""
        if state not in TORRENT_STATE:
            log.debug("Trying to set an invalid state %s", state)
            return

        self.state = state
        return

    def set_status_message(self, message):
        self.statusmsg = message

    def get_eta(self):
        """Returns the ETA in seconds for this torrent"""
        if self.status == None:
            status = self.handle.status()
        else:
            status = self.status

        if self.is_finished and self.options["stop_at_ratio"]:
            # We're a seed, so calculate the time to the 'stop_share_ratio'
            if not status.upload_payload_rate:
                return 0
            stop_ratio = self.options["stop_ratio"]
            return ((status.all_time_download * stop_ratio) - status.all_time_upload) / status.upload_payload_rate

        left = status.total_wanted - status.total_wanted_done

        if left <= 0 or status.download_payload_rate == 0:
            return 0

        try:
            eta = left / status.download_payload_rate
        except ZeroDivisionError:
            eta = 0

        return eta

    def get_ratio(self):
        """Returns the ratio for this torrent"""
        if self.status == None:
            status = self.handle.status()
        else:
            status = self.status

        if status.total_done > 0:
            # We use 'total_done' if the downloaded value is 0
            downloaded = status.total_done
        else:
            # Return -1.0 to signify infinity
            return -1.0

        return float(status.all_time_upload) / float(downloaded)

    def get_files(self):
        """Returns a list of files this torrent contains"""
        if self.torrent_info == None and self.handle.has_metadata():
            torrent_info = self.handle.get_torrent_info()
        else:
            torrent_info = self.torrent_info

        if not torrent_info:
            return []

        ret = []
        files = torrent_info.files()
        for index, file in enumerate(files):
            ret.append({
                'index': index,
                'path': file.path.decode("utf8", "ignore"),
                'size': file.size,
                'offset': file.offset
            })
        return ret

    def get_peers(self):
        """Returns a list of peers and various information about them"""
        ret = []
        peers = self.handle.get_peer_info()

        for peer in peers:
            # We do not want to report peers that are half-connected
            if peer.flags & peer.connecting or peer.flags & peer.handshake:
                continue
            try:
                client = str(peer.client).decode("utf-8")
            except UnicodeDecodeError:
                client = str(peer.client).decode("latin-1")

            # Make country a proper string
            country = str()
            for c in peer.country:
                if not c.isalpha():
                    country += " "
                else:
                    country += c

            ret.append({
                "client": client,
                "country": country,
                "down_speed": peer.payload_down_speed,
                "ip": "%s:%s" % (peer.ip[0], peer.ip[1]),
                "progress": peer.progress,
                "seed": peer.flags & peer.seed,
                "up_speed": peer.payload_up_speed,
            })

        return ret

    def get_queue_position(self):
        """Returns the torrents queue position"""
        return self.handle.queue_position()

    def get_file_progress(self):
        """Returns the file progress as a list of floats.. 0.0 -> 1.0"""
        if not self.handle.has_metadata():
            return 0.0

        file_progress = self.handle.file_progress()
        ret = []
        for i,f in enumerate(self.get_files()):
            try:
                ret.append(float(file_progress[i]) / float(f["size"]))
            except ZeroDivisionError:
                ret.append(0.0)

        return ret

    def get_tracker_host(self):
        """Returns just the hostname of the currently connected tracker
        if no tracker is connected, it uses the 1st tracker."""
        if self.tracker_host:
            return self.tracker_host

        if not self.status:
            self.status = self.handle.status()

        tracker = self.status.current_tracker
        if not tracker and self.trackers:
            tracker = self.trackers[0]["url"]

        if tracker:
            url = urlparse(tracker.replace("udp://", "http://"))
            if hasattr(url, "hostname"):
                host = (url.hostname or 'DHT')
                # Check if hostname is an IP address and just return it if that's the case
                import socket
                try:
                    socket.inet_aton(host)
                except socket.error:
                    pass
                else:
                    # This is an IP address because an exception wasn't raised
                    return url.hostname

                parts = host.split(".")
                if len(parts) > 2:
                    if parts[-2] in ("co", "com", "net", "org") or parts[-1] in ("uk"):
                        host = ".".join(parts[-3:])
                    else:
                        host = ".".join(parts[-2:])
                self.tracker_host = host
                return host
        return ""

    def get_last_seen_complete(self):
        """
        Returns the time a torrent was last seen complete, ie, with all pieces
        available.
        """
        if lt.version_minor > 15:
            return self.status.last_seen_complete
        self.calculate_last_seen_complete()
        return self._last_seen_complete

    def get_status(self, keys, diff=False):
        """
        Returns the status of the torrent based on the keys provided

        :param keys: the keys to get the status on
        :type keys: list of str
        :param diff: if True, will return a diff of the changes since the last
        call to get_status based on the session_id
        :type diff: bool

        :returns: a dictionary of the status keys and their values
        :rtype: dict

        """

        # Create the full dictionary
        self.status = self.handle.status()
        if self.handle.has_metadata():
            self.torrent_info = self.handle.get_torrent_info()

        # Adjust progress to be 0-100 value
        progress = self.status.progress * 100

        # Adjust status.distributed_copies to return a non-negative value
        distributed_copies = self.status.distributed_copies
        if distributed_copies < 0:
            distributed_copies = 0.0

        # Calculate the seeds:peers ratio
        if self.status.num_incomplete == 0:
            # Use -1.0 to signify infinity
            seeds_peers_ratio = -1.0
        else:
            seeds_peers_ratio = self.status.num_complete / float(self.status.num_incomplete)

        full_status = {
            "active_time": self.status.active_time,
            "all_time_download": self.status.all_time_download,
            "compact": self.options["compact_allocation"],
            "distributed_copies": distributed_copies,
            "download_payload_rate": self.status.download_payload_rate,
            "file_priorities": self.options["file_priorities"],
            "hash": self.torrent_id,
            "is_auto_managed": self.options["auto_managed"],
            "is_finished": self.is_finished,
            "max_connections": self.options["max_connections"],
            "max_download_speed": self.options["max_download_speed"],
            "max_upload_slots": self.options["max_upload_slots"],
            "max_upload_speed": self.options["max_upload_speed"],
            "message": self.statusmsg,
            "move_on_completed_path": self.options["move_completed_path"],
            "move_on_completed": self.options["move_completed"],
            "move_completed_path": self.options["move_completed_path"],
            "move_completed": self.options["move_completed"],
            "next_announce": self.status.next_announce.seconds,
            "num_peers": self.status.num_peers - self.status.num_seeds,
            "num_seeds": self.status.num_seeds,
            "owner": self.owner,
            "paused": self.status.paused,
            "prioritize_first_last": self.options["prioritize_first_last_pieces"],
            "sequential_download": self.options["sequential_download"],
            "progress": progress,
            "shared": self.options["shared"],
            "remove_at_ratio": self.options["remove_at_ratio"],
            "save_path": self.options["download_location"],
            "seeding_time": self.status.seeding_time,
            "seeds_peers_ratio": seeds_peers_ratio,
            "seed_rank": self.status.seed_rank,
            "state": self.state,
            "stop_at_ratio": self.options["stop_at_ratio"],
            "stop_ratio": self.options["stop_ratio"],
            "time_added": self.time_added,
            "total_done": self.status.total_done,
            "total_payload_download": self.status.total_payload_download,
            "total_payload_upload": self.status.total_payload_upload,
            "total_peers": self.status.num_incomplete,
            "total_seeds":  self.status.num_complete,
            "total_uploaded": self.status.all_time_upload,
            "total_wanted": self.status.total_wanted,
            "tracker": self.status.current_tracker,
            "trackers": self.trackers,
            "tracker_status": self.tracker_status,
            "upload_payload_rate": self.status.upload_payload_rate
        }

        def ti_comment():
            if self.handle.has_metadata():
                try:
                    return self.torrent_info.comment().decode("utf8", "ignore")
                except UnicodeDecodeError:
                    return self.torrent_info.comment()
            return ""

        def ti_priv():
            if self.handle.has_metadata():
                return self.torrent_info.priv()
            return False
        def ti_total_size():
            if self.handle.has_metadata():
                return self.torrent_info.total_size()
            return 0
        def ti_num_files():
            if self.handle.has_metadata():
                return self.torrent_info.num_files()
            return 0
        def ti_num_pieces():
            if self.handle.has_metadata():
                return self.torrent_info.num_pieces()
            return 0
        def ti_piece_length():
            if self.handle.has_metadata():
                return self.torrent_info.piece_length()
            return 0
        def ti_pieces_info():
            if self.handle.has_metadata():
                return self.get_pieces_info()
            return None

        fns = {
            "comment": ti_comment,
            "eta": self.get_eta,
            "file_progress": self.get_file_progress,
            "files": self.get_files,
            "is_seed": self.handle.is_seed,
            "name": self.get_name,
            "num_files": ti_num_files,
            "num_pieces": ti_num_pieces,
            "pieces": ti_pieces_info,
            "peers": self.get_peers,
            "piece_length": ti_piece_length,
            "private": ti_priv,
            "queue": self.handle.queue_position,
            "ratio": self.get_ratio,
            "total_size": ti_total_size,
            "tracker_host": self.get_tracker_host,
            "last_seen_complete": self.get_last_seen_complete
        }

        # Create the desired status dictionary and return it
        status_dict = {}

        if len(keys) == 0:
            status_dict = full_status
            for key in fns:
                status_dict[key] = fns[key]()
        else:
            for key in keys:
                if key in full_status:
                    status_dict[key] = full_status[key]
                elif key in fns:
                    status_dict[key] = fns[key]()

        session_id = self.rpcserver.get_session_id()
        if diff:
            if session_id in self.prev_status:
                # We have a previous status dict, so lets make a diff
                status_diff = {}
                for key, value in status_dict.items():
                    if key in self.prev_status[session_id]:
                        if value != self.prev_status[session_id][key]:
                            status_diff[key] = value
                    else:
                        status_diff[key] = value

                self.prev_status[session_id] = status_dict
                return status_diff

            self.prev_status[session_id] = status_dict
            return status_dict

        return status_dict

    def apply_options(self):
        """Applies the per-torrent options that are set."""
        self.handle.set_max_connections(self.max_connections)
        self.handle.set_max_uploads(self.max_upload_slots)
        self.handle.set_upload_limit(int(self.max_upload_speed * 1024))
        self.handle.set_download_limit(int(self.max_download_speed * 1024))
        self.handle.prioritize_files(self.file_priorities)
        self.handle.set_sequential_download(self.options["sequential_download"])
        self.handle.resolve_countries(True)

    def pause(self):
        """Pause this torrent"""
        # Turn off auto-management so the torrent will not be unpaused by lt queueing
        self.handle.auto_managed(False)
        if self.handle.is_paused():
            # This torrent was probably paused due to being auto managed by lt
            # Since we turned auto_managed off, we should update the state which should
            # show it as 'Paused'.  We need to emit a torrent_paused signal because
            # the torrent_paused alert from libtorrent will not be generated.
            self.update_state()
            component.get("EventManager").emit(TorrentStateChangedEvent(self.torrent_id, "Paused"))
        else:
            try:
                self.handle.pause()
            except Exception, e:
                log.debug("Unable to pause torrent: %s", e)
                return False

        return True

    def resume(self):
        """Resumes this torrent"""

        if self.handle.is_paused() and self.handle.is_auto_managed():
            log.debug("Torrent is being auto-managed, cannot resume!")
            return
        else:
            # Reset the status message just in case of resuming an Error'd torrent
            self.set_status_message("OK")

            if self.handle.is_finished():
                # If the torrent has already reached it's 'stop_seed_ratio' then do not do anything
                if self.options["stop_at_ratio"]:
                    if self.get_ratio() >= self.options["stop_ratio"]:
                        #XXX: This should just be returned in the RPC Response, no event
                        #self.signals.emit_event("torrent_resume_at_stop_ratio")
                        return

            if self.options["auto_managed"]:
                # This torrent is to be auto-managed by lt queueing
                self.handle.auto_managed(True)

            try:
                self.handle.resume()
            except:
                pass

            return True

    def connect_peer(self, ip, port):
        """adds manual peer"""
        try:
            self.handle.connect_peer((ip, int(port)), 0)
        except Exception, e:
            log.debug("Unable to connect to peer: %s", e)
            return False
        return True

    def move_storage(self, dest):
        """Move a torrent's storage location"""

        if deluge.common.windows_check():
            # Attempt to convert utf8 path to unicode
            # Note: Inconsistent encoding for 'dest', needs future investigation
            try:
                dest_u = unicode(dest, "utf-8")
            except TypeError:
                # String is already unicode
                dest_u = dest
        else:
            dest_u = dest
            
        if not os.path.exists(dest_u):
            try:
                # Try to make the destination path if it doesn't exist
                os.makedirs(dest_u)
            except IOError, e:
                log.exception(e)
                log.error("Could not move storage for torrent %s since %s does "
                          "not exist and could not create the directory.",
                          self.torrent_id, dest_u)
                return False
        try:
            self.handle.move_storage(dest_u)
        except:
            return False

        return True

    def save_resume_data(self):
        """Signals libtorrent to build resume data for this torrent, it gets
        returned in a libtorrent alert"""
        self.handle.save_resume_data()
        self.waiting_on_resume_data = True

    def write_torrentfile(self):
        """Writes the torrent file"""
        path = "%s/%s.torrent" % (
            os.path.join(get_config_dir(), "state"),
            self.torrent_id)
        log.debug("Writing torrent file: %s", path)
        try:
            self.torrent_info = self.handle.get_torrent_info()
            # Regenerate the file priorities
            self.set_file_priorities([])
            md = lt.bdecode(self.torrent_info.metadata())
            torrent_file = {}
            torrent_file["info"] = md
            open(path, "wb").write(lt.bencode(torrent_file))
        except Exception, e:
            log.warning("Unable to save torrent file: %s", e)

    def delete_torrentfile(self):
        """Deletes the .torrent file in the state"""
        path = "%s/%s.torrent" % (
            os.path.join(get_config_dir(), "state"),
            self.torrent_id)
        log.debug("Deleting torrent file: %s", path)
        try:
            os.remove(path)
        except Exception, e:
            log.warning("Unable to delete the torrent file: %s", e)

    def force_reannounce(self):
        """Force a tracker reannounce"""
        try:
            self.handle.force_reannounce()
        except Exception, e:
            log.debug("Unable to force reannounce: %s", e)
            return False

        return True

    def scrape_tracker(self):
        """Scrape the tracker"""
        try:
            self.handle.scrape_tracker()
        except Exception, e:
            log.debug("Unable to scrape tracker: %s", e)
            return False

        return True

    def force_recheck(self):
        """Forces a recheck of the torrents pieces"""
        paused = self.handle.is_paused()
        try:
            self.handle.force_recheck()
            self.handle.resume()
        except Exception, e:
            log.debug("Unable to force recheck: %s", e)
            return False
        self.forcing_recheck = True
        self.forcing_recheck_paused = paused
        return True

    def rename_files(self, filenames):
        """Renames files in the torrent. 'filenames' should be a list of
        (index, filename) pairs."""
        for index, filename in filenames:
            filename = sanitize_filepath(filename)
            self.handle.rename_file(index, filename.encode("utf-8"))

    def rename_folder(self, folder, new_folder):
        """Renames a folder within a torrent.  This basically does a file rename
        on all of the folders children."""
        log.debug("attempting to rename folder: %s to %s", folder, new_folder)
        if len(new_folder) < 1:
            log.error("Attempting to rename a folder with an invalid folder name: %s", new_folder)
            return

        new_folder = sanitize_filepath(new_folder, folder=True)

        wait_on_folder = (folder, new_folder, [])
        for f in self.get_files():
            if f["path"].startswith(folder):
                # Keep a list of filerenames we're waiting on
                wait_on_folder[2].append(f["index"])
                self.handle.rename_file(f["index"], f["path"].replace(folder, new_folder, 1).encode("utf-8"))
        self.waiting_on_folder_rename.append(wait_on_folder)

    def cleanup_prev_status(self):
        """
        This method gets called to check the validity of the keys in the prev_status
        dict.  If the key is no longer valid, the dict will be deleted.

        """
        for key in self.prev_status.keys():
            if not self.rpcserver.is_session_valid(key):
                del self.prev_status[key]

    def calculate_last_seen_complete(self):
        if self._last_seen_complete+60 > time.time():
            # Simple caching. Only calculate every 1 min at minimum
            return self._last_seen_complete

        availability = self.handle.piece_availability()
        if filter(lambda x: x<1, availability):
            # Torrent does not have all the pieces
            return
        log.trace("Torrent %s has all the pieces. Setting last seen complete.",
                  self.torrent_id)
        self._last_seen_complete = time.time()

    def get_pieces_info(self):
        pieces = {}
        # First get the pieces availability.
        availability = self.handle.piece_availability()
        # Pieces from connected peers
        for peer_info in self.handle.get_peer_info():
            if peer_info.downloading_piece_index < 0:
                # No piece index, then we're not downloading anything from
                # this peer
                continue
            pieces[peer_info.downloading_piece_index] = 2

        # Now, the rest of the pieces
        for idx, piece in enumerate(self.handle.status().pieces):
            if idx in pieces:
                # Piece beeing downloaded, handled above
                continue
            elif piece:
                # Completed Piece
                pieces[idx] = 3
                continue
            elif availability[idx] > 0:
                # Piece not downloaded nor beeing downloaded but available
                pieces[idx] = 1
                continue
            # If we reached here, it means the piece is missing, ie, there's
            # no known peer with this piece, or this piece has not been asked
            # for so far.
            pieces[idx] = 0

        sorted_indexes = pieces.keys()
        sorted_indexes.sort()
        # Return only the piece states, no need for the piece index
        # Keep the order
        return [pieces[idx] for idx in sorted_indexes]
