"""
Birger EF-232 lens controller serial protocol module.

Implements the ASCII command protocol described in the Canon EF-232 Library
User Manual (Birger Engineering). Provides:
  - A reader thread that continuously parses lines from the device, classifies
    them as status strings (`%:`, `&:`, `@:`, `#:`), lens connection messages,
    or command responses, and updates internal state accordingly.
  - A `send` helper that serializes commands and waits for a matching response.
  - Focus-specific helpers (`learn_range`, `read_range`, `move_to`, `halt`).

Operates the device in terse + new protocol mode (`rm0,1`) with background
querying and spontaneous responses enabled (`sm12` + `sr1`).

Focus is addressed in the lens' native encoder counts via `fa`/`fp`, not via
the 14-bit mapped `eh`/`%:` scale. The mapped scale is capped at 0x3FFF, which
is coarser than — and on lenses with long travel unable to reach the ends of —
the raw range that `la` learns. `fp` reports that raw range, and both ends may
be negative, so nothing here assumes a zero-based scale.
"""

import queue
import re
import threading
import time
from typing import Optional, Tuple

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
# `fp` reply, e.g. "fmin:-11076  fmax:11743  current:11743"
_RE_FP = re.compile(r"^fmin:(-?\d+)\s+fmax:(-?\d+)\s+current:(-?\d+)$")


class BirgerError(RuntimeError):
    """Raised for errors returned by the Birger lens controller.

    `code` carries the numeric ERRx code when the device rejected a command,
    so callers can react to specific failures (notably ERR24, which asks for
    a relearn) instead of parsing the message text.
    """

    def __init__(self, message: str, code: Optional[int] = None):
        super().__init__(message)
        self.code = code


