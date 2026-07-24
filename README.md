# Birger – ASCOM Alpaca Server for the Birger EF-232

A FastAPI-based server implementing the **IFocuserV4** interface for the Birger Engineering EF-232 Canon EF lens controller. Communication is over a serial port (typically a USB-to-serial adapter, e.g. `/dev/ttyUSB0`) using the ASCII command protocol documented in the *Canon EF-232 Library User Manual*.

---

## Implemented IFocuserV4 capabilities as of this driver version

| Property/Method      | Supported |
|----------------------|-----------|
| Absolute             | ✅ True (raw encoder counts, `fa`) |
| IsMoving             | ✅ In-flight `fa` worker, backed by `fp` polling |
| MaxIncrement         | ✅ Full learned range |
| MaxStep              | ✅ Learned range read from `fp` at connect |
| Position             | ✅ 0..MaxStep, offset from the learned `fmin` |
| StepSize             | ❌ Not implemented (no defined microns/step for Canon EF) |
| TempComp             | ❌ Hardware does not support |
| TempCompAvailable    | ✅ False |
| Temperature          | ❌ Hardware does not support |
| Halt                 | ✅ Re-commands current position (see caveat below) |
| Move                 | ✅ Absolute focus (`fa`) on a worker thread |
| Action               | ✅ `LearnFocusRange` — runs `la` and adopts the new bounds |

Aperture control is **not** exposed by this driver — IFocuserV4 has no aperture concept and the EF-232 is treated as focus-only here.

### Focus scale

The EF-232 exposes focus two ways: a 14-bit *mapped* scale (`eh` to command,
`%:xxxx` to read) capped at 0x3FFF, and the lens' *raw* encoder counts (`fa` to
command, `fp` to read). This driver uses the raw counts.

The mapped scale cannot address the whole lens on long-travel glass. A Canon
400mm f/2.8 measures `fmin:-11076 fmax:11743` — 22,819 counts of travel against
16,384 mapped steps, so the mapped scale is both unable to reach some positions
and about 1.4× coarser than the encoder everywhere else.

Raw counts can be negative, so ASCOM `Position` is reported as `count - fmin`,
putting the client-visible scale at `0..MaxStep` where `MaxStep = fmax - fmin`.
Both bounds are read from `fp` at connect, so `MaxStep` follows whatever lens is
attached rather than being a compiled-in constant.

**Positions are only meaningful relative to the learn that produced them.** Each
`la` re-measures the bounds and lands a few counts off the last run, which shifts
the `fmin` origin and every stored position with it. Relearn when the lens
changes, not on a schedule — see `auto_learn` below.

---

## Configuration

Edit `config.yaml` (or mount a replacement at `/alpyca/config.yaml` inside the container):

```yaml
entity: birger
server:
  host: 0.0.0.0
  port: 5020
log_level: INFO
devices:
  - entity: BIRGER_1
    device_number: 0
    port: /dev/ttyUSB0
    baud: 115200
    timeout: 2.0
    move_timeout: 60.0
    auto_learn: false
    initial_focus: null    # 0..MaxStep to drive to on connect; null leaves it alone
```

| Field           | Description |
|-----------------|-------------|
| `port`          | Serial device path (e.g. `/dev/ttyUSB0`, `/dev/ttyS0`) |
| `baud`          | Default 115200 — change only if you've reprogrammed the controller |
| `timeout`       | Per-command response timeout in seconds |
| `move_timeout`  | How long to wait for `fa` to report the move complete. Full-travel moves on a long telephoto take tens of seconds. |
| `auto_learn`    | If true, runs `la` on every connect. Defaults to **false**: the controller keeps the learned range across power cycles, and relearning shifts every stored position. The driver relearns on its own when the controller reports ERR24. |
| `initial_focus` | Position in the `0..MaxStep` scale to drive to on connect, or `null` to leave the lens where it is. Out-of-range values fail the connect rather than being clamped. |

### When to relearn the focus range

`la` is needed when the lens is changed, when the controller reports ERR24
(*invalid focus range in memory*), or after anything that clears controller
memory. It is **not** needed per night, per reboot, or per server restart — the
range survives all of those.

Two ways to trigger it:

- The driver runs `la` automatically when a `fp` or `fa` comes back ERR24.
- `PUT /action` with `Action=LearnFocusRange` runs it on demand and returns the
  new `MaxStep`. This is the one to use after a lens swap.

Either way, focus positions recorded against the previous bounds need to be
re-derived afterwards.

---

## Running locally

```bash
pip install -r requirements.txt
python main.py
```

---

## Running via Docker

The container needs access to the serial device. On Ubuntu:

```bash
docker build -t alpaca-birger .
docker run --rm \
  --device=/dev/ttyUSB0:/dev/ttyUSB0 \
  -p 5020:5020 \
  -p 32227:32227/udp \
  -v $(pwd)/config.yaml:/alpyca/config.yaml \
  alpaca-birger
```

If your serial device path differs, adjust both the `--device` mapping and the `port:` field in `config.yaml` accordingly.

---

## Notes on the Birger protocol

- Initialization sequence on connect: `routeesc,0` → `rm0,1` (terse + new) → `vs` (verify identity) → `sm12` (background querying) → `sr1` (spontaneous responses) → `lp` (lens presence check) → `fp` (read learned range; `la` first if `auto_learn`, or on ERR24) → `fa` (drive to `initial_focus`, if set).
- Focus moves use the **absolute focus** command `fa<count>`. `fa` blocks — the controller answers `DONE` only once the lens has stopped — so `Move` runs it on a worker thread and returns immediately, per ASCOM's asynchronous `Move`/`IsMoving` contract.
- `IsMoving` is true while a move worker is in flight. Between moves it falls back to `fp` polling, which catches focus changes this driver did not command.
- `%:xxxx` status strings still arrive (via `sm12` + `sr1`) but are discarded: they are on the mapped scale and must not be mixed into raw counts. Position comes only from `fp`.
- `Halt` re-commands the last known count because the Birger has no halt command. Since `fa` does not return until the lens has stopped, a halt issued mid-move cannot interrupt the move in flight — it serializes behind it.
- Move failures are logged rather than returned: under ASCOM the motion is asynchronous, so there is no longer a request to fail. Read `Position` back to see where the lens actually ended up.

---

## ASCOM Conformance

<!-- conformu:start -->
Last tested with **ConformU 4.3.0 (Build 49708.0503dc7)** on 2026-07-23
(`python test_conformu.py`):

| Device | Errors | Issues | Info | Status |
|--------|:------:|:------:|:----:|:------:|
| BIRGER_1 (Focuser #0) | 1 | 0 | 78 | ✓ PASS |

_Errors may be non-zero when no hardware is attached (NotConnectedException is the expected response). **Issues == 0** indicates Alpaca protocol conformance._
<!-- conformu:end -->
