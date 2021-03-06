import subprocess
import threading
import os
import logging
from os.path import expanduser
from sys import platform
from sys import exit

logger = logging.getLogger(__name__)

class Player(object):
    """ Media player class. Playing is handled by player sub classes """
    process = None

    icy_title_prefix = 'ICY Title: '

    # Input:   old user input     - used to early suppress output
    #                               in case of consecutive equal messages
    # Volume:  old volume input   - used to suppress output (and firing of delay thread)
    #                               in case of consecutive equal volume messages
    # Title:   old title input    - printed by delay thread
    oldUserInput = {'Input': '', 'Volume': '', 'Title': ''}

    """ volume percentage """
    volume = -1
    delay_thread = None
    icy_found = False

    """ vlc only content filtering string """
    vlc_filter = "http stream debug: "

    def __init__(self, outputStream):
        self.outputStream = outputStream

    def __del__(self):
        self.close()

    def save_volume(self):
        pass

    def _do_save_volume(self, config_string):
        ret_string = "Volume: already saved..."
        if self.volume == -1:
            """ inform no change """
            if (logger.isEnabledFor(logging.DEBUG)):
                logger.debug("Volume is -1. Aborting...")
            return ret_string
        else:
            """ change volume """
            if (logger.isEnabledFor(logging.DEBUG)):
                logger.debug("Volume is {}%. Saving...".format(self.volume))
            profile_found = False
            config_file = self.config_files[0]
            ret_string = "Volume: {}% - saved".format(str(self.volume))
            if os.path.exists(config_file):
                if self.PROFILE_FROM_USER:
                    with open(config_file, 'r') as c_file:
                        config_string = c_file.read()

                    if "[pyradio]" in config_string:
                        profile_found = True

                        """ split on [pyradio]
                        last item has our options """
                        sections = config_string.split("[pyradio]")

                        """ split at [ - i.e. isolate consecutive profiles
                        first item has pyradio options """
                        py_section = sections[-1].split("[")

                        """ split to lines in order to get '^volume=' """
                        py_options = py_section[0].split("\n")

                        """ replace volume line """
                        vol_set = False
                        for i, opt in enumerate(py_options):
                            if opt.startswith("volume="):
                                py_options[i]="volume=" + str(self.volume)
                                vol_set = True
                                break
                        """ or add it if it does not exist """
                        if not vol_set:
                            py_options.append("volume=" + str(self.volume))

                        """ join lines together in py_section's first item """
                        py_section[0] = "\n".join(py_options)

                        """ join consecutive profiles (if exist)
                        in last item of sections """
                        if len(py_section) > 1:
                            sections[-1] = "[".join(py_section)
                        else:
                            sections[-1] = py_section[0]

                        """ finally get the string back together """
                        config_string = "[pyradio]".join(sections)

                    try:
                        with open(config_file, "w") as c_file:
                            c_file.write(config_string)
                    except EnvironmentError:
                        if (logger.isEnabledFor(logging.DEBUG)):
                            logger.debug("Error saving file {}".format(config_file))
                        return "Volume: {}% - NOT saved (Error writing file)".format(str(self.volume))
                    self.volume = -1

            """ no user profile or user config file does not exist """
            if not profile_found:
                if not os.path.isdir(os.path.dirname(config_file)):
                    try:
                        os.mkdir(os.path.dirname(config_file))
                    except OSError:
                        if (logger.isEnabledFor(logging.DEBUG)):
                            logger.debug("Error saving file {}".format(config_file))
                        return "Volume: {}% - NOT saved (Error writing file)".format(str(self.volume))
                new_profile_string = "volume=100\n\n" + config_string
                try:
                    with open(config_file, "a") as c_file:
                        c_file.write(new_profile_string.format(str(self.volume)))
                except EnvironmentError:
                    if (logger.isEnabledFor(logging.DEBUG)):
                        logger.debug("Error saving file {}".format(config_file))
                    return "Volume: {}% - NOT saved (Error writing file)".format(str(self.volume))
                self.volume = -1
                self.PROFILE_FROM_USER = True
            return ret_string

    def updateStatus(self):
        if (logger.isEnabledFor(logging.DEBUG)):
            logger.debug("updateStatus thread started.")
        try:
            out = self.process.stdout
            while(True):
                subsystemOut = out.readline().decode("utf-8", "ignore")
                if subsystemOut == '':
                    break
                """ for cvlc we only want web messages """
                if self.PLAYER_CMD == 'cvlc':
                    if (self.vlc_filter not in subsystemOut) and (self.volume_string not in subsystemOut):
                        continue
                subsystemOut = subsystemOut.strip()
                subsystemOut = subsystemOut.replace("\r", "").replace("\n", "")
                if self.oldUserInput['Input'] != subsystemOut:
                    if (logger.isEnabledFor(logging.DEBUG)):
                        logger.debug("User input: {}".format(subsystemOut))
                    self.oldUserInput['Input'] = subsystemOut
                    if self.volume_string in subsystemOut:
                        if self.oldUserInput['Volume'] != subsystemOut:
                            self.oldUserInput['Volume'] = subsystemOut
                            self.volume = ''.join(c for c in subsystemOut if c.isdigit())
                            self.outputStream.write(self.formatVolumeString(subsystemOut))
                            self.threadUpdateTitle()
                    else:
                        # get all input before we get first icy-title
                        if (not self.icy_found):
                            self.oldUserInput['Title'] = subsystemOut
                        # once we get the first icy-title,
                        # get only icy-title entries
                        if self.isIcyEntry(subsystemOut):
                            self.oldUserInput['Title'] = subsystemOut
                            self.icy_found = True

                        # some servers sends first icy-title too early; it gets overwritten once
                        # we get the first, so we block all but icy messages, after the first one
                        # is received (whenever we get an input, we print the previous icy message)
                        if self.icy_found:
                            subsystemOut = self.oldUserInput['Title']

                        # make sure title will not pop-up while Volume value is on
                        if self.delay_thread is None:
                            self.outputStream.write(self.formatTitleString(subsystemOut))
                        else:
                            if (not self.delay_thread.isAlive()):
                                self.outputStream.write(self.formatTitleString(subsystemOut))
        except:
            logger.error("Error in updateStatus thread.",
                         exc_info=True)
        if (logger.isEnabledFor(logging.DEBUG)):
            logger.debug("updateStatus thread stopped.")

    def threadUpdateTitle(self, delay=1):
        if self.oldUserInput['Title'] != '':
            if self.delay_thread is not None:
                if self.delay_thread.isAlive():
                    self.delay_thread.cancel()
            try:
               self.delay_thread = threading.Timer(delay, self.updateTitle,  [ self.outputStream, self.formatTitleString(self.oldUserInput['Title']) ] )
               self.delay_thread.start()
            except:
                if (logger.isEnabledFor(logging.DEBUG)):
                    logger.debug("delay thread start failed")

    def updateTitle(self, *arg, **karg):
        arg[0].write(arg[1])

    def isIcyEntry(self, a_string):
        for a_tokken in self.icy_tokkens:
            if a_string.startswith(a_tokken):
                return True
        return False

    def formatTitleString(self, titleString):
        return titleString

    def formatVolumeString(self, volumeString):
        return volumeString

    def formatTitleString(self, titleString):
        return titleString

    def isPlaying(self):
        return bool(self.process)

    def play(self, streamUrl):
        """ use a multimedia player to play a stream """
        self.close()
        self.oldUserInput = {'Input': '', 'Volume': '', 'Title': ''}
        self.icy_found = False
        opts = []
        isPlayList = streamUrl.split("?")[0][-3:] in ['m3u', 'pls']
        opts = self._buildStartOpts(streamUrl, isPlayList)
        self.process = subprocess.Popen(opts, shell=False,
                                        stdout=subprocess.PIPE,
                                        stdin=subprocess.PIPE,
                                        stderr=subprocess.STDOUT)
        t = threading.Thread(target=self.updateStatus, args=())
        t.start()
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug("Player started")

    def _sendCommand(self, command):
        """ send keystroke command to player """

        if(self.process is not None):
            try:
                if logger.isEnabledFor(logging.DEBUG):
                    logger.debug("Command: {}".format(command).strip())
                self.process.stdin.write(command.encode("utf-8"))
                self.process.stdin.flush()
            except:
                msg = "Error when sending: {}"
                logger.error(msg.format(command).strip(),
                             exc_info=True)

    def close(self):
        """ exit pyradio (and kill player instance) """

        # First close the subprocess
        self._stop()

        # Here is fallback solution and cleanup
        if self.delay_thread is not None:
            self.delay_thread.cancel()
        if self.process is not None:
            os.kill(self.process.pid, 15)
            self.process.wait()
        self.process = None

    def _buildStartOpts(self, streamUrl, playList):
        pass

    def mute(self):
        pass

    def _stop(self):
        pass

    def volumeUp(self):
        pass

    def volumeDown(self):
        pass