class BirgerDevice:
    """Low-level driver for the Birger EF-232 lens controller."""

    def __init__(
        self,
        port: str,
        baud: int = 115200,
        timeout: float = 2.0,
        move_timeout: float = 60.0,
    ):
        self._port = port
        self._baud = baud
        self._timeout = timeout
        self._move_timeout = move_timeout

        self._serial: Optional[serial.Serial] = None
        self._reader: Optional[threading.Thread] = None
        self._poller: Optional[threading.Thread] = None
        self._poll_interval: float = 0.5
        self._poll_enabled = threading.Event()
        self._stop = threading.Event()
        # Set while an `fa` is in flight. `fa` blocks until the lens stops, so
        # it holds the tx lock for the whole move; polling during that window
        # would just queue up behind it and land stale.
        self._move_active = threading.Event()

        # Serialize command/response cycles
        self._tx_lock = threading.Lock()
        # Lines that aren't status/lens-msg get queued here for `send` to consume
        self._rx_queue: "queue.Queue[str]" = queue.Queue()

        # State maintained by `read_range`
        self._state_lock = threading.Lock()
        self._focus_count: Optional[int] = None
        self._focus_min: Optional[int] = None
        self._focus_max: Optional[int] = None
        self._lens_present: bool = False
        # `last_focus_update`: any successful `fp` parse (used by read_range sync).
        # `last_focus_change`: only when the parsed count actually differs
        # from the previous one — feeds the IsMoving heuristic so periodic
        # polling at a stationary position doesn't look like motion.
        self._last_focus_update: float = 0.0
        self._last_focus_change: float = 0.0

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
        self._poll_enabled.clear()
        self._reader = threading.Thread(
            target=self._reader_loop, name="BirgerReader", daemon=True
        )
        self._reader.start()
        self._poller = threading.Thread(
            target=self._poll_loop, name="BirgerPoller", daemon=True
        )
        self._poller.start()
        logger.debug(f"Opened {self._port} at {self._baud} baud")

    def start_polling(self, interval: float = 0.5) -> None:
        """Start periodic `fp` polling so the focus count stays fresh.

        `sr1` only broadcasts changes to ports other than the originating
        port, so a single-serial-port driver never sees its own moves
        spontaneously. The poll keeps `_focus_count` current via `fp`.
        """

        self._poll_interval = interval
        self._poll_enabled.set()

    def stop_polling(self) -> None:
        self._poll_enabled.clear()

    def close(self) -> None:
        """Stop the reader/poller threads and close the serial port."""

        self._stop.set()
        self._poll_enabled.clear()
        if self._poller is not None:
            self._poller.join(timeout=2.0)
            self._poller = None
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

    def drain(self, idle: float = 0.3, total_timeout: float = 1.5) -> None:
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
    def _poll_loop(self) -> None:
        """Periodically refresh the focus count while polling is enabled."""

        while not self._stop.is_set():
            if not self._poll_enabled.wait(timeout=0.2):
                continue
            if not self._move_active.is_set():
                try:
                    self.read_range()
                except Exception as e:
                    logger.debug(f"poll fp failed: {e}")
            # Sleep in small slices so close() can promptly stop us
            t = self._poll_interval
            while t > 0 and not self._stop.is_set() and self._poll_enabled.is_set():
                step = min(0.1, t)
                time.sleep(step)
                t -= step

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

        if _RE_FOCUS.match(line):
            # `%:xxxx` is the 14-bit mapped position, a different scale from
            # the raw counts this driver works in. `sm12` + `sr1` keep these
            # arriving spontaneously; swallow them so they never reach a
            # `send` waiting on a command response.
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
        - If both are None, fire-and-forget.
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
                        f"{command!r} returned ERR{code}: {BIRGER_ERROR_CODE.name(code)}",
                        code=code,
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
    def focus_count(self) -> Optional[int]:
        """Last count from `fp`, on that command's signed raw basis."""
        with self._state_lock:
            return self._focus_count

    @property
    def focus_position(self) -> Optional[int]:
        """Last position on the 0-based basis `fa` accepts (0 == `fmin`)."""
        with self._state_lock:
            if self._focus_count is None or self._focus_min is None:
                return None
            return self._focus_count - self._focus_min

    @property
    def focus_min(self) -> Optional[int]:
        with self._state_lock:
            return self._focus_min

    @property
    def focus_max(self) -> Optional[int]:
        with self._state_lock:
            return self._focus_max

    @property
    def lens_present(self) -> bool:
        with self._state_lock:
            return self._lens_present

    @property
    def last_focus_update(self) -> float:
        with self._state_lock:
            return self._last_focus_update

    @property
    def last_focus_change(self) -> float:
        with self._state_lock:
            return self._last_focus_change

    #####################
    # Focus operations  #
    #####################
    def read_range(self) -> Tuple[int, int, int]:
        """Read the learned raw focus range and current count (`fp`).

        Returns `(fmin, fmax, current)` in encoder counts and caches them. This
        is the only thing that updates the cached count — the spontaneous
        `%:xxxx` strings are on the mapped scale and must not be mixed in.
        """

        line = self.send("fp", startswith="fmin:", timeout=self._timeout)
        m = _RE_FP.match(line)
        if m is None:
            raise BirgerError(f"Unparseable fp response: {line!r}")
        fmin, fmax, current = (int(g) for g in m.groups())
        if fmax <= fmin:
            raise BirgerError(
                f"Nonsensical focus range from fp: fmin={fmin} fmax={fmax}",
                code=BIRGER_ERROR_CODE.INVALID_FOCUS_RANGE,
            )
        now = time.monotonic()
        with self._state_lock:
            if current != self._focus_count:
                self._last_focus_change = now
            self._focus_count = current
            self._focus_min = fmin
            self._focus_max = fmax
            self._last_focus_update = now
        return fmin, fmax, current

    def move_to(self, position: int) -> None:
        """Drive focus to an absolute position (`fa`).

        The two focus commands do not share a basis. `fa` counts up from the
        near end of the learned range — 0 is `fmin`, `fmax - fmin` is the far
        end — while `fp` reports `current` as a signed raw count. Take
        `position` on the `fa` basis; `focus_position` converts the other way.

        `fa` blocks: the controller answers only once the lens has stopped, so
        callers that need a prompt return must run this on a worker thread.
        Out-of-range targets raise rather than being clamped — a silently
        truncated move parks the lens somewhere the caller never asked for.
        """

        position = int(position)
        with self._state_lock:
            fmin, fmax = self._focus_min, self._focus_max
        if fmin is None or fmax is None:
            raise BirgerError("Focus range unknown; call read_range() first")
        span = fmax - fmin
        if position < 0 or position > span:
            raise BirgerError(f"Focus position {position} out of range (0–{span})")

        self._move_active.set()
        try:
            self.send(f"fa{position}", startswith="DONE", timeout=self._move_timeout)
        finally:
            self._move_active.clear()

    def halt(self) -> None:
        """Stop motion by re-commanding the last known count (manual lacks halt).

        The EF-232 has no halt command, and `fa` does not return until the lens
        has stopped, so a halt issued mid-move cannot interrupt the move in
        flight — it serializes behind it and then holds the lens at wherever
        this call found it.
        """

        position = self.focus_position
        if position is None:
            return  # Nothing to halt against
        self.move_to(position)

    def learn_range(self, timeout: float = 60.0) -> None:
        """Learn the focus range and refresh the cached bounds (manual 5.18 `la`).

        Racks the lens end to end and parks it at `fmax`, and shifts the
        measured bounds by a few counts each time it runs — so every stored
        position is relative to the most recent learn.
        """

        self.send("la", expect="DONE:LA", timeout=timeout)
        self.read_range()

    def query_lens_presence(self) -> bool:
        """Force-refresh `lens_present` from the device (manual 5.21 `lp`)."""

        line = self.send("lp", startswith="", timeout=2.0)
        present = line.strip() == "1"
        with self._state_lock:
            self._lens_present = present
        return present
