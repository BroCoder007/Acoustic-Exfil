# Fan-style encrypted-audio demonstration

For the current integrated ML receiver and full beginner setup, read
**FIRST_TIME_SETUP.md**. The sender's one-message workflow described here
remains the same; root `main.c` now applies ML before decoding.

## V3: fan audio is heard but does not lock

Run `python ultrasonic_tx.py` or `python aegis_menu.py` from this folder. Enter
your message at the single **Message:** prompt. The program saves an encrypted
fan-sound WAV in `aegis_out` automatically, using data/background ratio 0.85.
Each output gets a new filename. There are no setup or confirmation questions.
`python aegis_menu.py --fan-demo` is an alias for this same simple flow.

Flash the V3 `main.c` first; the OLED title should read **AEGIS V3**. `main.c.txt`
now contains the same code for users copying the text into Keil. Compile only
one copy of the receiver. Earlier originals are preserved in `original/`.

Play `fan_check_v3.wav` fully (9.16 seconds); successful phone output is
`AES: HELLO`. The earlier reported F0/F1 peaks near 25-33 ADC counts and DR=0
show that both bands were reaching the microphone with no reported sample gap;
they do not prove that the individual sync bits were classified correctly.

V2 cleared a phase's entire preamble history on any window with confidence below
0.35. V3 retains the symbol decisions and requires an exact sync match together
with amplitude evidence and average quality. It still requires CRC before
publishing any message. The new waveform also gives the active data tone more
contrast over its quiet residual overtone, to tolerate unequal speaker/mic
response. This does not establish reliable masking in the physical room.

`tests/test_fan_acquisition.py` includes a frozen V2 waveform that failed before
the acquisition fix and now decodes. Additional passing cases attenuate either
data band by 6 dB and add ADC noise, while the fan-only control stays rejected.
These are controlled simulations, not measurements of the user's board.

Once HELLO works, try a longer message, then reduce data strength gradually if
desired. A lower ratio is a new reception condition and may fail. Increasing
the total speaker volume cannot selectively improve data relative to its mixed
fan background, and microphone clipping can make decoding worse.

`fan_demo.wav` contains the AES-encrypted message **DEMO ALERT**, mixed with
synthetic airflow and mechanical hum. `fan_only_control.wav` contains a matching
fan background with no message. Both last 11.56 seconds; their peak levels are
normalized separately, so they are not a calibrated loudness comparison.

This is a controlled example of detecting a known acoustic communication
protocol amid background sound. It is not a recording of a real datacenter and
does not establish that the signal is inaudible to people. Noise masking depends
on the frequency relationship between signal and noise, not just overall
volume; see the primary study [The measurement of critical masking bands](https://pubmed.ncbi.nlm.nih.gov/1186166/).

## Play the demonstration

Use the V3 `main.c` with `USE_ULTRASONIC=0` and the existing demo AES key.
Connect the phone's Bluetooth terminal first.

1. Play the **fan-only control**. A complete message should not be reported.
2. Play **fan_demo.wav** from start to finish. Expected successful output is
   `AES: DEMO ALERT` after roughly 11 seconds.
3. Listen for whether the added overtones still sound too obvious in your room.
   The firmware's progress/error messages identify decoding failures. Host tests
   passing does not resolve the earlier hardware reception issue by itself.

To make another message, run from the project folder:

```powershell
python ultrasonic_tx.py message --profile fan-demo --mode 1 --text "HELLO" --data-level 0.85 --seed 42 --out my_fan_demo.wav --play
```

Or run `python aegis_menu.py` and enter only your message. Optional settings and
diagnostic tools remain under `python aegis_menu.py --advanced`. Its generic
Send a message action also offers **aes** and signal **fan**. The **random** option
remains incompatible with this fixed-frequency firmware.

## How the sound differs

The fan sound continues before and after the data. Low hum, harmonics, airflow
and gentle flutter provide the background. Both 2 kHz and 3 kHz overtones run
continuously during the data; their balance changes smoothly with the bits.
The existing C decoder compares their powers and can use the same framing,
timing, CRC and AES key. This is compatible with its fixed-frequency detector;
the V3 acquisition improvement is needed for the formerly failing noisy case.

The `--data-level` setting controls approximate carrier/background RMS ratio
before final peak normalization. The current default is **0.85**; the earlier
`fan_demo.wav` example was generated at 0.55. Lowering it can make
reception fail; zero makes a fan-only control. This parameter is not a promise
of perceptual masking, decoding range or compatibility with other fan noises.
Use normal playback levels rather than trying to overcome noise with volume.

## What is validated

`tests/test_fan_demo.py` checks generated sound at 44.1/48 kHz against the
offline decoder and the actual C decoder in the host test DLL, including an
encrypted 255-byte message at 48 kHz. The fan-only controls publish no decoded
message in those tests. The normal receiver regression checks also pass.
These are synthetic ADC tests without microphone distortion, echoes, speaker
frequency response, room noise or a test of perceived audibility.

Run `python tests/test_fan_demo.py` after building the test DLL as described in
`RECEIVER_FIX.md`. `python tests/make_fan_demo.py` regenerates the example pair
and allocates a fresh AES nonce. Keep the nonce database while reusing the key.

For the project presentation, distinguish **recognizing the demonstration's
known protocol** from identifying every possible threat actor's audio. AES
encryption itself has no special acoustic signature. This receiver can decrypt
the demo because it knows the shared key; it cannot automatically decrypt an
unknown actor's AES traffic. A general detector would need to cover additional
modulations/protocols and be evaluated on real benign and malicious recordings.
