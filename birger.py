"""
Birger EF-232 lens controller serial protocol module.

Implements the ASCII command protocol described in the Canon EF-232 Library
User Manual (Birger Engineering). Provides:
  - A reader thread that continuously parses lines from the device, classifies
    them as status strings (`%:`, `&:`, `@:`, `#:`), lens connection messages,
    or command responses, and updates internal state accordingly.
  - A `send` helper that serializes commands and waits for a matching response.
  - Focus-specific helpers (`learn_range`, `move_to`, `halt`, `focus_position`).

Operates the device in terse + new protocol mode (`rm0,1`) with background
querying and spontaneous responses enabled (`sm12` + `sr1`).
"""

import queue
import re
import threading
import time
from typing import Optional

import serial

from log import get_logger


logger = get_logger()


######################################
# Error codes (manual Rev 1.4 Tbl 4) #
######################################
class BIRGER_ERROR_CODE:
    NO_ERROR = 0
    UNRECOGNIZED_COMMAND = 1
    LENS_IN_MANUAL_FOCUS = 2
    NO_LENS_CONNECTED = 3
    LENS_DISTANCE_STOP_ERROR = 4
    APERTURE_NOT_INITIALIZED = 5
    INVALID_BAUD_RATE = 6
    BAD_PARAMETER = 9
    XMODEM_TIMEOUT = 10
    XMODEM_ERROR = 11
    XMODEM_UNLOCK = 12
    INVALID_PORT = 14
    LICENSE_UNLOCK_FAILURE = 15
    INVALID_LICENSE_FILE = 16
    INVALID_LIBRARY_FILE = 17
    LIBRARY_NOT_READY_FOR_LENS = 21
    LIBRARY_NOT_READY_FOR_COMMANDS = 22
    COMMAND_NOT_LICENSED = 23
    INVALID_FOCUS_RANGE = 24
    DISTANCE_STOPS_UNSUPPORTED = 25

    _NAMES = {
        0: "No error",
        1: "Unrecognized command",
        2: "Lens is in manual focus mode",
        3: "No lens connected",
        4: "Lens distance stop error",
        5: "Aperture not initialized",
        6: "Invalid baud rate",
        9: "Bad parameter",
        10: "XModem timeout",
        11: "XModem error",
        12: "XModem unlock code incorrect",
        14: "Invalid port",
        15: "License unlock failure",
        16: "Invalid license file",
        17: "Invalid library file",
        21: "Library not ready for lens communications",
        22: "Library not ready for commands",
        23: "Command not licensed",
        24: "Invalid focus range in memory (try relearning)",
        25: "Distance stops not supported by lens",
    }

    @classmethod
    def name(cls, code: int) -> str:
        return cls._NAMES.get(code, f"Unknown error ({code})")


######################
# Status-line regexes #
######################
_RE_FOCUS = re.compile(r"^%:([0-9a-fA-F]{4})$")
_RE_FLAGS = re.compile(r"^#:([0-9a-fA-F]{4})$")
_RE_ERR = re.compile(r"^ERR(\d+)$")


# Absolute mapped focus range is 14-bit: 0..0x3FFF (Birger manual section 3.4)
FOCUS_MIN = 0
FOCUS_MAX = 0x3FFF


