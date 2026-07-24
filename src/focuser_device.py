import threading
import time
from datetime import datetime, timezone
from threading import Lock
from typing import Optional

from birger import (
    BIRGER_ERROR_CODE,
    BirgerDevice,
    BirgerError,
)
from config import DeviceConfig
from log import get_logger


logger = get_logger()


# `fa` does not return until the lens has stopped, so an in-flight move worker
# is itself the IsMoving signal. The settle window covers the tail: the worker
# reads the count back on completion, and any firmware that answers `fa` early
# will still show the polled count shifting, which keeps IsMoving true.
_MOVE_SETTLE_SECONDS = 1.0

# `la` racks the lens end to end; on a long telephoto that is not quick.
_LEARN_TIMEOUT = 90.0


class FocuserDevice:
    """Low-level driver for the Birger EF-232 lens controller."""

    def __init__(self, device_config: DeviceConfig):
        self._config = device_config

        self.birger: Optional[BirgerDevice] = None

        # Learned raw encoder bounds, read from `fp` at connect. ASCOM positions
        # are offset from `_focus_min` so the scale the client sees starts at 0.
        self._focus_min: Optional[int] = None
        self._focus_max: Optional[int] = None

        # Number of move workers in flight; nonzero means the lens is moving.
        self._active_moves = 0
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
                    self._config.port,
                    self._config.baud,
                    self._config.timeout,
                    self._config.move_timeout,
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
            # responses so those strings keep flowing. Both commands have
            # definite known responses, so wait for them explicitly —
            # fire-and-forget + drain races on slow lenses.
            self.birger.send("sm12", expect="DONE", timeout=2.0)
            self.birger.send("sr1", expect="OK", timeout=2.0)

            # Verify a lens is connected before asking about the focus range
            if not self.birger.query_lens_presence():
                self.birger.close()
                raise RuntimeError("No lens connected to Birger EF-232")

            # The controller keeps the learned range in its own memory across
            # power cycles, so `fp` alone is normally enough. Relearn only when
            # explicitly configured, or when the controller reports its stored
            # range is unusable — every learn shifts the measured bounds by a
            # few counts, and every stored position is relative to them.
            if self._config.auto_learn:
                logger.info(f"Learning focus range for {self._config.entity}...")
                self.birger.learn_range(timeout=_LEARN_TIMEOUT)
            else:
                try:
                    self.birger.read_range()
                except BirgerError as e:
                    if e.code != BIRGER_ERROR_CODE.INVALID_FOCUS_RANGE:
                        raise
                    logger.warning(
                        f"Controller reports no usable focus range ({e}); relearning"
                    )
                    self.birger.learn_range(timeout=_LEARN_TIMEOUT)

            self._focus_min = self.birger.focus_min
            self._focus_max = self.birger.focus_max
            logger.info(
                f"{self._config.entity} focus range: raw {self._focus_min}.."
                f"{self._focus_max} ({self.max_step} steps)"
            )

            # `fa` blocks for the duration of a move and holds the serial lock,
            # so the poller only runs between moves. It exists to notice focus
            # changes this driver did not command.
            self.birger.start_polling(interval=0.5)

            # Optionally drive to a configured startup position. Done after the
            # range is known so the target can be validated against it.
            if self._config.initial_focus is not None:
                logger.info(
                    f"Driving {self._config.entity} to initial focus "
                    f"{self._config.initial_focus}"
                )
                self.move(self._config.initial_focus)

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
        # `fa` addresses absolute encoder counts.
        return True

    @property
    def is_moving(self) -> bool:
        if self.birger is None:
            return False
        with self._move_lock:
            if self._active_moves > 0:
                return True
        # No worker in flight. The polled count settling is the backstop: it
        # covers focus this driver did not command, and any firmware whose
        # `fa` answers before the lens has actually stopped.
        return (time.monotonic() - self.birger.last_focus_change) < _MOVE_SETTLE_SECONDS

    @property
    def max_increment(self) -> int:
        # The full learned range is reachable in a single move.
        return self.max_step

    @property
    def max_step(self) -> int:
        if self._focus_min is None or self._focus_max is None:
            raise RuntimeError("Not connected to focuser")
        return self._focus_max - self._focus_min

    @property
    def position(self) -> int:
        if self.birger is None or self._focus_min is None:
            raise RuntimeError("Not connected to focuser")
        count = self.birger.focus_count
        if count is None:
            # Force a refresh if we have not seen a count yet
            try:
                self.birger.read_range()
            except BirgerError as e:
                logger.warning(f"Focus position read failed: {e}")
            count = self.birger.focus_count
        if count is None:
            return 0
        # Each `la` shifts the measured bounds by a few counts, so a lens
        # parked at an end can read just outside the range learned earlier.
        # ASCOM requires 0..MaxStep, so clamp what we report — commanded
        # targets are validated and rejected rather than clamped.
        return max(0, min(self.max_step, count - self._focus_min))

    @property
    def step_size(self) -> float:
        # Encoder counts have no defined micron size for the Canon EF mount;
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
        count = self.birger.focus_count
        if count is None:
            return  # Nothing to halt against
        self._start_move(count)

    def move(self, position: int) -> None:
        if self.birger is None or self._focus_min is None:
            raise RuntimeError("Not connected to focuser")
        if position < 0 or position > self.max_step:
            raise ValueError(f"Position {position} out of range (0–{self.max_step})")
        self._start_move(position + self._focus_min)
        logger.debug(f"Moving to position {position}")

    def learn_range(self) -> None:
        """Relearn the focus range (`la`) and adopt the new bounds.

        Exposed as the `LearnFocusRange` action for lens changes. Runs inline
        rather than on a worker so the caller gets a real success or failure —
        it takes tens of seconds and should be rare. Every position recorded
        against the old bounds shifts by however far the new ones moved.
        """

        if self.birger is None:
            raise RuntimeError("Not connected to focuser")
        with self._move_lock:
            self._active_moves += 1
        try:
            self.birger.learn_range(timeout=_LEARN_TIMEOUT)
            self._focus_min = self.birger.focus_min
            self._focus_max = self.birger.focus_max
            logger.info(
                f"{self._config.entity} relearned focus range: raw "
                f"{self._focus_min}..{self._focus_max} ({self.max_step} steps)"
            )
        finally:
            with self._move_lock:
                self._active_moves -= 1

    def _start_move(self, target_count: int) -> None:
        """Run `fa` on a worker so Move returns promptly, per ASCOM semantics."""

        with self._move_lock:
            self._active_moves += 1
        threading.Thread(
            target=self._run_move,
            args=(target_count,),
            name="BirgerMove",
            daemon=True,
        ).start()

    def _run_move(self, target_count: int) -> None:
        """Body of a move worker.

        Motion is asynchronous under ASCOM, so a failure here has no request to
        propagate to — it is logged, and the position the client reads back
        afterwards is the truth about where the lens ended up.
        """

        try:
            self.birger.move_to(target_count)
        except BirgerError as e:
            if e.code == BIRGER_ERROR_CODE.INVALID_FOCUS_RANGE:
                logger.warning(f"Move rejected ({e}); relearning focus range")
                try:
                    self.birger.learn_range(timeout=_LEARN_TIMEOUT)
                    self._focus_min = self.birger.focus_min
                    self._focus_max = self.birger.focus_max
                    self.birger.move_to(target_count)
                except BirgerError as retry_error:
                    logger.error(
                        f"Move to raw count {target_count} failed after "
                        f"relearn: {retry_error}"
                    )
            else:
                logger.error(f"Move to raw count {target_count} failed: {e}")
        finally:
            # Nothing may escape before the decrement — a leaked count would
            # pin IsMoving true for the life of the process.
            try:
                self.birger.read_range()
            except Exception as e:
                logger.debug(f"Post-move fp failed: {e}")
            with self._move_lock:
                self._active_moves -= 1

    @property
    def timestamp(self) -> str:
        return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
