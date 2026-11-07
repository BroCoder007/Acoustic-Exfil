# AegisAcoustic: first-time setup and demonstration

This guide starts with an unprogrammed **STM32F401CCU6 Black Pill** and a Windows
PC. Build the receiver, connect the peripherals, generate an encrypted message
inside fan sound, and receive its text on an OLED and Android phone.

The source is already integrated. **Current root `main.c` includes ML by
default**, with the root ML files beside it. Do not use the older
`firmware_ml/main_ml.c` staging copy for these instructions.

## 1. What runs where

**PC:** typed text -> AES-128-CTR frame -> 2000/3000 Hz data tones mixed with
fan sound -> saved WAV -> speaker.

**STM32:** analog microphone -> 40 kHz ADC -> sample queue -> trained ML filter
-> existing FSK decoder -> CRC check -> AES decryption -> OLED and HC-05.

The model is already trained and exported as C constants. No Python, training
data or TensorFlow installation is needed on the board. The Black Pill USB
connection can supply power; this firmware does not implement a USB serial
terminal or receive audio through USB.

## 2. Hardware you need

| Item | Requirement |
| --- | --- |
| Board | STM32F401CCU6 Black Pill with the existing 25 MHz crystal |
| Microphone | Amplified **analog waveform output**, compatible with a 0..3.3 V ADC input |
| OLED | SSD1306-compatible **128 x 64 I2C**, address `0x3C` |
| Bluetooth | HC-05 Bluetooth Classic serial module |
| Programmer | ST-Link with SWD support |
| Other | Wires, suitable power/USB cable and the existing 2 kOhm series resistor |
| Playback/monitor | PC speaker and Android phone with a Classic serial terminal |

An I2S/PDM/USB microphone, a digital sound-detection `DO` output, an SPI OLED
or a BLE-only module is not a drop-in replacement. Use a microphone's analog
`OUT`/`AO` waveform pin. The exact microphone model was not supplied; verify
its supply/output specifications.