class MpvPlayer(Player):
    """Implementation of Player object for MPV"""

    PLAYER_CMD = "mpv"

    """ items of this tupple are considered icy-title
        and get displayed after first icy-title is received """
    icy_tokkens = ('icy-title:', 'Exiting... (Quit)')

    """ USE_PROFILE
    -1 : not checked yet
     0 : do not use
     1 : use profile"""
    USE_PROFILE = -1

    """ True if profile comes from ~/.config/mpv/mpv.conf """
    PROFILE_FROM_USER = False

    """ String to denote volume change """
    volume_string = 'Volume: '

    config_files = [expanduser("~") + "/.config/mpv/mpv.conf"]
    if platform.startswith('darwin'):
        config_files.append("/usr/local/etc/mpv/mpv.conf")
    elif platform.startswith('win'):
        config_files[0] = os.path.join(os.getenv('APPDATA'), "mpv", "mpv.conf")
    else:
        # linux, freebsd, etc.
        config_files.append("/etc/mpv/mpv.conf")

    if os.path.exists('/tmp/mpvsocket'):
        os.system("rm /tmp/mpvsocket");

    def save_volume(self):
        return self._do_save_volume("[pyradio]\nvolume={}\n")

    def _configHasProfile(self):
        """ Checks if mpv config has [pyradio] entry / profile.

        Profile example:

        [pyradio]
        volume-max=300
        volume=50"""

        for i, config_file in enumerate(self.config_files):
            if os.path.exists(config_file):
                with open(config_file) as f:
                    config_string = f.read()
                if "[pyradio]" in config_string:
                    if i == 0:
                        self.PROFILE_FROM_USER = True
                    return 1
        return 0

    def _buildStartOpts(self, streamUrl, playList=False):
        """ Builds the options to pass to subprocess."""

        """ Test for newer MPV versions as it supports different IPC flags. """
        p = subprocess.Popen([self.PLAYER_CMD, "--input-ipc-server"], stdout=subprocess.PIPE, stdin=subprocess.PIPE, shell=False)
        out = p.communicate()
        if "not found" not in str(out[0]):
            if logger.isEnabledFor(logging.DEBUG):
                logger.debug("--input-ipc-server is supported.")
            newerMpv = 1;
        else:
            if logger.isEnabledFor(logging.DEBUG):
                logger.debug("--input-ipc-server is not supported.")
            newerMpv = 0;

        if playList:
            if newerMpv:
                opts = [self.PLAYER_CMD, "--quiet", "--playlist", streamUrl, "--input-ipc-server=/tmp/mpvsocket"]
            else:
                opts = [self.PLAYER_CMD, "--quiet", "--playlist", streamUrl, "--input-unix-socket=/tmp/mpvsocket"]
        else:
            if newerMpv:
                opts = [self.PLAYER_CMD, "--quiet", streamUrl, "--input-ipc-server=/tmp/mpvsocket"]
            else:
                opts = [self.PLAYER_CMD, "--quiet", streamUrl, "--input-unix-socket=/tmp/mpvsocket"]
        if self.USE_PROFILE == -1:
            self.USE_PROFILE = self._configHasProfile()

        if self.USE_PROFILE == 1:
            opts.append("--profile=pyradio")
            if (logger.isEnabledFor(logging.DEBUG)):
                logger.debug("using profile [pyradio]")
        return opts

    def mute(self):
        """ mute mpv """
        os.system("echo 'cycle mute' | socat - /tmp/mpvsocket 2>/dev/null");

    def pause(self):
        """ pause streaming (if possible) """
        os.system("echo 'cycle pause' | socat - /tmp/mpvsocket 2>/dev/null");

    def _stop(self):
        """ exit pyradio (and kill mpv instance) """
        os.system("echo 'quit' | socat - /tmp/mpvsocket 2>/dev/null");
        os.system("rm /tmp/mpvsocket");

    def volumeUp(self):
        """ increase mpv's volume """
        os.system("echo 'cycle volume' | socat - /tmp/mpvsocket 2>/dev/null");

    def volumeDown(self):
        """ decrease mpv's volume """
        os.system("echo 'cycle volume down' | socat - /tmp/mpvsocket 2>/dev/null");

    def formatTitleString(self, titleString):
        return titleString.replace('icy-title: ', self.icy_title_prefix)

