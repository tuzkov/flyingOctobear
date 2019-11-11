# coding=utf-8
from __future__ import absolute_import
import re
import requests
import logging
import threading
import time
import sys
# #from enum import IntEnum
from datetime import datetime

import octoprint.plugin
import octoprint.printer
from octoprint.settings import settings
from octoprint.filemanager import FileDestinations
import octoprint.util
from octoprint.events import eventManager, Events


#class ERROR_CODE(IntEnum):
#    OK = 0
 #   TIMEOUT = 1
  #  BUSY = 2
   # PARA_TOO_LONG = 3
    #MISSING_PARAMETERS = 4
#    FAILED = 5
 #   UPLOAD_FAILED_ON_PRINTTING = 6
  #  UPLOAD_FAILED_DISK_ERROR = 7
   # USER_CANCELED = 8
    #UNKNOW = 9
    #UPLOAD_FAILED_SIZE_NOT_MATCH = 10


class Ghost4Printer(octoprint.printer.PrinterInterface):
    """
            Implementation of octoprint.printer.PrinterInterface
    """

    def __init__(self, host_get=None, logger=None):
        self._logger = logger if logger is not None else logging.getLogger(
            "octoprint.plugins.flyingOctobear")

        self.host_get = host_get
        self.session_id = -1
        self.port = None
        self.baudrate = None
        self.printer_profile = None

        """Valid axes identifiers."""
        self.valid_axes = ("x", "y", "z", "e")

        """Regex for valid tool identifiers."""
        self.valid_tool_regex = re.compile(r"^(tool\d+)$")

        """Regex for valid heater identifiers."""
        self.valid_heater_regex = re.compile(r"^(tool\d+|bed|chamber)$")

        self._callbacks = []
        self._state = "CLOSED"
        self._state_error = None

        self._print_started = None
        self._work = True

        self._selected_file_lock = threading.Lock()
        self._selected_file = None

        self._control_lock = threading.Lock()
        self._failed_attempts = 0

        self._data_lock = threading.Lock()
        self._data = dict(state=dict(text=self.get_state_string(), flags=self._getStateFlags()),
                          job=dict(file=dict(name=None,
                                             path=None,
                                             size=None,
                                             origin=None,
                                             date=None),
                                   estimatedPrintTime=None,
                                   lastPrintTime=None,
                                   filament=dict(length=None,
                                                 volume=None),
                                   user=None),
                          progress=dict(completion=None,
                                        filepos=None,
                                        printTime=None,
                                        printTimeLeft=None,
                                        printTimeOrigin=None),
                          currentZ=None,
                          offsets=dict()
                          )
        self._worker = threading.Thread(target=self._work_updates)
        self._worker.daemon = True
        self._worker.start()

        self._current_tool = "tool0"
        self._current_temp_lock = threading.Lock()
        self._current_temperature = dict(
            tool0=dict(
                actual=None,
                target=None,
            ),
            bed=dict(
                actual=None,
                target=None,
            )
        )


    @classmethod
    def get_connection_options(cls, *args, **kwargs):
        """
        Retrieves the available ports, baudrates, preferred port and baudrate for connecting to the printer.

        Returned ``dict`` has the following structure::

                        ports: <list of available serial ports>
                        baudrates: <list of available baudrates>
                        portPreference: <configured default serial port>
                        baudratePreference: <configured default baudrate>
                        autoconnect: <whether autoconnect upon server startup is enabled or not>

        Returns:
                        (dict): A dictionary holding the connection options in the structure specified above
        """
        return {
            "ports": ["not used"],
            "baudrates": ["not used"],
            "portPreference": "not used",
            "baudratePreference": "not used",
            "autoconnect": settings().getBoolean(["serial", "autoconnect"])
        }

    def host(self):
        host = self.host_get()
        if not host:
            raise Exception(
                "Printer host is not specified. Please input it into settings")
        return host

    def _is_connected(self):
        return self.session_id != -1

    def connect(self, port=None, baudrate=None, profile=None, *args, **kwargs):
        """
        Connects to the printer, using the specified serial ``port``, ``baudrate`` and printer ``profile``. If a
        connection is already established, that connection will be closed prior to connecting anew with the provided
        parameters.

        Arguments:
                        port (str): Name of the serial port to connect to. If not provided, an auto detection will be attempted.
                        baudrate (int): Baudrate to connect with. If not provided, an auto detection will be attempted.
                        profile (str): Name of the printer profile to use for this connection. If not provided, the default
                                        will be retrieved from the :class:`PrinterProfileManager`.
        """
        if self._is_connected():
            self.disconnect()

        eventManager().fire(Events.CONNECTING)
        self._set_state("CONNECTING")

        self.port = port
        self.baudrate = baudrate
        self.printer_profile = profile

        try:
            resp = self.make_request("connect")
        except Exception as e:
            self._set_error_state("Fail to connect to printer: {}".format(e))
            self._logger.error("Fail to connect to printer",
                               exc_info=sys.exc_info())
            return

        if resp.status_code != 200:
            self._set_error_state(
                "Printer response invalid: status code is not 200")
            self._logger.error(
                "Printer response invalid: status code is {}".format(resp.status_code))
            return

        try:
            j = resp.json()
            if j["c"] != 0:
                self._set_error_state(
                    "Printer response invalid: code invalid, have {} want {}".format(j["c"], 0))
                self._logger.error("Printer response invalid: code invalid, have {} want {}\n".format(
                    j["c"], 0)+octoprint.util.get_exception_string())
            session_id = j["m"]
        except:
            self._set_error_state(
                "Printer response invalid: fail to parse a body")
            self._logger.error(
                "Printer response invalid: fail to parse a body\n"+octoprint.util.get_exception_string())
            return

        self.session_id = session_id

        eventManager().fire(Events.CONNECTED, dict(port=123, baudrate=250000))
        self._set_state("OPERATIONAL")
        self._logger.info(
            "Printer connected, session_id {}".format(self.session_id))

    def disconnect(self, *args, **kwargs):
        """
        Disconnects from the printer. Does nothing if no connection is currently established.
        """

        eventManager().fire(Events.DISCONNECTING)
        self.session_id = -1
        self.port = None
        self.baudrate = None
        self.printer_profile = None

        eventManager().fire(Events.DISCONNECTED)
        self._set_state("CLOSED")

    def get_transport(self, *args, **kwargs):
        """
        Returns the communication layer's transport object, if a connection is currently established.

        Note that this doesn't have to necessarily be a :class:`serial.Serial` instance, it might also be something
        different, so take care to do instance checks before attempting to access any properties or methods.

        Returns:
                        object: The communication layer's transport object
        """
        raise NotImplementedError()

    def job_on_hold(self, blocking=True, *args, **kwargs):
        """
        Contextmanager that allows executing code while printing while making sure that no commands from the file
        being printed are continued to be sent to the printer. Note that this will only work for local files,
        NOT SD files.

        Example:

        .. code-block:: python

                 with printer.job_on_hold():
                                 park_printhead()
                                 take_snapshot()
                                 send_printhead_back()

        It should be used sparingly and only for very specific situations (such as parking the print head somewhere,
        taking a snapshot from the webcam, then continuing). If you abuse this, you WILL cause print quality issues!

        A lock is in place that ensures that the context can only actually be held by one thread at a time. If you
        don't want to block on acquire, be sure to set ``blocking`` to ``False`` and catch the ``RuntimeException`` thrown
        if the lock can't be acquired.

        Args:
                blocking (bool): Whether to block while attempting to acquire the lock (default) or not
        """
        raise NotImplementedError()

    def set_job_on_hold(self, value, blocking=True, *args, **kwargs):
        """
        Setter for finer control over putting jobs on hold. Set to ``True`` to ensure that no commands from the file
        being printed are continued to be sent to the printer. Set to ``False`` to resume. Note that this will only
        work for local files, NOT SD files.

        Make absolutely sure that if you set this flag, you will always also unset it again. If you don't, the job will
        be stuck forever.

        Example:

        .. code-block:: python

                 if printer.set_job_on_hold(True):
                                 try:
                                                 park_printhead()
                                                 take_snapshot()
                                                 send_printhead_back()
                                 finally:
                                                 printer.set_job_on_hold(False)

        Just like :func:`~octoprint.printer.PrinterInterface.job_on_hold` this should be used sparingly and only for
        very specific situations. If you abuse this, you WILL cause print quality issues!

        Args:
                value (bool): The value to set
                blocking (bool): Whether to block while attempting to set the value (default) or not

        Returns:
                (bool) Whether the value could be set successfully (True) or a timeout was encountered (False)
        """
        raise NotImplementedError()

    def fake_ack(self, *args, **kwargs):
        """
        Fakes an acknowledgment for the communication layer. If the communication between OctoPrint and the printer
        gets stuck due to lost "ok" responses from the server due to communication issues, this can be used to get
        things going again.
        """
        raise NotImplementedError()

    def commands(self, commands, tags=None, *args, **kwargs):
        """
        Sends the provided ``commands`` to the printer.

        Arguments:
                        commands (str, list): The commands to send. Might be a single command provided just as a string or a list
                                        of multiple commands to send in order.
                        tags (set of str): An optional set of tags to attach to the command(s) throughout their lifecycle
        """
        if not self.is_ready():
            return

        if isinstance(commands, str):
            commands = [commands]

        if not isinstance(commands, list):
            self._logger.exception(
                "commands should be a string or list of strings")
            return

        # list of commands can be sent as an '\n' separated string
        command_str = '\n'.join(commands)
        with self._control_lock:
            if not self.is_ready():
                return
            try:
                resp = self.make_cmd_request(
                    "gcode", data=command_str.encode("gbk"))
            except:
                self._logger.exception(
                    "fail to send command. cmd: '%s'", command_str, exc_info=sys.exc_info())
                return

            try:
                j = resp.json()
            except:
                self._logger.exception(
                    "command resp is not json. body: '%s'", resp.text, exc_info=sys.exc_info())
                return

            if j.get("c", -1) != 0:
                self._logger.error(
                    "command resp not ok. cmd: '%s', error_code: '%d', message: '%s'", command_str, j.get("c", -1), j.get("m"))
            else:
                self._logger.info(
                    "command sent succesfully. cmd: '%s'", command_str)

    def script(self, name, context=None, tags=None, *args, **kwargs):
        """
        Sends the GCODE script ``name`` to the printer.

        The script will be run through the template engine, the rendering context can be extended by providing a
        ``context`` with additional template variables to use.

        If the script is unknown, an :class:`UnknownScriptException` will be raised.

        Arguments:
                        name (str): The name of the GCODE script to render.
                        context (dict): An optional context of additional template variables to provide to the renderer.
                        tags (set of str): An optional set of tags to attach to the command(s) throughout their lifecycle

        Raises:
                        UnknownScriptException: There is no GCODE script with name ``name``
        """
        raise NotImplementedError()

    def jog(self, axes, relative=True, speed=None, tags=None, *args, **kwargs):
        """
        Jogs the specified printer ``axis`` by the specified ``amount`` in mm.

        Arguments:
                        axes (dict): Axes and distances to jog, keys are axes ("x", "y", "z"), values are distances in mm
                        relative (bool): Whether to interpret the distance values as relative (true, default) or absolute (false)
                                        coordinates
                        speed (int, bool or None): Speed at which to jog (F parameter). If set to ``False`` no speed will be set
                                        specifically. If set to ``None`` (or left out) the minimum of all involved axes speeds from the printer
                                        profile will be used.
                        tags (set of str): An optional set of tags to attach to the command(s) throughout their lifecycle
        """
        if not axes:
            raise ValueError("At least one axis to jog must be provided")

        for axis in axes:
            if not axis in self.valid_axes:
                raise ValueError("Invalid axis {}, valid axes are {}".format(
                    axis, ", ".join(self.valid_axes)))

        command = "G1 {}".format(" ".join(
            ["{}{}".format(axis.upper(), amount) for axis, amount in axes.items()]))

        if speed is None:
            speed = min([self.printer_profile["axes"][axis]["speed"]
                         for axis in axes])

        if speed and not isinstance(speed, bool):
            command += " F{}".format(speed)

        if relative:
            commands = ["G91", command, "G90"]
        else:
            commands = ["G90", command]

        self.commands(commands, tags=kwargs.get(
            "tags", set()) | {"trigger:printer.jog"})

    def home(self, axes, tags=None, *args, **kwargs):
        """
        Homes the specified printer ``axes``.

        Arguments:
                        axes (str, list): The axis or axes to home, each of which must converted to lower case must match one of
                                        "x", "y", "z" and "e"
                        tags (set of str): An optional set of tags to attach to the command(s) throughout their lifecycle
        """
        if not isinstance(axes, (list, tuple)):
            if isinstance(axes, (str, unicode)):
                axes = [axes]
            else:
                raise ValueError(
                    "axes is neither a list nor a string: {axes}".format(axes=axes))

        validated_axes = filter(
            lambda x: x in self.valid_axes, map(lambda x: x.lower(), axes))
        if len(axes) != len(validated_axes):
            raise ValueError(
                "axes contains invalid axes: {axes}".format(axes=axes))

        self.commands(["G91", "G28 %s" % " ".join(map(lambda x: "%s0" % x.upper(), validated_axes)), "G90"],
                      tags=kwargs.get("tags", set) | {"trigger:printer.home"})

    def extrude(self, amount, speed=None, tags=None, *args, **kwargs):
        """
        Extrude ``amount`` millimeters of material from the tool.

        Arguments:
                        amount (int, float): The amount of material to extrude in mm
                        speed (int, None): Speed at which to extrude (F parameter). If set to ``None`` (or left out)
                        the maximum speed of E axis from the printer profile will be used.
                        tags (set of str): An optional set of tags to attach to the command(s) throughout their lifecycle
        """
        if not isinstance(amount, (int, long, float)):
            raise ValueError(
                "amount must be a valid number: {amount}".format(amount=amount))

        # Use specified speed (if any)
        max_e_speed = self.printer_profile["axes"]["e"]["speed"]

        if speed is None:
            # No speed was specified so default to value configured in printer profile
            extrusion_speed = max_e_speed
        else:
            # Make sure that specified value is not greater than maximum as defined in printer profile
            extrusion_speed = min([speed, max_e_speed])

        self.commands(["G91", "G1 E%s F%d" % (amount, extrusion_speed), "G90"],
                      tags=kwargs.get("tags", set()) | {"trigger:printer.extrude"})

    def change_tool(self, tool, tags=None, *args, **kwargs):
        """
        Switch the currently active ``tool`` (for which extrude commands will apply).

        Arguments:
                        tool (str): The tool to switch to, matching the regex "tool[0-9]+" (e.g. "tool0", "tool1", ...)
                        tags (set of str): An optional set of tags to attach to the command(s) throughout their lifecycle
        """
        pass  # only tool0 for now

    def set_temperature(self, heater, value, tags=None, *args, **kwargs):
        """
        Sets the target temperature on the specified ``heater`` to the given ``value`` in celsius.

        Arguments:
                        heater (str): The heater for which to set the target temperature. Either "bed" for setting the bed
                                        temperature or something matching the regular expression "tool[0-9]+" (e.g. "tool0", "tool1", ...) for
                                        the hotends of the printer
                        value (int, float): The temperature in celsius to set the target temperature to.
                        tags (set of str): An optional set of tags to attach to the command(s) throughout their lifecycle
        """
        if not isinstance(value, (int, long, float)) or value < 0:
            raise ValueError(
                "value must be a valid number >= 0: {value}".format(value=value))
        if heater == "bed":
            self.commands("M140 S{}".format(int(value)))
        elif heater == "tool0":
            self.commands("M104 S{}".format(int(value)))
        else:
            self._logger.error(
                "unsupported heater '%s'. Supported only 'bed' and 'tool0'", heater)

    def set_temperature_offset(self, offsets=None, tags=None, *args, **kwargs):
        """
        Sets the temperature ``offsets`` to apply to target temperatures read from a GCODE file while printing.

        Arguments:
                        offsets (dict): A dictionary specifying the offsets to apply. Keys must match the format for the ``heater``
                                        parameter to :func:`set_temperature`, so "bed" for the offset for the bed target temperature and
                                        "tool[0-9]+" for the offsets to the hotend target temperatures.
                        tags (set of str): An optional set of tags to attach to the command(s) throughout their lifecycle
        """
        pass  # not supported

    def _convert_rate_value(self, factor, min=0, max=200):
        if not isinstance(factor, (int, float, long)):
            raise ValueError("factor is not a number")

        if isinstance(factor, float):
            factor = int(factor * 100.0)

        if factor < min or factor > max:
            raise ValueError(
                "factor must be a value between {} and {}".format(min, max))

        return factor

    def feed_rate(self, factor, tags=None, *args, **kwargs):
        """
        Sets the ``factor`` for the printer's feed rate.

        Arguments:
                        factor (int, float): The factor for the feed rate to send to the firmware. Percentage expressed as either an
                                        int between 0 and 100 or a float between 0 and 1.
                        tags (set of str): An optional set of tags to attach to the command(s) throughout their lifecycle
        """
        factor = self._convert_rate_value(factor, min=50, max=200)
        self.commands("M220 S%d" % factor,
                      tags=kwargs.get("tags", set()) | {"trigger:printer.feed_rate"})

    def flow_rate(self, factor, tags=None, *args, **kwargs):
        """
        Sets the ``factor`` for the printer's flow rate.

        Arguments:
                        factor (int, float): The factor for the flow rate to send to the firmware. Percentage expressed as either an
                                        int between 0 and 100 or a float between 0 and 1.
                        tags (set of str): An optional set of tags to attach to the command(s) throughout their lifecycle
        """
        factor = self._convert_rate_value(factor, min=75, max=125)
        self.commands("M221 S%d" % factor,
                      tags=kwargs.get("tags", set()) | {"trigger:printer.flow_rate"})

    def can_modify_file(self, path, sd, *args, **kwargs):
        """
        Determines whether the ``path`` (on the printer's SD if ``sd`` is True) may be modified (updated or deleted)
        or not.

        A file that is currently being printed is not allowed to be modified. Any other file or the current file
        when it is not being printed is fine though.

        :since: 1.3.2

        .. warning::

                 This was introduced in 1.3.2 to work around an issue when updating a file that is already selected.
                 I'm not 100% sure at this point if this is the best approach to solve this issue, so if you decide
                 to depend on this particular method in this interface, be advised that it might vanish in future
                 versions!

        Arguments:
                        path (str): path in storage of the file to check
                        sd (bool): True if to check against SD storage, False otherwise

        Returns:
                        (bool) True if the file may be modified, False otherwise
        """
        return not (self.is_current_file(path, sd) and (self.is_printing() or self.is_paused()))

    def is_current_file(self, path, sd, *args, **kwargs):
        """
        Returns whether the provided ``path`` (on the printer's SD if ``sd`` is True) is the currently selected
        file for printing.

        :since: 1.3.2

        .. warning::

                 This was introduced in 1.3.2 to work around an issue when updating a file that is already selected.
                 I'm not 100% sure at this point if this is the best approach to solve this issue, so if you decide
                 to depend on this particular method in this interface, be advised that it might vanish in future
                 versions!

        Arguments:
                        path (str): path in storage of the file to check
                        sd (bool): True if to check against SD storage, False otherwise

        Returns:
                        (bool) True if the file is currently selected, False otherwise
        """
        if not sd:
            return False
        with self._selected_file_lock:
            return path == self._selected_file

    def select_file(self, path, sd, printAfterSelect=False, pos=None, tags=None, *args, **kwargs):
        """
        Selects the specified ``path`` for printing, specifying if the file is to be found on the ``sd`` or not.
        Optionally can also directly start the print after selecting the file.

        Arguments:
                        path (str): The path to select for printing. Either an absolute path or relative path to a  local file in
                                        the uploads folder or a filename on the printer's SD card.
                        sd (boolean): Indicates whether the file is on the printer's SD card or not.
                        printAfterSelect (boolean): Indicates whether a print should be started
                                        after the file is selected.
                        tags (set of str): An optional set of tags to attach to the command(s) throughout their lifecycle

        Raises:
                        InvalidFileType: if the file is not a machinecode file and hence cannot be printed
                        InvalidFileLocation: if an absolute path was provided and not contained within local storage or
                                        doesn't exist
        """
        if not self.is_ready():
            return
        if not sd:
            eventManager().fire(Events.ERROR, payload=dict(error="only SD file can be selected"))
            return
        with self._selected_file_lock:
            self._selected_file = path
        self._set_job_file(path)

    def unselect_file(self, *args, **kwargs):
        """
        Unselects and currently selected file.
        """
        with self._selected_file_lock:
            self._selected_file = None
        self._set_job_file(None)

    def start_print(self, tags=None, *args, **kwargs):
        """
        Starts printing the currently selected file. If no file is currently selected, does nothing.

        Arguments:
                        tags (set of str): An optional set of tags to attach to the command(s) throughout their lifecycle
        """
        if not self.is_ready():
            self._logger.error("Printer is not ready for print")
            return

        with self._selected_file_lock:
            if self._selected_file is None:
                self._logger.warn("No file selected for print")
                return

            try:
                resp = self.make_cmd_request(
                    "print_start", data=self._selected_file)
                self.check_resp_status(resp)
                self._set_state("PRINTING")
                self._print_started = datetime.now()
                eventManager().fire(Events.PRINT_STARTED, dict(
                    name=self._selected_file, path=self._selected_file, origin="sdcard"))
            except:
                self._logger.error("Print start failed",
                                   exc_info=sys.exc_info())
                eventManager().fire(Events.ERROR, dict(error="Print start failed"))

    def pause_print(self, tags=None, *args, **kwargs):
        """
        Pauses the current print job if it is currently running, does nothing otherwise.

        Arguments:
                        tags (set of str): An optional set of tags to attach to the command(s) throughout their lifecycle
        """
        try:
            resp = self.make_cmd_request("pause_print")
            self.check_resp_status(resp)
            self._set_state("PAUSED")
            eventManager().fire(Events.PRINT_PAUSED, dict(
                name=self._selected_file, path=self._selected_file, origin="sdcard"))
        except:
            self._logger.error("Print pause failed", exc_info=sys.exc_info())
            eventManager().fire(Events.ERROR, dict(error="Print pause failed"))

    def resume_print(self, tags=None, *args, **kwargs):
        """
        Resumes the current print job if it is currently paused, does nothing otherwise.

        Arguments:
                        tags (set of str): An optional set of tags to attach to the command(s) throughout their lifecycle
        """
        try:
            resp = self.make_cmd_request("resume_print")
            self.check_resp_status(resp)
            self._set_state("PRINTING")
            eventManager().fire(Events.PRINT_RESUMED, dict(
                name=self._selected_file, path=self._selected_file, origin="sdcard"))
        except:
            self._logger.error("Print resume failed", exc_info=sys.exc_info())
            eventManager().fire(Events.ERROR, dict(error="Print resume failed"))

    def toggle_pause_print(self, tags=None, *args, **kwargs):
        """
        Pauses the current print job if it is currently running or resumes it if it is currently paused.

        Arguments:
                        tags (set of str): An optional set of tags to attach to the command(s) throughout their lifecycle
        """
        if self.is_printing():
            self.pause_print(tags=tags, *args, **kwargs)
        elif self.is_paused():
            self.resume_print(tags=tags, *args, **kwargs)

    def cancel_print(self, tags=None, *args, **kwargs):
        """
        Cancels the current print job.

        Arguments:
                        tags (set of str): An optional set of tags to attach to the command(s) throughout their lifecycle
        """
        try:
            resp = self.make_cmd_request("stop_print")
            self.check_resp_status(resp)
            self._set_state("OPERATIONAL")
            eventManager().fire(Events.PRINT_CANCELLED, dict(
                name=self._selected_file, path=self._selected_file, origin="sdcard"))
        except:
            self._logger.error("Print cancel failed", exc_info=sys.exc_info())
            eventManager().fire(Events.ERROR, dict(error="Print cancel failed"))

    def log_lines(self, *lines):
        """
        Logs the provided lines to the printer log and serial.log
        Args:
                *lines: the lines to log
        """
        pass

    def get_state_string(self, *args, **kwargs):
        """
        Returns:
                         (str) A human readable string corresponding to the current communication state.
        """
        return self._state_error if self._state_error is not None else self.get_state_id()

    def get_state_id(self, *args, **kwargs):
        """
        Identifier of the current communication state.

        Possible values are:

                * OPEN_SERIAL
                * DETECT_SERIAL
                * DETECT_BAUDRATE
                * CONNECTING
                * OPERATIONAL
                * PRINTING
                * RESUMING
                * CANCELLING
                * PAUSED
                * PAUSING
                * FINISHING
                * CLOSED
                * ERROR
                * CLOSED_WITH_ERROR
                * TRANSFERING_FILE
                * OFFLINE
                * UNKNOWN
                * NONE

        Returns:
                         (str) A unique identifier corresponding to the current communication state.
        """
        return self._state if self._state is not None else "NONE"

    def get_current_data(self, *args, **kwargs):
        """
        Returns:
                        (dict) The current state data.
        """
        with self._data_lock:
            return self._data

    def get_current_job(self, *args, **kwargs):
        """
        Returns:
                        (dict) The data of the current job.
        """
        with self._data_lock:
            return self._data.get("job", {})

    def get_current_temperatures(self, *args, **kwargs):
        """
        Returns:
                        (dict) The current temperatures.
        """
        with self._current_temp_lock:
            return self._current_temperature

    def get_temperature_history(self, *args, **kwargs):
        """
        Returns:
                        (list) The temperature history.
        """
        raise NotImplementedError()

    def get_current_connection(self, *args, **kwargs):
        """
        Returns:
                        (tuple) The current connection information as a 4-tuple ``(connection_string, port, baudrate, printer_profile)``.
                                        If the printer is currently not connected, the tuple will be ``("Closed", None, None, None)``.
        """
        if self._is_connected():
            return ("Open", self.port, self.baudrate, self.printer_profile)
        return ("Closed", None, None, None)

    def is_closed_or_error(self, *args, **kwargs):
        """
        Returns:
                        (boolean) Whether the printer is currently disconnected and/or in an error state.
        """
        return self.get_state_id() in ["CLOSED", "ERROR", "CLOSED_WITH_ERROR"]

    def is_operational(self, *args, **kwargs):
        """
        Returns:
                        (boolean) Whether the printer is currently connected and available.
        """
        return self.get_state_id() in ["OPERATIONAL", "PRINTING", "RESUMING", "CANCELLING", "PAUSED", "PAUSING", "FINISHING"]

    def is_printing(self, *args, **kwargs):
        """
        Returns:
                        (boolean) Whether the printer is currently printing.
        """
        return self.get_state_id() == "PRINTING"

    def is_cancelling(self, *args, **kwargs):
        """
        Returns:
                        (boolean) Whether the printer is currently cancelling a print.
        """
        return self.get_state_id() == "CANCELLING"

    def is_pausing(self, *args, **kwargs):
        """
        Returns:
                (boolean) Whether the printer is currently pausing a print.
        """
        return self.get_state_id() == "PAUSING"

    def is_paused(self, *args, **kwargs):
        """
        Returns:
                        (boolean) Whether the printer is currently paused.
        """
        return self.get_state_id() == "PAUSED"

    def is_error(self, *args, **kwargs):
        """
        Returns:
                        (boolean) Whether the printer is currently in an error state.
        """
        return self.get_state_id() == "ERROR"

    def is_ready(self, *args, **kwargs):
        """
        Returns:
                        (boolean) Whether the printer is currently operational and ready for new print jobs (not printing).
        """
        return self.get_state_id() == "OPERATIONAL"

    def is_resuming(self, *args, **kwargs):
        return self.get_state_id() == "RESUMING"

    def is_finishing(self, *args, **kwargs):
        return self.get_state_id() == "FINISHING"

    def is_sd_ready(self, *args, **kwargs):
        return self.is_ready()

    def register_callback(self, callback, *args, **kwargs):
        """
        Registers a :class:`PrinterCallback` with the instance.

        Arguments:
                        callback (PrinterCallback): The callback object to register.
        """
        if not isinstance(callback, octoprint.printer.PrinterCallback):
            self._logger.warn(
                "Registering an object as printer callback which doesn't implement the PrinterCallback interface")

        self._callbacks.append(callback)
        self._sendInitialStateUpdate(callback)

    def unregister_callback(self, callback, *args, **kwargs):
        """
        Unregisters a :class:`PrinterCallback` from the instance.

        Arguments:
                        callback (PrinterCallback): The callback object to unregister.
        """
        if callback in self._callbacks:
            self._callbacks.remove(callback)

    def _send_update(self):
        with self._data_lock:
            self._data["state"] = dict(
                text=self.get_state_string(), flags=self._getStateFlags())
            self._sendCurrentDataCallbacks(self._data)

    def _set_error_state(self, error):
        self._state_error = error
        self._state = "ERROR"
        self._send_update()
        eventManager().fire(Events.ERROR, dict(error=error))

    def _set_state(self, state):
        self._state = state
        self._state_error = None
        self._send_update()

    def _sendInitialStateUpdate(self, callback):
        try:
            with self._data_lock:
                callback.on_printer_send_initial_data(self._data)
        except:
            self._logger.exception(
                u"Error while pushing initial state update to callback {}".format(callback))

    def _sendCurrentDataCallbacks(self, data):
        for callback in self._callbacks:
            try:
                callback.on_printer_send_current_data(data)
            except:
                self._logger.exception(
                    u"Exception while pushing current data to callback {}".format(callback))

    def _sendAddTemperatureCallbacks(self, data):
        for callback in self._callbacks:
            try:
                callback.on_printer_add_temperature(data)
            except:
                self._logger.exception(
                    u"Exception while adding temperature data point to callback {}".format(callback))

    def _sendAddLogCallbacks(self, data):
        for callback in self._callbacks:
            try:
                callback.on_printer_add_log(data)
            except:
                self._logger.exception(
                    u"Exception while adding communication log entry to callback {}".format(callback))

    def _sendAddMessageCallbacks(self, data):
        for callback in self._callbacks:
            try:
                callback.on_printer_add_message(data)
            except:
                self._logger.exception(
                    u"Exception while adding printer message to callback {}".format(callback))

    def _getStateFlags(self):
        return dict(operational=self.is_operational(),
                    printing=self.is_printing(),
                    cancelling=self.is_cancelling(),
                    pausing=self.is_pausing(),
                    resuming=self.is_resuming(),
                    finishing=self.is_finishing(),
                    closedOrError=self.is_closed_or_error(),
                    error=self.is_error(),
                    paused=self.is_paused(),
                    ready=self.is_ready(),
                    sdReady=self.is_sd_ready(),
                    )

    def make_cmd_request(self, cmd, data=None):
        return self.make_request("cmd", headers=dict(cmd=cmd), data=data if data is not None else "\n")

    def make_request(self, path, headers=None, data=None):
        url = "http://{}/{}".format(self.host(), path)
        if self.session_id != -1:
            if headers is None:
                headers = {}
            headers["session_id"] = str(self.session_id)
        if data is not None:
            return requests.post(url, headers=headers, data=data)
        return requests.get(url, headers=headers)

    def check_resp_status(self, resp, request_name=""):
        try:
            j = resp.json()
            if j["c"] == 0:
                self._logger.info("Request %s succeseded", request_name)
            elif j["c"] == 1:
                # timeout, but usually print is started
                self._logger.info("Request %s has timeout", request_name)
            else:
                raise Exception(
                    "Request %s failed: code %d msg %s" % (request_name, j["c"], j["m"]))
        except:
            self._logger.error("Request failed, body %s", resp.text,
                               exc_info=sys.exc_info())
            eventManager().fire(Events.ERROR, dict(
                error="Request failed"
            ))

    def _format_job(self, name):
        return dict(file=dict(name=name,
                              path=name,
                              display=name,
                              origin=FileDestinations.SDCARD),
                    estimatedPrintTime=2,
                    averagePrintTime=2,
                    lastPrintTime=None,
                    filament=None,
                    user=None)

    def _set_job_file(self, name):
        with self._data_lock:
            self._data["job"] = self._format_job(name)
        self._send_update()
        if name is not None:
            eventManager().fire(Events.FILE_SELECTED, payload={
                "name": name, "path": name, "origin": "sdcard"})
        else:
            eventManager().fire(Events.FILE_DESELECTED)

    def get_sd_files(self, *args, **kwargs):
        if not self.is_sd_ready():
            return []
        try:
            resp = self.make_request("filelist", data="SD:".encode("gbk"))
        except:
            self._logger.exception(
                "fail to request sd files", exc_info=sys.exc_info())
            return []

        files = []
        lines = resp.text.splitlines()
        for line in lines:
            if len(line) > 1 and line[0] == "F":
                files.append((line[1:], 0))

        return files

    def add_sd_file(self, filename, path, on_success=None, on_failure=None, *args, **kwargs):
        if not self.is_sd_ready():
            self._logger.error("Printer is not connected or busy")
            return

        eventManager().fire(Events.TRANSFER_STARTED, dict(local=filename, remote=filename))

        try:
            with open(path, 'rb') as f:
                data = f.read()
            resp = self.make_request("upload", headers=dict(
                path="SD:/{}".format(filename)), data=data)
            j = resp.json()
            if j["c"] == 0:
                eventManager().fire(Events.TRANSFER_DONE, dict(
                    local=filename,
                    remote=filename,
                ))
                if callable(on_success):
                    on_success(filename, filename, FileDestinations.SDCARD)
            else:
                raise Exception(
                    "Fail to upload file: code %d msg %s" % (j["c"], j["m"]))
        except:
            self._logger.error("Fail to upload file",
                               exc_info=sys.exc_info())
            eventManager().fire(Events.TRANSFER_FAILED, dict(
                local=filename,
                remote=filename,
            ))
            if callable(on_failure):
                on_failure(filename, filename, FileDestinations.SDCARD)

    def _work_updates(self):
        while True:
            if not self._is_connected():
                time.sleep(2)
                continue
            try:
                data = self._request_status()
            except:
                self._logger.exception(
                    "exception inside worker", exc_info=sys.exc_info())
                self._failed_attempts += 1
                if self._failed_attempts > 5:
                    self._logger.info("fail to connect to printer at %d attempts. Disconnecting", self._failed_attempts)
                    self._failed_attempts = 0
                    self.disconnect()                
            try:
                if data is not None:
                    self._failed_attempts = 0
                    with self._data_lock:
                        if data["is_printing"]:
                            self._data["job"] = self._format_job(
                                data.get("printing_filename"))
                            if self._print_started is not None:
                                print_time = (datetime.now() -
                                              self._print_started).seconds
                            self._data["progress"] = dict(
                                completion=data.get("printing_progress") if data.get(
                                    "printing_progress") != 0 else 1,
                                printTime=print_time,
                                printTimeLeft=2,
                                filepos=data.get("printing_progress"),
                            )
                            self._logger.debug(
                                "progress %s", self._data["progress"])
                            self._logger.debug(
                                "flags %s", self._getStateFlags())
                        else:
                            self._data["progress"] = dict()
                    self._send_update()
                    with self._current_temp_lock:
                        self._current_temperature = dict(
                            tool0=dict(
                                actual=data.get("temp_tool0"),
                                target=data.get("temp_tool0_target"),
                            ),
                            bed=dict(
                                actual=data.get("temp_bed"),
                                target=data.get("temp_bed_target"),
                            ),
                        )
                        self._sendAddTemperatureCallbacks(
                            self._current_temperature)
                    state = None
                    if data["is_paused"]:
                        state = "PAUSED"
                    elif data["is_printing"]:
                        state = "PRINTING"
                    elif not data["is_printing"] and not data["is_paused"]:
                        state = "OPERATIONAL"
                    if state is not None and state != self.get_state_id():
                        if self.get_state_id() == "PRINTING" and state == "OPERATIONAL":
                            eventManager().fire(Events.PRINT_DONE, dict(
                                name=self._selected_file, path=self._selected_file, origin="sdcard"))
                        self._set_state(state)
            except:
                self._logger.exception(
                    "exception inside worker", exc_info=sys.exc_info())

            time.sleep(2)

    def _request_status(self):
        if not self._is_connected():
            return
        resp = self.make_request("status")

        if resp.status_code != 200:
            self._logger.warn(
                "Printer status response invalid: status code is {}".format(resp.status_code))
            return

        body = resp.text
        if body.find("DATA") != 0:
            self._logger.warn(
                "Printer status response invalid: expected 'DATA' at start")
            self._logger.debug("Actual body: '{}'".format(body))
            return
        body = body[4:]
        status = body.split(",")
        if len(status) != 18:
            self._logger.warn(
                "Printer status response invalid: expected 18 values, got {}".format(len(status)))
            self._logger.debug("Actual body: '{}'".format(body))
            return
        return dict(
            connect_state=status[0],

            is_bed_heating=status[1] == "1",
            is_tool0_heating=status[2] == "1",
            is_tool1_heating=status[3] == "1",
            is_printing=status[4] == "1",
            is_paused=status[5] == "1",

            temp_bed_target=int(status[6]),
            temp_bed=int(status[7]),
            temp_tool0_target=int(status[8]),
            temp_tool0=int(status[9]),
            temp_tool1_target=int(status[10]),
            temp_tool1=int(status[11]),

            tool_count=int(status[12]),
            current_tool=int(status[13]),
            printing_progress=int(status[14]),
            print_version_code=int(status[15]),
            print_id=int(status[16]),
            printing_filename=status[17],
        )


class OctoBear(octoprint.plugin.TemplatePlugin, octoprint.plugin.SettingsPlugin):
    def printer_host(self):
        return self._settings.get(["printer_host"])

    def get_settings_defaults(self):
        return dict(printer_host="")

    def get_template_vars(self):
        return dict(printer_host=self._settings.get(["printer_host"]))

    def get_template_configs(self):
        return [
            dict(type="settings", custom_bindings=False)
        ]

    def printer_factory_hook(self, components, *args, **kwargs):
        return Ghost4Printer(self.printer_host)


def __plugin_load__():
    plugin = OctoBear()

    global __plugin_implementation__
    __plugin_implementation__ = plugin

    global __plugin_hooks__
    __plugin_hooks__ = {
        "octoprint.printer.factory": plugin.printer_factory_hook}