The [ST specifications](https://www.st.com/en/microcontrollers-microprocessors/stm32f401cc.html)
describe the Cortex-M4F, 84 MHz maximum, 256 KB Flash and 64 KB RAM used here.

## 3. Wiring

Disconnect power while wiring. Follow **printed pin names**; physical pin order
varies between modules. Every module and programmer must share ground.

| Signal | STM32 connection | Notes |
| --- | --- | --- |
| Mic analog `OUT`/`AO` | `PA0` through 2 kOhm series resistor | ADC input |
| Mic ground | `GND` | Common ground |
| Mic supply | Its rated supply | Use 3.3 V when the module supports it |
| OLED `SCL` / `SDA` | `PB8` / `PB9` | I2C1 |
| OLED `VCC` / `GND` | `3V3` / `GND` for compatible module | Pull-ups must also be to 3.3 V |
| HC-05 `TXD` | `PA10` | Module TX -> MCU RX |
| HC-05 `RXD` | `PA9` | MCU TX -> module RX, 3.3 V logic |
| HC-05 `GND` | `GND` | Common ground |
| HC-05 `VCC` | Its specified supply | Often 5 V for a regulated breakout; bare module needs its rated 3.3 V supply |
| ST-Link `SWDIO` | `PA13` / `SWDIO` | Programming data |
| ST-Link `SWCLK` | `PA14` / `SWCLK` | Programming clock |
| ST-Link ground | `GND` | Common ground |
| ST-Link voltage reference | Board `3V3`, following probe pinout | A voltage-sense pin is not universally a power output |
| ST-Link reset, if available | `NRST` | Useful for connect-under-reset |

Keep the actual waveform at PA0 within **0..3.3 V**. The resistor alone does
not make a 5 V microphone output safe. Use a suitable bias/level circuit if
needed; do not connect a speaker output directly to PA0.

Use one suitable board power source; do not join competing 3.3 V power outputs.
Keep **BOOT0 low / 0** for normal Flash execution. Leave HC-05 `KEY`/`EN` in its
normal data-mode state according to its instructions. For an already working
circuit, keep the wiring: the ML integration does not move pins.

## 4. Install software once

1. Install [Python for Windows](https://www.python.org/downloads/windows/).
   Python **3.12** was used for validation; the pinned dependencies require
   **3.11 or newer**. Ensure `py` or `python` works in a newly opened terminal.
2. Install [Keil MDK/uVision](https://www.keil.com/download/product/) with an Arm
   compiler. Use Arm Compiler 6 for a new project.
3. In Keil's **Pack Installer**, install **ARM::CMSIS** and
   [Keil::STM32F4xx_DFP](https://www.keil.arm.com/packs/stm32f4xx_dfp-keil/overview/),
   including their dependencies. These supply headers, startup/system support
   and Flash algorithms separately from this project's application sources.
4. Install the [ST-Link USB driver](https://www.st.com/en/development-tools/stsw-link009.html)
   if Windows/Keil does not recognize the probe.
5. On Android, install **Serial Bluetooth Terminal** from its
   [developer's page](https://www.kai-morich.de/android/), or another Classic SPP terminal.

Keil builds/programs the receiver; Python generates WAVs. After installation,
the demonstration does not require training downloads or an internet connection.

## 5. Prepare Python

Extract the complete project to a writable folder, for example
`D:\Embedded System Project`, keeping its Python files together. Open
PowerShell and run these lines individually:

```powershell
Set-Location 'D:\Embedded System Project'
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe check_setup.py
```

Change the first path if you extracted elsewhere. If using another supported
Python version, substitute it for `-3.12`, or use `python -m venv .venv` when
`python --version` shows the intended version.

Expect `PASS` for Python, NumPy, cryptography and the required files, followed
by `PC setup ready.` This checks PC setup, not hardware. Only NumPy and
cryptography are needed; their pins match the validated environment.

Using the environment's Python directly avoids activation-script restrictions.
No execution-policy changes, `.cmd` file or setup menu are needed. This follows
Python's [venv/pip guide](https://packaging.python.org/en/latest/guides/installing-using-pip-and-virtual-environments/).

## 6. Create a Keil project

For an existing working Keil project, keep its startup/device settings, use
the **current root `main.c`**, add the two root ML C files, and proceed to
section 7. Do not compile the previous receiver at the same time.

For a new project:

1. Open uVision -> **Project -> New uVision Project**.
2. Save `AegisAcoustic.uvprojx` in the same folder as `main.c`.
3. Select **STMicroelectronics -> STM32F401CCUx** (STM32F401CCU6). If absent,
   install the STM32F4 device pack and reopen device selection.
4. In **Manage Run-Time Environment**, select **CMSIS -> CORE** and
   **Device -> Startup**. Resolve dependencies and accept. Use the selected
   device's startup/system files, not a different board's.
5. Right-click **Source Group 1 -> Add Existing Files to Group**. Add exactly:

   ```text
   main.c
   aegis_ml_denoise.c
   aegis_ml_adapter.c
   ```

6. Keep these headers beside those sources:

   ```text
   aegis_ml_denoise.h
   aegis_ml_adapter.h
   aegis_ml_model_data.h
   ```

Headers are not compiled separately. Do not add `main.c.txt` or C files from
`original`, `tests`, `firmware_ml` or `ml_experiment` to the target. The model
header already contains weights; no retraining is needed.

There must be one `main`, one `SysTick_Handler`, one `ADC_IRQHandler` and one
system/startup implementation. Exclude/merge duplicate strong generated
handlers; the startup file's weak defaults are expected and are overridden.

No `.uvprojx` or vendor startup files were present in the supplied workspace;
this procedure creates them on your machine from the installed device pack.
See [Keil's project/file-group guide](https://www.keil.com/support/man/docs/uv4/uv4_ca_projtargfilegr.asp).

## 7. Configure the target

Open **Project -> Options for Target**. Labels vary slightly by compiler:

| Setting | Value |
| --- | --- |
| Device | STM32F401CCUx |
| Compiler for a new project | Arm Compiler 6 |
| Floating Point Hardware | **Single Precision**, FPv4-SP Cortex-M4 |
| C language | C99 or newer; enable C99 Mode in an older compatible AC5 project |
| Optimization | **Level 2 / O2** |
| C/C++ include path | `.` plus the device-pack paths |
| Device define | `STM32F401xC`, if the pack has not added it |
| IROM1 start / size | `0x08000000` / `0x00040000` (256 KB) |
| IRAM1 start / size | `0x20000000` / `0x00010000` (64 KB) |
| Linker | Use target memory layout, unless an existing scatter file defines it |
| Stack reservation | Start with **4096 bytes / `0x1000`** |
| Output | Create HEX File if you want a `.hex` as well |

For the usual assembly startup, set `Stack_Size EQU 0x00001000` using its
configuration wizard/text. For a C startup or scatter-file stack definition,
adjust that mechanism instead; do not create a second startup/stack. Actual
stack high-water usage still needs measurement on the board.

Keep the standard C/math libraries linked: `sinf`, `cosf`, `logf` and `expf`
are used. CMSIS-DSP, X-CUBE-AI, TensorFlow Lite Micro, HAL initialization and
CubeMX application code are not required.

`main.c` already defaults to **ML on**, **84 MHz** and **2000/3000 Hz**. Remove
an old target-level `AEGIS_ENABLE_ML=0` override if present. Do not define
`DM_HOST_TEST` for the board. Keep `USE_ULTRASONIC=0`: this model suppresses
the ultrasonic 18/19 kHz channel.

The clock setup assumes the existing **25 MHz crystal** and normally powered
3.3 V board. It sets AHB/APB1/APB2 to 84/42/84 MHz, Flash latency 2 and regulator
scale 2, adjusting ADC, timer, OLED and UART together. A different crystal
requires adapting this configuration. If `HSE_VALUE` is explicitly defined,
use `25000000`. Keil's Xtal field alone does not configure hardware clocks.

## 8. Build and flash

1. Choose **Project -> Rebuild all target files**. Resolve errors in Build Output.
2. Check the map/memory report: Flash must fit 256 KB; RAM including RW/ZI,
   stack, heap and other reservations must fit 64 KB. The filter adds
   **7528 bytes static RAM** and **9952 bytes model constants**, plus code,
   libraries and stack. These are not the full application's link sizes.
3. Connect/power the board and ST-Link with BOOT0 low.
4. **Options for Target -> Debug -> Use:** choose **ST-Link Debugger**. Open
   **Settings**, select **SW / SWD**, and confirm probe/target detection. A
   modest SWD speed such as 1 MHz is a reasonable starting setting.
5. Under **Utilities**, use the same target driver. In **Flash Download**,
   select the pack's STM32F401 on-chip Flash algorithm at `0x08000000` covering
   256 KB. Enable programming, verification and **Reset and Run** if available.
6. Choose **Flash -> Download**. After successful download, reset/run the
   board. In a debug session, press Run; a program halted at `main` is not listening.

If connection fails, check power, ground, SWDIO/SWCLK, BOOT0 and reset. Try
lower SWD speed or connect-under-reset with NRST connected. A toolchain
license/code-size error needs an appropriate compiler edition, not suppressed
errors or a download of an old build.

References: [ST clock example](https://github.com/STMicroelectronics/STM32CubeF4/blob/master/Projects/STM32F401RE-Nucleo/Templates/Src/main.c),
[Keil Flash programming](https://www.keil.com/products/uvision/db_trg_flash.asp),
[Keil memory layout](https://www.keil.com/support/man/docs/uv4/uv4_dg_adsld.asp).

## 9. Check the idle receiver and Bluetooth

Expect **AEGIS ML 84M XTAL**, a listening state, **ML ON**, and **DR 0** on the
OLED. `RC` means the internal oscillator; `25M`/`16M` means a slower fallback.

| Field | Meaning |
| --- | --- |
| `F0`, `F1` | Data-tone strengths after filtering |
| `PP`, `DC` | Raw microphone swing and average bias; bias depends on module |
| `DR` | Lost samples / ADC overruns; should stay **0** |
| `ML ON`, time in `US` | Filter active and peak filter/decoder burst duration |
| `OK`, `BAD` | Valid frames / acquired frames rejected |
| `LOCKED` | Preamble found; reception is still in progress |

A 128-sample block arrives every **3200 us**. **ML SLOW** appears above **2400 us**
peak burst time. This is a headroom warning, not total CPU utilization; check
DR across the whole transmission too. A zero timing at startup can precede
the first completed block.

To connect the phone:

1. Put HC-05 in normal data mode, not AT configuration mode.
2. Pair in Android settings using its configured PIN, commonly `1234` or `0000`.
3. Open Serial Bluetooth Terminal, grant Bluetooth/Nearby Devices permissions,
   choose **Devices -> Bluetooth Classic -> HC-05**, and connect.
4. Within about two seconds expect a status line such as:

   ```text
   AEGIS ALIVE ML ON CLK=84M MAX_US=1200 DR=0 RX=0/?
   ```

`1200` is an illustration, not a measured board result. The module-to-MCU UART
must be **9600 baud, 8 data bits, no parity, 1 stop bit**. A phone setting does
not necessarily reconfigure the module's UART. A previously reconfigured HC-05
must match `BT_BAUD` in `main.c`.

If the terminal is blank, fix that path first: pairing, Classic rather than
BLE, normal mode, crossed TX/RX, common ground, baud and a running MCU. The
code echoes phone input as an optional two-way check. Missing OLED hardware
does not intentionally stop Bluetooth service.

## 10. Send HELLO

With the receiver running and phone connected, run from the project folder:

```powershell
.\.venv\Scripts\python.exe ultrasonic_tx.py
```

The only prompt is `Message:`. Type `HELLO` and press Enter. The program prints
the saved location under `aegis_out`, with a new name like
`fan_message_YYYYMMDD_HHMMSS_ffffff.wav`. It saves the WAV; it does not
automatically play it.

Open the printed file and play it through the PC speaker. Start with moderate
volume and a nearby speaker, for example 10-30 cm from the mic; that is a
starting arrangement, not a validated range. Avoid clipping. Start from the
beginning and let the entire file play so the preamble and CRC are received.

`HELLO` takes about **9.16 seconds**. Expected terminal output includes:

```text
RX LOCKED: preamble found; waiting for full frame
AES: HELLO
```

Heartbeats/progress may appear between them. The OLED shows `RECEIVED`, the
`OK` count increases, and `AES: HELLO` appears. PC13's LED turns on briefly
after a complete valid message. `LOCKED` is not completion; text is published
only after the full frame passes CRC. `ML ON` is the ML indicator, not that LED.

## 11. Longer messages and daily use

Run the same sender command for each new message. The maximum is **255 UTF-8
bytes**, not necessarily 255 characters. Many non-ASCII characters use multiple
bytes; use ASCII for the first demo because the OLED font is limited to basic
printable characters. The phone receives the complete line; the OLED scrolls.

Encrypted audio duration at the current rate is:

```text
seconds = 1.0 + (payload_bytes + 12) * 8 * 0.060
```

Five bytes take 9.16 s, 50 take 30.76 s, and 255 take **129.16 s**. Do not stop
at `LOCKED`. There is one transmission and no acknowledgment/automatic retry.

For daily use: power/run the already programmed board, connect the terminal,
run Python, enter the message and play the saved WAV. Reflashing or retraining
is unnecessary unless you change firmware/model. With an already configured
Python environment, `python ultrasonic_tx.py` works as before. `aegis_menu.py`
also defaults to the same single-message prompt; advanced settings are optional.

## 12. Troubleshooting

| Observation | Check / next action |
| --- | --- |
| `python`/`py` missing | Install Python; reopen the terminal or use its actual path |
| Missing NumPy/cryptography | Install requirements with the **same** environment's Python used to run the sender |
| `stm32f4xx.h` missing | Check device pack, CMSIS CORE, Device Startup and include paths |
| ML header missing | Keep the three headers beside `main.c`; include `.` |
| ML function undefined | Add both root ML C files to the target |
| Duplicate main/IRQ symbols | Exclude earlier receivers, backups, tests and duplicate generated strong handlers |
| Floating point compile error | Select Cortex-M4 Single Precision hardware |
| OLED blank but heartbeat works | Check SSD1306 128x64 I2C/0x3C, PB8/PB9, 3.3 V pull-ups and ground |
| OLED works but terminal blank | Check HC-05 data mode, 9600 UART, TX/RX and Classic phone connection |
| `PP` barely changes | Check analog mic output, PA0, power, speaker and gain |
| Large `PP` but no lock | Restart from beginning; check 2/3 kHz profile, distortion, mic response and level |
| Locked but no text yet | Wait for the complete file; maximum message takes over two minutes |
| `CRC ERROR` | Incorrect received bits; improve gain/volume/distance/echo and replay |
| `SIGNAL LOST` | Carrier disappeared or became weak; check continuous playback and signal level |
| `HEADER ERROR` | Acquired bits did not form a supported header; check profile and audio quality |
| `SAMPLE LOSS` / rising DR | Check 84 MHz, O2, FPU, no debugger pauses and processing time |
| `ML SLOW` | Check clock/FPU/optimization; compare with bypass below |
| Wrong text despite passing CRC | Check identical AES keys; CRC covers ciphertext corruption, not key correctness |

To verify a saved WAV without hardware, replace the filename below:

```powershell
.\.venv\Scripts\python.exe aegis_tools.py decode-wav '.\aegis_out\YOUR_FILE.wav' --start-s 0.5
```

Expect your payload and `CRC: PASS`. This offline decoder does not test the
STM32 ML path. `aegis_tools.py selftest` checks protocol/generated-audio logic.

## 13. ML comparison and rollback

For a comparison at the new clock, change the default `AEGIS_ENABLE_ML` at
the top of `main.c` to `0`, rebuild and flash. The title says `AEGIS RAW` and
status says `ML BYPASS`. Set it to `1` and rebuild to re-enable ML. Keep
`main.c.txt` synchronized if you maintain that viewing copy. Slow processing
does not silently bypass the filter.

For the exact pre-ML receiver, exclude current `main.c` and both ML C files,
include **`original/main-before-ml.c`** instead, then rebuild/flash. It retains
the old clock. Do not compile both receivers together. Original checksums are
in `original/PRE_ML_SHA256.json`. Python does not need changing for rollback.

## 14. Model scope and verification

The model learns spectral attenuation to preserve the known data tones while
reducing some noise. It is not a generic speech cleaner or a detector/decryptor
for arbitrary acoustic protocols or unknown keys. The demo uses the shared
public key `00112233445566778899aabbccddeeff`. AES-CTR plus CRC is not
authenticated encryption. Keep the sender's `.nonce_state.sqlite3` across runs
while reusing its key; do not reset it or distribute that per-computer state.

Training used synthesized data audio plus a small real environmental-noise
subset and synthetic fan noise. Same-band noise, clipping and lost samples
are not universally recoverable. Different rooms/mics/fans can change results.
See `docs/MODEL_CARD.md`, `docs/SOURCES.md` and the retained attribution files.

The host C checks cover the integrated filter/decoder/Bluetooth queue, including
a 255-byte encrypted fan frame, full output line, gap recovery and corrupt-CRC
rejection. The one-prompt sender was also checked. Reports are in
`docs/VALIDATION.json` and `docs/INTEGRATION_AUDIT.json`.

**Keil/Arm compilation, target memory map, stack usage, board timing and physical
microphone reception have not been verified here.** No prebuilt `.hex` or
successful physical demonstration is claimed. Perform the build, timing, DR
and HELLO checks above on the first hardware run.
