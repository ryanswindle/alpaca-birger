import time
from datetime import datetime, timezone
from threading import Lock
from typing import Optional

from birger import (
    BirgerDevice,
    BirgerError,
    FOCUS_MAX,
    FOCUS_MIN,
)
from config import DeviceConfig
from log import get_logger


logger = get_logger()


# IsMoving stays True after a Move() until we've observed the lens move
# AND its reported position has been stable for at least the settle window.
# - STARTUP_TIMEOUT covers "move issued but no change yet seen" (the poll
#   interval is ~500 ms, so we need at least two polls of grace before we
#   conclude the lens isn't going to move at all — e.g. already at target).
# - SETTLE_SECONDS is the quiet window after the last observed change.
_MOVE_STARTUP_TIMEOUT = 2.0
_MOVE_SETTLE_SECONDS = 1.0


class FocuserDevice:
    """Low-level driver for the Birger EF-232 lens controller."""

    def __init__(self, device_config: DeviceConfig):
        self._config = device_config

        self.birger: Optional[BirgerDevice] = None

        # Last commanded target position (0..16383), or None if never set
        self._target_position: Optional[int] = None
        # Monotonic timestamp of the most recent move command, used to bridge
        # the gap between issuing `eh` and the first spontaneous %:xxxx update.
        self._last_move_time: float = 0.0
        self._move_lock = Lock()

        # Connection state
        self._connected = False
        self._connecting = False

    #######################################
    # ASCOM Methods Common To All Devices #
    #######################################
    def connect(self):
        """Open the serial port and bring the Birger up to operating state."""

        if self._connecting or self._connected:
            return

        self._connecting = True
        try:
            if self.birger is None:
                self.birger = BirgerDevice(
                    self._config.port, self._config.baud, self._config.timeout
                )

            self.birger.open()

            # Bring the device into terse + new-protocol mode and confirm by
            # reading the short version string (manual section 4.1 of BEI app
            # manual outlines this same sequence). `routeesc,0` is a bootloader
            # command — when sent to a running library it emits ERR1; we drain
            # that and any pre-existing buffered output. After `rm0,1` the
            # device stops echoing and stops emitting OK/legacy acks, so `vs`
            # gets a clean `s:C2v…` response.
            self.birger.drain()
            self.birger.send("routeesc,0")
            self.birger.drain()
            self.birger.send("rm0,1")
            self.birger.drain()
            vs = self.birger.send("vs", startswith="s:", timeout=2.0)
            logger.debug(f"Birger version: {vs}")
            if "C2" not in vs:
                raise RuntimeError(f"Unexpected version response: {vs!r}")

            # Enable background lens querying so spontaneous status reflects
            # current focus (bit 3 of sm flags), and turn on spontaneous
            # responses so the reader can track focus position live. Both
            # commands have definite known responses, so wait for them
            # explicitly — fire-and-forget + drain races on slow lenses.
            self.birger.send("sm12", expect="DONE", timeout=2.0)
            self.birger.send("sr1", expect="OK", timeout=2.0)

            # Verify a lens is connected before attempting the focus learn
            if not self.birger.query_lens_presence():
                self.birger.close()
                raise RuntimeError("No lens connected to Birger EF-232")

            if self._config.auto_learn:
                logger.info(f"Learning focus range for {self._config.entity}...")
                self.birger.learn_range(timeout=60.0)

            # Prime the cached focus position via a status dump
            self.birger.query_status()

            # sr1 broadcasts to other ports only, so on a single-port driver
            # we never see our own moves spontaneously — poll `gs` instead.
            self.birger.start_polling(interval=0.5)

            # Optionally drive to a configured startup position. Done after
            # polling starts so the IsMoving heuristic can observe the motion,
            # and after auto_learn so the mapped 14-bit scale is already valid.
            if self._config.initial_focus is not None:
                logger.info(
                    f"Driving {self._config.entity} to initial focus "
                    f"{self._config.initial_focus} (0x{self._config.initial_focus:04x})"
                )
                with self._move_lock:
                    self._target_position = self._config.initial_focus
                    self._last_move_time = time.monotonic()
                    self.birger.move_to(self._config.initial_focus)

            self._connected = True
            logger.info(f"Connected to focuser {self._config.entity}")

        except Exception as e:
            logger.error(f"Connection error: {e}")
            if self.birger is not None:
                try:
                    self.birger.close()
                except Exception:
                    pass
            self._connected = False
            raise
        finally:
            self._connecting = False

    @property
    def connected(self) -> bool:
        return self._connected

    @connected.setter
    def connected(self, value: bool):
        if value and not self._connected:
            self.connect()
        elif not value and self._connected:
            self.disconnect()

    @property
    def connecting(self) -> bool:
        return self._connecting

    def disconnect(self):
        """Close the serial port."""

        if self.birger is not None:
            try:
                self.birger.close()
            except Exception as e:
                logger.warning(f"Birger close failed: {e}")

        self._connected = False
        logger.info(f"Disconnected from focuser {self._config.entity}")

    @property
    def entity(self) -> str:
        return self._config.entity

    ########################
    # IFocuserV4 properties #
    ########################
    @property
    def absolute(self) -> bool:
        # Birger maps the focus range to an absolute 14-bit scale (manual 3.4).
        return True

    @property
    def is_moving(self) -> bool:
        if self.birger is None or self._target_position is None:
            return False
        now = time.monotonic()
        last_change = self.birger.last_focus_change

        # Move issued but no position change observed yet — give the poller
        # a few cycles to either capture motion or confirm the lens is
        # already at the commanded position.
        if last_change < self._last_move_time:
            return (now - self._last_move_time) < _MOVE_STARTUP_TIMEOUT

        # Motion has been observed since the last move; report moving until
        # the position has been quiet for the settle window.
        return (now - last_change) < _MOVE_SETTLE_SECONDS

    @property
    def max_increment(self) -> int:
        # The full mapped range is reachable in a single move.
        return FOCUS_MAX

    @property
    def max_step(self) -> int:
        return FOCUS_MAX

    @property
    def position(self) -> int:
        if self.birger is None:
            raise RuntimeError("Not connected to focuser")
        pos = self.birger.focus_position
        if pos is None:
            # Force a refresh if we have not seen a position yet
            try:
                self.birger.query_status()
            except BirgerError as e:
                logger.warning(f"Status query failed: {e}")
            pos = self.birger.focus_position
        return pos if pos is not None else FOCUS_MIN

    @property
    def step_size(self) -> float:
        # Mapped step size in microns is not defined for the Canon EF mount;
        # raise NotImplemented per ASCOM convention via the route layer.
        raise NotImplementedError("StepSize")

    @property
    def temp_comp(self) -> bool:
        return False

    @property
    def temp_comp_available(self) -> bool:
        return False

    @property
    def temperature(self) -> float:
        raise NotImplementedError("Temperature")

    def halt(self) -> None:
        if self.birger is None:
            raise RuntimeError("Not connected to focuser")
        with self._move_lock:
            self.birger.halt()
            # Anchor the move-complete heuristic at the current position
            self._target_position = self.birger.focus_position

    def move(self, position: int) -> None:
        if self.birger is None:
            raise RuntimeError("Not connected to focuser")
        if position < FOCUS_MIN or position > FOCUS_MAX:
            raise ValueError(
                f"Position {position} out of range ({FOCUS_MIN}–{FOCUS_MAX})"
            )
        with self._move_lock:
            self._target_position = position
            self._last_move_time = time.monotonic()
            self.birger.move_to(position)
            logger.debug(f"Moving to position {position}")

    @property
    def timestamp(self) -> str:
        return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