class MpPlayer(Player):
    """Implementation of Player object for MPlayer"""

    PLAYER_CMD = "mplayer"

    """ items of this tupple are considered icy-title
        and get displayed after first icy-title is received """
    icy_tokkens = ('ICY Info:', 'Exiting... (Quit)')

    """ USE_PROFILE
    -1 : not checked yet
     0 : do not use
     1 : use profile"""
    USE_PROFILE = -1

    """ True if profile comes from ~/.mplayer/config """
    PROFILE_FROM_USER = False

    """ String to denote volume change """
    volume_string = 'Volume: '

    config_files = [expanduser("~") + "/.mplayer/config"]
    if platform.startswith('darwin'):
        config_files.append("/usr/local/etc/mplayer/mplayer.conf")
    elif platform.startswith('win'):
        config_files[0] = os.path.join(os.getenv('APPDATA'), "mplayer", "config")
    else:
        # linux, freebsd, etc.
        config_files.append("/etc/mplayer/mplayer.conf")

    def save_volume(self):
        return self._do_save_volume("[pyradio]\nvolstep=1\nvolume={}\n")

    def _configHasProfile(self):
        """ Checks if mplayer config has [pyradio] entry / profile.

        Profile example:

        [pyradio]
        volstep=2
        volume=28"""

        for i, config_file in enumerate(self.config_files):
            if os.path.exists(config_file):
                with open(config_file) as f:
                    config_string = f.read()
                if "[pyradio]" in config_string:
                    if i == 0:
                        self.PROFILE_FROM_USER = True
                    return 1
        return 0

    def _buildStartOpts(self, streamUrl, playList=False):
        """ Builds the options to pass to subprocess."""
        if playList:
            opts = [self.PLAYER_CMD, "-quiet", "-playlist", streamUrl]
        else:
            opts = [self.PLAYER_CMD, "-quiet", streamUrl]
        if self.USE_PROFILE == -1:
            self.USE_PROFILE = self._configHasProfile()

        if self.USE_PROFILE == 1:
            opts.append("-profile")
            opts.append("pyradio")
            if (logger.isEnabledFor(logging.DEBUG)):
                logger.debug("using profile [pyradio]")
        return opts

    def mute(self):
        """ mute mplayer """
        self._sendCommand("m")

    def pause(self):
        """ pause streaming (if possible) """
        self._sendCommand("p")

    def _stop(self):
        """ exit pyradio (and kill mplayer instance) """
        self._sendCommand("q")

    def volumeUp(self):
        """ increase mplayer's volume """
        self._sendCommand("*")

    def volumeDown(self):
        """ decrease mplayer's volume """
        self._sendCommand("/")

    def formatTitleString(self, titleString):
        if "StreamTitle='" in titleString:
            tmp = titleString[titleString.find("StreamTitle='"):].replace("StreamTitle='", self.icy_title_prefix)
            return tmp[:tmp.find("';")]
        else:
            return titleString

    def formatVolumeString(self, volumeString):
        return volumeString[volumeString.find(self.volume_string):].replace(' %','%')

