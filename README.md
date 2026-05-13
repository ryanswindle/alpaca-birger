# Birger – ASCOM Alpaca Server for the Birger EF-232

A FastAPI-based server implementing the **IFocuserV4** interface for the Birger Engineering EF-232 Canon EF lens controller. Communication is over a serial port (typically a USB-to-serial adapter, e.g. `/dev/ttyUSB0`) using the ASCII command protocol documented in the *Canon EF-232 Library User Manual*.

---

## Implemented IFocuserV4 capabilities as of this driver version

| Property/Method      | Supported |
|----------------------|-----------|
| Absolute             | ✅ True (14-bit mapped range 0..16383) |
| IsMoving             | ✅ Tracked via spontaneous `%:xxxx` responses |
| MaxIncrement         | ✅ Full mapped range (16383) |
| MaxStep              | ✅ 16383 |
| Position             | ✅ Mapped 0..16383 |
| StepSize             | ❌ Not implemented (no defined microns/step for Canon EF) |
| TempComp             | ❌ Hardware does not support |
| TempCompAvailable    | ✅ False |
| Temperature          | ❌ Hardware does not support |
| Halt                 | ✅ Re-commands current position to stop motion |
| Move                 | ✅ Servo focus (`eh` with checksum) |

Aperture control is **not** exposed by this driver — IFocuserV4 has no aperture concept and the EF-232 is treated as focus-only here.

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
    auto_learn: true
```

| Field        | Description |
|--------------|-------------|
| `port`       | Serial device path (e.g. `/dev/ttyUSB0`, `/dev/ttyS0`) |
| `baud`       | Default 115200 — change only if you've reprogrammed the controller |
| `timeout`    | Per-command response timeout in seconds |
| `auto_learn` | If true, runs `la` (learn focus range) on every successful connect |

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

- Initialization sequence on connect: `routeesc,0` → `rm0,1` (terse + new) → `vs` (verify identity) → `sm12` (background querying) → `sr1` (spontaneous responses) → `lp` (lens presence check) → `la` (learn focus range, if `auto_learn`).
- Focus moves use the **servo focus with checksum** command `eh<pos4hex>,<chk>` (manual section 5.6). The command is non-blocking; the lens reports progress via spontaneous `%:xxxx` status strings.
- `IsMoving` is inferred by comparing the live position against the last commanded target, with a settling window to absorb the fact that the 14-bit mapped range is finer than the lens' raw encoder resolution.
- `Halt` re-issues `eh` at the currently reported position because the Birger has no explicit halt command.
