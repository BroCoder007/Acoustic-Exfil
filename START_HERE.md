# AegisAcoustic with integrated ML

New to the project? Follow **[FIRST_TIME_SETUP.md](FIRST_TIME_SETUP.md)** for
wiring, installation, creating a Keil project, flashing and the first demo.

The current root `main.c` already includes ML, enabled by default. Compile it
with root `aegis_ml_denoise.c` and `aegis_ml_adapter.c`, keeping the three ML
headers beside them. Keep your device-pack startup/CMSIS files. Exclude earlier
receivers, backups and host tests from Keil.

Once flashed, check **AEGIS ML 84M XTAL**, **ML ON**, and **DR=0**. Connect the
HC-05 Bluetooth Classic terminal and confirm `AEGIS ALIVE` every two seconds.

From the project folder, after installing `requirements.txt`:

```powershell
.\.venv\Scripts\python.exe ultrasonic_tx.py
```

Enter your message at **Message:**. Open and play the saved WAV in **aegis_out**.
It contains encrypted data with fan sound. There are no setup questions. Saving
the WAV does not automatically play it.

Expected phone output after the full transmission: **AES: your message**.
`LOCKED` means reception is in progress. A maximum 255-byte encrypted message
takes about **129.16 seconds**. The OLED scrolls; the phone gets the full line.

With an already configured Python environment, `python ultrasonic_tx.py`
works as before. `python aegis_menu.py` offers the same one-message flow.

The exact previous receiver is **original/main-before-ml.c**. The integrated
source passed host checks; target build/timing still need the hardware checks
in the full guide. Model details are in **docs/MODEL_CARD.md**.