class VlcPlayer(Player):
    """Implementation of Player for VLC"""

    PLAYER_CMD = "cvlc"

    """ items of this tupple are considered icy-title
        and get displayed after first icy-title is received """
    icy_tokkens = ('Icy-Title=', 'Exiting... (Quit)')

    muted = False

    """ String to denote volume change """
    volume_string = '( audio volume: '

    """ vlc reports volume in values 0..512 """
    actual_volume = -1
    max_volume = 512

    def save_volume(self):
        pass

    def _buildStartOpts(self, streamUrl, playList=False):
        """ Builds the options to pass to subprocess."""
        #opts = [self.PLAYER_CMD, "-Irc", "--quiet", streamUrl]
        opts = [self.PLAYER_CMD, "-Irc", "-vv", streamUrl]
        return opts

    def mute(self):
        """ mute vlc """

        if not self.muted:
            if self.actual_volume == -1:
                # read actual_volume
                self._sendCommand("voldown 0\n")
            self._sendCommand("volume 0\n")
            self.muted = True
        else:
            self._sendCommand("volume {}\n".format(self.actual_volume))
            self.muted = False

    def pause(self):
        """ pause streaming (if possible) """
        self._sendCommand("stop\n")

    def _stop(self):
        """ exit pyradio (and kill vlc instance) """
        self._sendCommand("shutdown\n")

    def volumeUp(self):
        """ increase vlc's volume """
        self._sendCommand("volup\n")

    def volumeDown(self):
        """ decrease vlc's volume """
        self._sendCommand("voldown\n")

    def formatVolumeString(self, volumeString):
        self.actual_volume = int(volumeString.split(self.volume_string)[1].split(',')[0].split()[0])
        return 'Volume: {}%'.format(int(100 * self.actual_volume / self.max_volume))

    def formatTitleString(self, titleString):
        sp = titleString.split(self.icy_tokkens[0])
        if sp[0] == titleString:
            return titleString
        else:
            return self.icy_title_prefix + sp[1]


def probePlayer(requested_player=''):
    """ Probes the multimedia players which are available on the host
    system."""
    ret_player = None
    if logger.isEnabledFor(logging.DEBUG):
        logger.debug("Probing available multimedia players...")
    implementedPlayers = Player.__subclasses__()
    if logger.isEnabledFor(logging.INFO):
        logger.info("Implemented players: " +
                    ", ".join([player.PLAYER_CMD
                              for player in implementedPlayers]))

    for player in implementedPlayers:
        if requested_player == '':
            ret_player = check_player(player)
            if ret_player is not None:
                break
        else:
            if player.PLAYER_CMD == requested_player:
                ret_player = check_player(player)

    if ret_player is None:
        if requested_player == '':
            logger.error("No supported player found. Terminating...")
        else:
            logger.error('Requested player "' + requested_player + '" not supported. Terminating...')
        exit(1)
    return ret_player

def check_player(a_player):
    try:
        p = subprocess.Popen([a_player.PLAYER_CMD, "--help"],
                             stdout=subprocess.PIPE,
                             stdin=subprocess.PIPE,
                             shell=False)
        p.terminate()
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug("{} supported.".format(str(a_player)))
        return a_player
    except OSError:
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug("{} not supported.".format(str(a_player)))
        return None