def focus_checksum(position: int) -> int:
    """XOR of the four 4-bit nibbles of a 14-bit focus position (manual 5.6)."""
    checksum = 0
    mask = 0x1000
    for _ in range(4):
        checksum ^= (position // mask) & 0xF
        mask >>= 4
    return checksum & 0x0F


class BirgerError(RuntimeError):
    """Raised for errors returned by the Birger lens controller."""


class BirgerDevice:
    """Low-level driver for the Birger EF-232 lens controller."""

    def __init__(self, port: str, baud: int = 115200, timeout: float = 2.0):
        self._port = port
        self._baud = baud
        self._timeout = timeout

        self._serial: Optional[serial.Serial] = None
        self._reader: Optional[threading.Thread] = None
        self._stop = threading.Event()

        # Serialize command/response cycles
        self._tx_lock = threading.Lock()
        # Lines that aren't status/lens-msg get queued here for `send` to consume
        self._rx_queue: "queue.Queue[str]" = queue.Queue()

        # State maintained by the reader thread
        self._state_lock = threading.Lock()
        self._focus_position: Optional[int] = None
        self._lens_present: bool = False
        self._last_focus_update: float = 0.0

    #################
    # Connection    #
    #################
    def open(self) -> None:
        """Open the serial port and start the reader thread."""

        if self._serial is not None:
            return

        self._serial = serial.Serial(
            self._port,
            self._baud,
            bytesize=serial.EIGHTBITS,
            parity=serial.PARITY_NONE,
            stopbits=serial.STOPBITS_ONE,
            timeout=0.2,
        )
        self._stop.clear()
        self._reader = threading.Thread(
            target=self._reader_loop, name="BirgerReader", daemon=True
        )
        self._reader.start()
        logger.debug(f"Opened {self._port} at {self._baud} baud")

    def close(self) -> None:
        """Stop the reader thread and close the serial port."""

        self._stop.set()
        if self._reader is not None:
            self._reader.join(timeout=2.0)
            self._reader = None
        if self._serial is not None:
            try:
                self._serial.close()
            except Exception as e:
                logger.warning(f"Serial close failed: {e}")
            self._serial = None

    @property
    def is_open(self) -> bool:
        return self._serial is not None and self._serial.is_open

    def drain(self, idle: float = 0.15, total_timeout: float = 1.0) -> None:
        """Discard queued response lines until no new line arrives for `idle` seconds.

        Used between init commands whose responses we don't care about (e.g.
        `routeesc,0` which yields ERR1 when run against the library instead of
        the bootloader). Bounded by `total_timeout` so we never block forever.
        """

        deadline = time.monotonic() + total_timeout
        while time.monotonic() < deadline:
            try:
                line = self._rx_queue.get(timeout=idle)
            except queue.Empty:
                return
            logger.debug(f"drained: {line!r}")

    ##########
    # Reader #
    ##########
    def _reader_loop(self) -> None:
        """Read lines from the device and classify them."""

        buf = b""
        while not self._stop.is_set():
            try:
                chunk = self._serial.read(128)
            except Exception as e:
                if not self._stop.is_set():
                    logger.error(f"Serial read failed: {e}")
                return
            if not chunk:
                continue
            buf += chunk
            # The Birger terminates output lines with CR (and sometimes CR+LF).
            while True:
                idx_r = buf.find(b"\r")
                idx_n = buf.find(b"\n")
                if idx_r == -1 and idx_n == -1:
                    break
                if idx_r == -1:
                    idx = idx_n
                elif idx_n == -1:
                    idx = idx_r
                else:
                    idx = min(idx_r, idx_n)
                line, buf = buf[:idx], buf[idx + 1:]
                # Collapse any adjacent CR/LF
                while buf and buf[:1] in (b"\r", b"\n"):
                    buf = buf[1:]
                self._handle_line(line.decode("ascii", errors="replace").strip())

    def _handle_line(self, line: str) -> None:
        if not line:
            return
        logger.debug(f"RX: {line!r}")

        m = _RE_FOCUS.match(line)
        if m:
            pos = int(m.group(1), 16)
            with self._state_lock:
                self._focus_position = pos
                self._last_focus_update = time.monotonic()
            return

        if _RE_FLAGS.match(line):
            return  # GPIO/init flags – not used by IFocuserV4

        if line.startswith("&:") or line.startswith("@:") or line.startswith("$:"):
            return  # Aperture / focal length / distance stops – not used

        if line == "Lens Connected.":
            with self._state_lock:
                self._lens_present = True
            logger.info("Lens connected")
            return

        if line == "Lens Disconnected.":
            with self._state_lock:
                self._lens_present = False
            logger.warning("Lens disconnected")
            return

        # Anything else is a command response
        self._rx_queue.put(line)

    #################
    # Command I/O   #
    #################
    def send(
        self,
        command: str,
        expect: Optional[str] = None,
        startswith: Optional[str] = None,
        timeout: float = 5.0,
    ) -> str:
        """Send a command and (optionally) wait for a matching response line.

        - `expect`: exact match required (e.g., "DONE:LA").
        - `startswith`: any line starting with this prefix is accepted (e.g., "s:").
        - If both are None, fire-and-forget (e.g., for `eh`).
        Raises BirgerError on `ERRx` responses or timeout.
        """

        if not self.is_open:
            raise BirgerError("Serial port is not open")

        with self._tx_lock:
            # Drain any stale responses lingering from before this command
            while not self._rx_queue.empty():
                try:
                    self._rx_queue.get_nowait()
                except queue.Empty:
                    break

            data = (command + "\r").encode("ascii")
            logger.debug(f"TX: {command!r}")
            self._serial.write(data)
            self._serial.flush()

            if expect is None and startswith is None:
                return ""

            deadline = time.monotonic() + timeout
            while True:
                remaining = max(0.0, deadline - time.monotonic())
                if remaining == 0.0:
                    raise BirgerError(f"Timeout waiting for response to {command!r}")
                try:
                    line = self._rx_queue.get(timeout=remaining)
                except queue.Empty:
                    raise BirgerError(f"Timeout waiting for response to {command!r}")
                m = _RE_ERR.match(line)
                if m:
                    code = int(m.group(1))
                    raise BirgerError(
                        f"{command!r} returned ERR{code}: {BIRGER_ERROR_CODE.name(code)}"
                    )
                if expect is not None and line == expect:
                    return line
                if startswith is not None and line.startswith(startswith):
                    return line
                # Otherwise skip and keep waiting

    ##################
    # State helpers  #
    ##################
    @property
    def focus_position(self) -> Optional[int]:
        with self._state_lock:
            return self._focus_position

    @property
    def lens_present(self) -> bool:
        with self._state_lock:
            return self._lens_present

    @property
    def last_focus_update(self) -> float:
        with self._state_lock:
            return self._last_focus_update

    #####################
    # Focus operations  #
    #####################
    def move_to(self, position: int) -> None:
        """Servo the focus to a 14-bit mapped position (manual 5.6 `eh`)."""

        position = max(FOCUS_MIN, min(FOCUS_MAX, int(position)))
        chk = focus_checksum(position)
        self.send(f"eh{position:04x},{chk:x}")

    def halt(self) -> None:
        """Stop motion by re-commanding the current position (manual lacks halt)."""

        pos = self.focus_position
        if pos is None:
            return  # Nothing to halt against
        self.move_to(pos)

    def learn_range(self, timeout: float = 60.0) -> None:
        """Learn the focus range so the mapped 0..16383 scale is valid (manual 5.18)."""

        self.send("la", expect="DONE:LA", timeout=timeout)

    def query_lens_presence(self) -> bool:
        """Force-refresh `lens_present` from the device (manual 5.21 `lp`)."""

        line = self.send("lp", startswith="", timeout=2.0)
        present = line.strip() == "1"
        with self._state_lock:
            self._lens_present = present
        return present

    def query_status(self, timeout: float = 2.0) -> None:
        """Request a full status dump (manual 5.12 `gs`).

        The reply consists entirely of status-prefixed lines (`%:`, `&:`,
        `@:`, `#:`) that the reader thread intercepts before they hit the
        response queue, so we wait for an observable state change instead
        of a queued response.
        """

        with self._state_lock:
            before = self._last_focus_update
        self.send("gs")
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._state_lock:
                if self._last_focus_update > before:
                    return
            time.sleep(0.02)
        raise BirgerError("Timeout waiting for status update after 'gs'")
