# Long-message receiver repair

Current setup: **FIRST_TIME_SETUP.md**. Root `main.c` now includes the optional
ML front end, enabled by default. This document records the earlier decoder
repairs, which remain part of the integrated receiver.

## Update: LOCKED but no Bluetooth message

If the phone receives `AEGIS ALIVE`, the Bluetooth output path is carrying data.
`LOCKED` only means the acoustic preamble was detected. Text is sent after the
complete frame passes CRC; an encrypted 255-byte frame needs 128.16 seconds of
uninterrupted data, plus the WAV's silence padding. A bad bit anywhere in the
frame can prevent publication. The previous screen did not explain that failure.

The updated **main.c** now reports:

| Phone output | Meaning |
| --- | --- |
| `AEGIS ALIVE V3` | Current firmware is running and Bluetooth output reaches the phone |
| `RX LOCKED: preamble found; waiting for full frame` | Sync detected; message is incomplete |
| `AEGIS ALIVE \| RX 1200/2104 bits` | Reception progress; total includes the body header, nonce and CRC |
| `AES: ...` or `RX: ...` | CRC passed; complete decoded payload queued |
| `RX ERROR: CRC mismatch; audio bits corrupted` | The full frame arrived but contained errors |
| `RX ERROR: signal lost or too weak` | Three consecutive symbol decisions were too weak/ambiguous |
| `RX ERROR: invalid mode/length header` | Sync was followed by an invalid header |
| `RX ERROR: ADC/sample buffer gap` | Sampling was interrupted by lost data |

The OLED shows a percentage while locked and displays the failure reason for
four seconds. A busy Bluetooth queue now defers the complete decoded message
instead of silently truncating its text. Host tests cover all 255 output bytes,
queue saturation/retry, queue wrap, and each decoder error reason.

Rebuild and flash **main.c**. `main.c.txt` now has identical V3 source text. First send a short
plain `HELLO`, then an AES `HELLO`, then the longer sentence. Watch the final
error or success line after all playback has finished. If the phone still shows
only the old `AEGIS ALIVE` text with no `V3`, check which source Keil compiled and
which firmware was flashed. These diagnostics identify the next hardware fault;
they do not establish that the real microphone path has been repaired.

Use **main.c** in Keil in place of the old receiver. It is based on the pasted
version and includes the OLED and HC-05 Bluetooth output. `main.c.txt` contains
the same V3 code for copying into an editor. The earlier receiver text and
Python versions are saved in `original/`.

## What caused the problem

- Both ends imposed a 32-byte payload limit. Longer sentences could not be sent.
- After the preamble the old decoder sampled every 2400 samples forever, without
  timing correction. Independent playback/ADC clocks can accumulate enough
  error to move a long transmission's decisions across bit boundaries.
- The old 60 ms coherent tone measurement was sensitive to frequency mismatch.
- A single symbol below `AMP_MIN` discarded the entire frame immediately.
- The Python CLI generated random frequencies even for `--profile audible`.
  The pasted C receiver measured only fixed frequencies. The menu defaulted to
  fixed tones but its help incorrectly described the receiver as a band decoder.
- The OLED displayed only 17 payload characters, making a correctly received
  longer message appear incomplete.

## Changes

- Payload limit is now **255 UTF-8 bytes** at both ends, using the existing
  one-byte length field and unchanged CRC/AES framing.
- The receiver sums independent short-hop powers and makes decisions from a
  30 ms window in each 60 ms bit, away from transitions.
- Confident transitions adjust the next decision time by at most one hop.
- Lock tolerates a brief weak interval. Three consecutive weak decisions abort;
  only a valid complete CRC publishes a message. This does not correct lost bits.
- Sample-ring overflow and ADC overrun cause fresh acquisition instead of
  decoding samples stitched across a gap. OLED transfers yield to a sample backlog.
- Bluetooth buffering holds a complete maximum-length message. The OLED scrolls
  the last validated text automatically.
- Sender profiles now have their stated meanings: `audible` = 2000/3000 Hz
  (default), `ultrasonic` = 18000/19000 Hz. `randomized` remains available for
  offline experiments and is incompatible with this fixed-tone firmware.

## Try it on the board

1. Add `main.c` to the existing Keil STM32F401CCU6 project; avoid compiling a
   second file containing `main`. Enable Single Precision floating point and
   compiler optimization, then build and flash. Keep `USE_ULTRASONIC` at **0**.
2. Use the updated Python files. Run this from the project folder:

   ```powershell
   python ultrasonic_tx.py message --profile audible --mode 0 --text "This is a longer sentence to test microphone locking and decoding." --out long_phrase_test.wav --play
   ```

   A matching `long_phrase_test.wav` is already provided. Existing WAVs have not
   been regenerated and may contain random carriers.
3. Expect `TONE HEARD`, then `LOCKED` after about 1.9 seconds of preamble. Keep
   playback going until the whole frame ends; `RECEIVED` means the CRC passed.
   This example takes about 36 seconds including silence. At 60 ms per bit,
   the maximum encrypted 255-byte frame takes about 128 seconds of data.
4. Check `OK` increases and `DR` stays **0**. A rising `DR` means actual samples
   were lost; adding a larger text buffer cannot repair that. `BAD` means a
   partial/invalid frame was rejected. The phone receives the full message;
   the OLED scrolls it using the original uppercase font.
5. Once plain reception works, use `--mode 1` for AES. Both sides must use the
   same key. Do not delete the nonce database while reusing its key.

## Validation and limits

`tests/test_receiver.py` exercises the **actual C decoder**, compiled into a
Windows test DLL by `tests/receiver_host.c`, using simulated 12-bit ADC input.
Passing cases include maximum-length plain/encrypted messages, injected noise,
clock offsets through +/-1% for audible text, all 16 hop alignments, an actual
sender-rendered waveform, a low-amplitude bit, CRC rejection, sample-gap recovery,
noise-only rejection, and protocol length checks. The existing Python self-test
also passes. Ultrasound is covered with a smaller clock offset (+0.02%).

To rebuild/run these checks in an x64 Visual Studio Developer Command Prompt:

```bat
cl /nologo /O2 /W4 /std:c11 /LD tests\receiver_host.c /Fe:tests\receiver_host.dll /Fo:tests\receiver_host.obj /link /IMPLIB:tests\receiver_host.lib
python tests\test_receiver.py
```

Host tests do not validate the Keil device build, interrupt throughput, analog
microphone, speaker response, room echoes, OLED or Bluetooth hardware. No board
was connected for testing. Clock correction needs bit transitions; very long
runs of identical bits still limit clock tolerance. CRC detects corruption but
does not repair it or retry automatically. Text longer than 255 bytes needs a
separate multi-frame protocol.

ADC overrun handling follows ST's [STM32F401 reference manual (RM0368)](https://www.st.com/resource/en/reference_manual/DM00096844.pdf).
