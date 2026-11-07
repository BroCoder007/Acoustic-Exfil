# AegisAcoustic with ML noise filtering

**Start with [FIRST_TIME_SETUP.md](FIRST_TIME_SETUP.md)** for hardware, wiring,
installation, a new Keil project, flashing, Python and the first received message.
[START_HERE.md](START_HERE.md) is the short daily-use reference.

The current `main.c` is the integrated receiver, with ML enabled by default:

```text
Microphone -> 40 kHz ADC -> queued samples -> trained ML filter
           -> FSK decoder -> frame CRC -> AES decryption -> OLED / HC-05
```

Add exactly these three application C files to Keil, alongside the device
pack's CMSIS/startup/system files:

```text
main.c
aegis_ml_denoise.c
aegis_ml_adapter.c
```

Keep the three ML headers beside those files. `main.c.txt` mirrors `main.c`
for convenient viewing; compile only `main.c`. The model weights are already
included; no retraining, TensorFlow installation or MCU AI framework is needed.

The sender still asks only for your message and saves encrypted fan audio:

```powershell
python ultrasonic_tx.py
```

The integrated filter uses the existing 2000/3000 Hz channel, the existing demo
AES key, 60 ms bits and up to 255 UTF-8 bytes. The firmware now configures an
84 MHz clock and reports ML processing time/sample losses. It is trained to
preserve this data channel, not arbitrary encrypted sounds or unknown keys.

## Project contents

| Location | Purpose |
| --- | --- |
| `main.c` + `aegis_ml_*` | Current integrated firmware source and trained weights |
| `ultrasonic_tx.py` | Message -> AES-encrypted fan-sound WAV |
| `aegis_menu.py` | Same one-message flow; optional advanced diagnostics |
| `aegis_tools.py` | Offline WAV decoding and protocol self-test |
| `requirements.txt`, `check_setup.py` | Reproducible PC dependencies and optional setup check |
| `docs/` | Model details, sources, attribution, validation and integration audit |
| `original/main-before-ml.c` | Exact pre-ML working receiver for rollback |
| `tests/` | Developer tests, excluded from the Keil target |
| `ml_experiment/` | Earlier training/data work; not required to run the board |
| `firmware_ml/` | Previous separate staging package; not the active application |

Do not add every C file in the workspace to Keil: backups, tests and previous
experiments contain alternate entry points. The release ZIP includes the current
application, Python tools, guide, model attribution and pre-ML receiver backup;
it excludes raw training datasets, generated host binaries and the private
per-computer nonce state.

Host C checks cover the integrated filter/decoder/Bluetooth queue, including a
255-byte encrypted fan message. A Keil/Arm build, real memory map, stack usage,
board timing and physical microphone reception have not been verified here.
See [docs/VALIDATION.json](docs/VALIDATION.json) and
[docs/MODEL_CARD.md](docs/MODEL_CARD.md) for the evidence and limits.
