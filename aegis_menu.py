#!/usr/bin/env python3
"""AegisAcoustic: enter a message to create encrypted fan sound.

Run:   python aegis_menu.py
       python aegis_menu.py --advanced  (optional diagnostics/settings menu)

The default flow asks only for the message and saves an encrypted fan WAV.
The optional advanced menu includes transmitter controls and offline diagnostics.

At any prompt:  Enter = keep the value in [brackets],  /back or Ctrl+C = cancel
the current action.

Needs: numpy and cryptography.  Playback uses winsound on Windows, or
sounddevice elsewhere (pip install sounddevice).  Keep this file in the same
folder as ultrasonic_tx.py and aegis_tools.py.
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

import numpy as np

import aegis_tools as tools
import ultrasonic_tx as tx

RULE = "-" * 72
DEMO_KEY_HEX = tx.DEMO_KEY.hex()
FIRMWARE_F0, FIRMWARE_F1 = 2000.0, 3000.0   # what the shipped STM32 firmware listens for

SETTINGS = {
    "out_dir": Path("aegis_out"),
    "amp": 0.5,
    "fs": 48000,
    "key_hex": DEMO_KEY_HEX,
    "signal": "fixed",      # fixed or fan for the board; random for offline experiments
    "f0": FIRMWARE_F0,
    "f1": FIRMWARE_F1,
}
LAST: dict[str, dict] = {}


# ----------------------------------------------------------------- input helpers
class Cancel(Exception):
    """The user cancelled the current action (/back, Ctrl+D or end of input)."""


def _read(prompt: str) -> str:
    try:
        raw = input(prompt)
    except EOFError:
        raise Cancel() from None
    raw = raw.strip()
    if raw.lower() == "/back":
        raise Cancel()
    return raw


def _fmt(value) -> str:
    return f"{value:g}" if isinstance(value, float) else str(value)


def ask(prompt, default=None, parse=None, show=None, optional=False):
    """Ask until the answer parses. Enter keeps the default."""
    shown = ""
    if default is not None:
        shown = f" [{show(default) if show else _fmt(default)}]"
    while True:
        raw = _read(f"  {prompt}{shown}: ")
        if raw == "":
            if default is not None:
                return default
            if optional:
                return None
            print("    ! a value is required")
            continue
        try:
            return parse(raw) if parse else raw
        except ValueError as exc:
            print(f"    ! {exc}")


def ask_yn(prompt: str, default: bool) -> bool:
    hint = "Y/n" if default else "y/N"
    while True:
        raw = _read(f"  {prompt} [{hint}]: ").lower()
        if raw == "":
            return default
        if raw in ("y", "yes"):
            return True
        if raw in ("n", "no"):
            return False
        print("    ! answer y or n")


def num(kind, lo, hi, lo_open=False):
    def parse(text):
        try:
            value = kind(text)
        except ValueError:
            raise ValueError("not a valid number") from None
        too_low = value <= lo if lo_open else value < lo
        if too_low or value > hi:
            raise ValueError(f"must be {'>' if lo_open else '>='} {_fmt(lo)} and <= {_fmt(hi)}")
        return value
    return parse


def parse_text(text: str) -> str:
    size = len(text.encode("utf-8"))
    if not 1 <= size <= tx.MAX_PAYLOAD:
        raise ValueError(f"message must be 1..{tx.MAX_PAYLOAD} UTF-8 bytes (this one is {size})")
    return text


def parse_mode(text: str) -> int:
    table = {"0": 0, "plain": 0, "p": 0, "1": 1, "aes": 1, "a": 1}
    if text.lower() not in table:
        raise ValueError("type plain or aes")
    return table[text.lower()]


def parse_signal(text: str) -> str:
    table = {"random": "random", "r": "random", "fixed": "fixed", "f": "fixed",
             "fan": "fan", "fan-demo": "fan"}
    if text.lower() not in table:
        raise ValueError("type fixed, fan, or random")
    return table[text.lower()]


def parse_fs(text: str) -> int:
    if text not in ("44100", "48000"):
        raise ValueError("sample rate must be 44100 or 48000")
    return int(text)


def parse_key(text: str) -> str:
    try:
        key = bytes.fromhex(text)
    except ValueError:
        raise ValueError("the key must be hex digits (0-9, a-f)") from None
    if len(key) != 16:
        raise ValueError("the key must be exactly 32 hex digits (16 bytes)")
    return key.hex()


def parse_seed(text: str):
    if text.lower() in ("random", "none"):
        return None
    try:
        return int(text)
    except ValueError:
        raise ValueError("type a whole number, or the word random") from None


def resolve_out(name: str) -> Path:
    path = Path(name.strip('"')).expanduser()
    if path.suffix.lower() != ".wav":
        path = path.with_name(path.name + ".wav")
    if not path.is_absolute() and path.parent == Path("."):
        path = SETTINGS["out_dir"] / path
    return path


def default_out(prefix: str) -> Path:
    return SETTINGS["out_dir"] / f"{prefix}_{datetime.now():%Y%m%d_%H%M%S}.wav"


def recent_wavs(limit: int = 9) -> list[Path]:
    seen, found = set(), []
    for folder in (SETTINGS["out_dir"], Path(".")):
        if folder.is_dir():
            for wav in folder.glob("*.wav"):
                real = wav.resolve()
                if real not in seen:
                    seen.add(real)
                    found.append(wav)
    found.sort(key=lambda f: f.stat().st_mtime, reverse=True)
    return found[:limit]


def pick_wav(default=None) -> Path:
    files = recent_wavs()
    if files:
        print("  WAV files found (newest first):")
        for i, wav in enumerate(files, 1):
            info = wav.stat()
            when = datetime.fromtimestamp(info.st_mtime).strftime("%Y-%m-%d %H:%M:%S")
            print(f"    {i}) {wav}   ({info.st_size / 1024:.0f} KB, {when})")

    def parse(text: str) -> Path:
        if text.isdigit():
            index = int(text)
            if 1 <= index <= len(files):
                return files[index - 1]
            raise ValueError("no such number in the list")
        path = Path(text.strip('"')).expanduser()
        if not path.is_file():
            raise ValueError(f"file not found: {path}")
        return path

    if default is None and files:
        default = files[0]
    return ask("WAV file (list number or path)", default, parse, show=str)


# ----------------------------------------------------------------- review / approval
def review(title: str, rows: list[tuple[str, str]]) -> None:
    print()
    print(f"--- Review: {title} " + "-" * max(3, 72 - len(title) - 13))
    width = max((len(k) for k, _ in rows), default=0)
    for key, value in rows:
        print(f"  {key:<{width}}   {value}" if key == "" else f"  {key:<{width}} : {value}")
    print(RULE)


def run_with_approval(title, params, collect, rows, execute):
    """collect -> review -> (y) run | (e) edit | anything else cancels."""
    params = collect(params)
    while True:
        review(title, rows(params))
        raw = _read("  Run this now?  [y] yes   [e] edit   [n] cancel   (Enter = cancel): ").lower()
        if raw in ("y", "yes"):
            print()
            try:
                execute(params)
            except (ValueError, OSError, AssertionError) as exc:
                print(f"\n  FAILED: {exc}")
            return params
        if raw in ("e", "edit"):
            params = collect(params)
            continue
        print("  Cancelled - nothing was run.")
        return params


# ----------------------------------------------------------------- audio helpers
def render_fixed(frame: bytes, fs: int, amp: float, lead: float, f0: float, f1: float) -> np.ndarray:
    """Plain two-tone BFSK (bit 0 = f0, bit 1 = f1), 60 ms per bit, continuous phase."""
    samples_per_bit = round(fs * tx.BIT_MS / 1000)
    bits = np.asarray(tx.to_bits(frame), dtype=np.uint8)
    freqs = np.repeat(np.where(bits == 1, f1, f0).astype(np.float64), samples_per_bit)
    audio = np.sin(np.cumsum(2.0 * np.pi * freqs / fs))
    ramp_n = round(0.003 * fs)
    ramp = 0.5 - 0.5 * np.cos(np.linspace(0.0, np.pi, ramp_n))
    audio[:ramp_n] *= ramp
    audio[-ramp_n:] *= ramp[::-1]
    audio *= amp
    return np.concatenate((np.zeros(round(lead * fs)), audio, np.zeros(round(0.5 * fs))))


def play_wav(path: Path) -> None:
    print("  Playing... (Ctrl+C stops waiting)")
    try:
        tx.play(path)
    except KeyboardInterrupt:
        try:
            import sounddevice as sd
            sd.stop()
        except Exception:
            pass
        print("  Playback interrupted.")
    except Exception as exc:  # no sound device, missing module, etc. - the WAV is already saved
        print(f"  Playback failed: {exc}")
        print("  The WAV file is saved. Play it with any audio player, or: pip install sounddevice")


def verify_wav(path: Path, start_s: float, key: bytes, expect: str | None) -> None:
    try:
        samples, fs = tools.read_wav(path)
        _frame, payload, mode, _nonce, freqs = tools.parse_frame_from_audio(
            samples, fs, round(start_s * fs), key=key)
    except ValueError as exc:
        print(f"  VERIFY: FAIL - {exc}")
        return
    text = payload.decode("utf-8", "replace")
    ok = expect is None or payload == expect.encode("utf-8")
    print(f"  VERIFY: {'PASS' if ok else 'MISMATCH'} - decoded \"{text}\" "
          f"({'AES' if mode else 'plain'}, CRC ok, carriers {min(freqs):.0f}-{max(freqs):.0f} Hz)")


# ----------------------------------------------------------------- 1) send a message
def message_defaults() -> dict:
    last = LAST.get("message", {})
    return {
        "text": last.get("text", "AEGIS-42"),
        "mode": last.get("mode", 0),
        "signal": SETTINGS["signal"],
        "f0": SETTINGS["f0"], "f1": SETTINGS["f1"],
        "fan": True, "seed": None, "data_level": tx.DEFAULT_FAN_DATA_LEVEL,
        "repeat": 1, "gap": 2.0,
        "amp": SETTINGS["amp"], "fs": SETTINGS["fs"],
        "key_hex": SETTINGS["key_hex"],
        "out": default_out("msg"),
        "verify": True, "play": True,
    }


def collect_message(p: dict) -> dict:
    p = dict(p)
    print("\n  Answer each question, or press Enter to keep the value in [brackets].")
    p["text"] = ask("Message text (1-255 bytes)", p["text"], parse_text)
    p["mode"] = ask("Encryption: plain or aes", p["mode"], parse_mode,
                    show=lambda m: "aes" if m else "plain")
    p["signal"] = ask("Signal: fixed, fan (fan-style board demo), or random (offline only)",
                      p["signal"], parse_signal)
    if p["signal"] == "fixed":
        p["f0"] = ask("Tone for bit 0 in Hz (1000-4000)", p["f0"], num(float, 1000, 4000))

        def parse_f1(text, f0=p["f0"]):
            value = num(float, 1000, 4000)(text)
            if abs(value - f0) < 300:
                raise ValueError("keep the two tones at least 300 Hz apart")
            return value
        p["f1"] = ask("Tone for bit 1 in Hz (1000-4000)", p["f1"], parse_f1)
    elif p["signal"] == "fan":
        p["data_level"] = ask("Data strength relative to fan (above 0, up to 1)",
                              p["data_level"], num(float, 0, 1, lo_open=True))
        p["seed"] = ask("Fan seed (a number, or 'random')", p["seed"], parse_seed,
                        show=lambda s: s, optional=True)
    else:
        p["fan"] = ask_yn("Add fan-like texture (noise + flutter)", p["fan"])
        p["seed"] = ask("Random seed (a number, or 'random' for a different sound every time)",
                        p["seed"], parse_seed, show=lambda s: s, optional=True)
    p["repeat"] = ask("How many times to send it (1-20)", p["repeat"], num(int, 1, 20))
    if p["repeat"] > 1:
        p["gap"] = ask("Pause between repeats in seconds (0.5-30)", p["gap"], num(float, 0.5, 30))
    p["amp"] = ask("Volume / amplitude (above 0, up to 0.8)", p["amp"], num(float, 0, 0.8, lo_open=True))
    p["fs"] = ask("Sample rate (44100 or 48000)", p["fs"], parse_fs)
    if p["mode"] == 1:
        p["key_hex"] = ask("AES key, 32 hex digits (Enter = current)", p["key_hex"], parse_key)
    p["out"] = ask("Save the WAV as", p["out"], resolve_out, show=str)
    p["verify"] = ask_yn("Afterwards, decode the saved WAV offline to verify it", p["verify"])
    p["play"] = ask_yn("Afterwards, play it through the speakers", p["play"])
    return p


def message_frame_bytes(p: dict) -> int:
    return 4 + 2 + (4 if p["mode"] else 0) + len(p["text"].encode("utf-8")) + 2


def message_cli(p: dict) -> str:
    text = p["text"].replace('"', '\\"')
    parts = ["python ultrasonic_tx.py message", f'--text "{text}"', f"--mode {p['mode']}",
             f"--repeat {p['repeat']}", f"--gap-s {p['gap']:g}", f"--amp {p['amp']:g}",
             f"--fs {p['fs']}", f'--out "{p["out"]}"']
    profile = {"random": "randomized", "fixed": "audible", "fan": "fan-demo"}[p["signal"]]
    parts.extend(("--profile", profile))
    if p["signal"] == "fan":
        parts.extend(("--data-level", str(p["data_level"])))
    if p["key_hex"] != DEMO_KEY_HEX:
        parts.append(f"--key {p['key_hex']}")
    if p["seed"] is not None:
        parts.append(f"--seed {p['seed']}")
    if not p["fan"]:
        parts.append("--no-fan-texture")
    return " ".join(parts)


def rows_message(p: dict) -> list[tuple[str, str]]:
    frame_bytes = message_frame_bytes(p)
    data_s = frame_bytes * 8 * tx.BIT_MS / 1000.0
    total = 0.5 + p["repeat"] * (data_s + 0.5) + (p["repeat"] - 1) * p["gap"]
    rows = [("Action", "generate a WAV file" + (", then play it" if p["play"] else ""))]
    rows.append(("Message", f"\"{p['text']}\" ({len(p['text'].encode('utf-8'))} bytes)"))
    if p["mode"]:
        rows.append(("Mode", "AES-128-CTR encrypted"))
        rows.append(("", f"uses {p['repeat']} new nonce(s) from .nonce_state.sqlite3 - never reused"))
        if p["key_hex"] == DEMO_KEY_HEX:
            rows.append(("", "key: the public DEMO key (fine for testing, not secret)"))
    else:
        rows.append(("Mode", "plain (not encrypted)"))
    if p["signal"] == "fixed":
        rows.append(("Signal", f"two tones: bit 0 = {p['f0']:g} Hz, bit 1 = {p['f1']:g} Hz"))
        if (p["f0"], p["f1"]) == (FIRMWARE_F0, FIRMWARE_F1):
            rows.append(("Firmware", "matches main.c default (USE_ULTRASONIC=0)"))
        else:
            rows.append(("Firmware", "set F0_HZ / F1_HZ in main.c to these exact frequencies first"))
    elif p["signal"] == "fan":
        rows.append(("Signal", "synthetic fan airflow + hum with softly changing 2/3 kHz data overtones"))
        rows.append(("Data strength", f"{p['data_level']:g} carrier/background RMS ratio"))
        rows.append(("Firmware", "matches main.c default; audible whine may remain, room test required"))
    else:
        (lo0, hi0), (lo1, hi1) = tx.BIT_BANDS[0], tx.BIT_BANDS[1]
        rows.append(("Signal", f"random carrier per bit: bit 0 in {lo0:g}-{hi0:g} Hz, bit 1 in {lo1:g}-{hi1:g} Hz"))
        rows.append(("", f"fan texture {'on' if p['fan'] else 'off'}, seed "
                         f"{p['seed'] if p['seed'] is not None else 'random'}"))
        rows.append(("Firmware", "random carriers are incompatible with main.c; choose fixed for the board"))
        rows.append(("", "the offline decoder (menu 3) supports random carriers"))
    rows.append(("Frames", f"{p['repeat']} x {frame_bytes} bytes = {data_s:.2f} s of data each"))
    rows.append(("Length", f"about {total:.1f} s in total"))
    rows.append(("Level", f"amplitude {p['amp']:g}, {p['fs']} Hz sample rate"))
    if p["amp"] > 0.6:
        rows.append(("Warning", "high volume - keep speakers away from your ears"))
    exists = " (already exists - will be overwritten)" if p["out"].exists() else ""
    rows.append(("Writes", f"{p['out']}{exists}"))
    rows.append(("Then", ("verify by offline decode; " if p["verify"] else "") +
                 ("play on the default speakers" if p["play"] else "no playback")))
    if p["signal"] in ("random", "fan"):
        rows.append(("Same as CLI", message_cli(p)))
    return rows


def exec_message(p: dict) -> None:
    key = bytes.fromhex(p["key_hex"])
    payload = p["text"].encode("utf-8")
    fs, amp = p["fs"], p["amp"]
    segments = []
    for i in range(p["repeat"]):
        nonce = tx.allocate_nonce(key) if p["mode"] else 0
        frame = tx.build_frame(payload, p["mode"], nonce, key)
        lead = 0.5 if i == 0 else 0.0
        if p["signal"] == "random":
            seed = None if p["seed"] is None else p["seed"] + i
            segments.append(tx.render(frame, fs, "randomized", amp, lead=lead,
                                      seed=seed, fan_texture=p["fan"]))
        elif p["signal"] == "fan":
            seed = None if p["seed"] is None else p["seed"] + i
            segments.append(tx.render(frame, fs, "fan-demo", amp, lead=lead,
                                      seed=seed, data_level=p["data_level"]))
        else:
            segments.append(render_fixed(frame, fs, amp, lead, p["f0"], p["f1"]))
        if i != p["repeat"] - 1:
            segments.append(np.zeros(round(p["gap"] * fs)))
        print(f"  frame {i + 1}/{p['repeat']}: {len(frame)} bytes, "
              f"{len(frame) * 8 * tx.BIT_MS / 1000:.2f} s, "
              f"{'nonce ' + str(nonce) if p['mode'] else 'plain'}")
    audio = np.concatenate(segments)
    p["out"].parent.mkdir(parents=True, exist_ok=True)
    tx.write_wav(p["out"], audio, fs)
    print(f"  Saved {p['out']}  ({len(audio) / fs:.2f} s, peak {np.max(np.abs(audio)):.3f})")
    if p["verify"]:
        verify_wav(p["out"], 0.5, key, p["text"])
    if p["play"]:
        play_wav(p["out"])


def action_message() -> None:
    LAST["message"] = run_with_approval("Send a message", message_defaults(),
                                        collect_message, rows_message, exec_message)


def action_fan_demo() -> None:
    tx.prompt_fan_message()


# ----------------------------------------------------------------- 2) test tone
def collect_tone(p: dict) -> dict:
    p = dict(p)
    print("\n  A steady tone, handy for checking that the microphone and speaker work.")
    p["freq"] = ask("Frequency in Hz (1000-4000)", p["freq"], num(float, 1000, 4000))
    p["seconds"] = ask("Length in seconds (0.1-30)", p["seconds"], num(float, 0.1, 30))
    p["amp"] = ask("Volume / amplitude (above 0, up to 0.8)", p["amp"], num(float, 0, 0.8, lo_open=True))
    p["fs"] = ask("Sample rate (44100 or 48000)", p["fs"], parse_fs)
    p["out"] = ask("Save the WAV as", p["out"], resolve_out, show=str)
    p["play"] = ask_yn("Play it through the speakers", p["play"])
    return p


def rows_tone(p: dict) -> list[tuple[str, str]]:
    exists = " (already exists - will be overwritten)" if p["out"].exists() else ""
    rows = [("Action", "generate a steady tone" + (", then play it" if p["play"] else "")),
            ("Tone", f"{p['freq']:g} Hz for {p['seconds']:g} s"),
            ("Level", f"amplitude {p['amp']:g}, {p['fs']} Hz sample rate")]
    if p["amp"] > 0.6:
        rows.append(("Warning", "high volume - keep speakers away from your ears"))
    rows.append(("Writes", f"{p['out']}{exists}"))
    rows.append(("Same as CLI", f"python ultrasonic_tx.py tone --freq {p['freq']:g} --seconds {p['seconds']:g} "
                                f"--amp {p['amp']:g} --fs {p['fs']} --out \"{p['out']}\""))
    return rows


def exec_tone(p: dict) -> None:
    fs = p["fs"]
    count = round(fs * p["seconds"])
    t = np.arange(count) / fs
    audio = p["amp"] * np.sin(2 * np.pi * p["freq"] * t)
    ramp_n = min(round(fs * 0.008), count // 4)
    ramp = 0.5 - 0.5 * np.cos(np.linspace(0, np.pi, ramp_n))
    audio[:ramp_n] *= ramp
    audio[-ramp_n:] *= ramp[::-1]
    p["out"].parent.mkdir(parents=True, exist_ok=True)
    tx.write_wav(p["out"], audio, fs)
    print(f"  Saved {p['out']}  ({len(audio) / fs:.2f} s, peak {np.max(np.abs(audio)):.3f})")
    if p["play"]:
        play_wav(p["out"])


def action_tone() -> None:
    defaults = {"freq": SETTINGS["f0"], "seconds": 3.0, "amp": SETTINGS["amp"], "fs": SETTINGS["fs"],
                "out": default_out("tone"), "play": True}
    LAST["tone"] = run_with_approval("Play a test tone", defaults, collect_tone, rows_tone, exec_tone)


# ----------------------------------------------------------------- 3) decode a WAV
def collect_decode(p: dict) -> dict:
    p = dict(p)
    print("\n  Reads one frame from a WAV file and checks its CRC. Nothing is changed or played.")
    p["wav"] = pick_wav(p.get("wav"))
    p["start_s"] = ask("Seconds into the file where the frame starts (messages made here: 0.5)",
                       p["start_s"], num(float, 0, 100000))
    p["search"] = ask_yn("Search around that time for the sync word (recommended)", p["search"])
    p["key_hex"] = ask("AES key, 32 hex digits (only used for AES frames)", p["key_hex"], parse_key)
    return p


def rows_decode(p: dict) -> list[tuple[str, str]]:
    return [("Action", "decode one frame offline and check its CRC"),
            ("File", str(p["wav"])),
            ("Frame start", f"{p['start_s']:g} s" + (" (search +/-160 ms for the sync word)" if p["search"] else " (exact)")),
            ("Key", "public DEMO key" if p["key_hex"] == DEMO_KEY_HEX else "custom key"),
            ("Side effects", "none (read-only)"),
            ("Same as CLI", f"python aegis_tools.py decode-wav \"{p['wav']}\" --start-s {p['start_s']:g}"
                            + (" --search-start" if p["search"] else ""))]


def exec_decode(p: dict) -> None:
    tools.command_decode(argparse.Namespace(wav=p["wav"], start_s=p["start_s"],
                                            search_start=p["search"], search_ms=160.0, key=p["key_hex"]))


def action_decode() -> None:
    defaults = {"wav": None, "start_s": 0.5, "search": True, "key_hex": SETTINGS["key_hex"]}
    LAST["decode"] = run_with_approval("Decode a WAV file", defaults, collect_decode, rows_decode, exec_decode)


# ----------------------------------------------------------------- 4) inspect a WAV
def collect_inspect(p: dict) -> dict:
    p = dict(p)
    print("\n  Lists the carrier frequency found in each 60 ms symbol. Nothing is changed or played.")
    p["wav"] = pick_wav(p.get("wav"))
    p["start_s"] = ask("Seconds into the file where the first symbol starts", p["start_s"], num(float, 0, 100000))
    max_symbols = (12 + tx.MAX_PAYLOAD) * 8
    p["count"] = ask(f"How many symbols to list (1-{max_symbols}; 48 = sync word + header)", p["count"], num(int, 1, max_symbols))
    return p


def rows_inspect(p: dict) -> list[tuple[str, str]]:
    return [("Action", "list the carrier frequency of each symbol"),
            ("File", str(p["wav"])),
            ("Symbols", f"{p['count']} starting at {p['start_s']:g} s"),
            ("Side effects", "none (read-only)"),
            ("Same as CLI", f"python aegis_tools.py inspect \"{p['wav']}\" --start-s {p['start_s']:g} --count {p['count']}")]


def exec_inspect(p: dict) -> None:
    tools.command_inspect(argparse.Namespace(wav=p["wav"], start_s=p["start_s"], count=p["count"]))


def action_inspect() -> None:
    defaults = {"wav": None, "start_s": 0.5, "count": 48}
    LAST["inspect"] = run_with_approval("Inspect a WAV file", defaults, collect_inspect, rows_inspect, exec_inspect)


# ----------------------------------------------------------------- 5) self-test
def action_selftest() -> None:
    run_with_approval(
        "Self-test", {}, lambda p: p,
        lambda p: [("Action", "run the built-in checks from aegis_tools.py"),
                   ("Checks", "framing, AES-CTR, CRC rejection, generated audio -> offline decode"),
                   ("Side effects", "none (no files, no sound, no nonces used)"),
                   ("Same as CLI", "python aegis_tools.py selftest")],
        lambda p: tools.command_selftest())


# ----------------------------------------------------------------- 6) settings
def settings_menu() -> None:
    while True:
        key_label = "public DEMO key" if SETTINGS["key_hex"] == DEMO_KEY_HEX else "custom key"
        print("\n  Settings (they become the defaults in the other menus)")
        print(f"    1) Output folder      : {SETTINGS['out_dir']}")
        print(f"    2) Volume / amplitude : {SETTINGS['amp']:g}")
        print(f"    3) Sample rate        : {SETTINGS['fs']}")
        print(f"    4) Signal type        : {SETTINGS['signal']}")
        print(f"    5) Fixed tones        : {SETTINGS['f0']:g} / {SETTINGS['f1']:g} Hz")
        print(f"    6) AES key            : {key_label}")
        print("    0) Back")
        choice = _read("  Choose: ")
        try:
            if choice == "1":
                SETTINGS["out_dir"] = Path(ask("Output folder", SETTINGS["out_dir"], lambda s: Path(s.strip('"')).expanduser(), show=str))
            elif choice == "2":
                SETTINGS["amp"] = ask("Volume / amplitude (above 0, up to 0.8)", SETTINGS["amp"], num(float, 0, 0.8, lo_open=True))
            elif choice == "3":
                SETTINGS["fs"] = ask("Sample rate (44100 or 48000)", SETTINGS["fs"], parse_fs)
            elif choice == "4":
                SETTINGS["signal"] = ask("Signal: fixed, fan, or random", SETTINGS["signal"], parse_signal)
            elif choice == "5":
                f0 = ask("Tone for bit 0 in Hz (1000-4000)", SETTINGS["f0"], num(float, 1000, 4000))
                f1 = ask("Tone for bit 1 in Hz (1000-4000)", SETTINGS["f1"], num(float, 1000, 4000))
                if abs(f1 - f0) < 300:
                    print("    ! keep the two tones at least 300 Hz apart - not changed")
                else:
                    SETTINGS["f0"], SETTINGS["f1"] = f0, f1
            elif choice == "6":
                print("  The firmware has its own copy of the key (aes_demo_key in aegis_acoustic.c);")
                print("  both sides must use the same key.")
                SETTINGS["key_hex"] = ask("AES key, 32 hex digits", SETTINGS["key_hex"], parse_key)
            elif choice in ("0", ""):
                return
            else:
                print("    ! choose 0-6")
        except Cancel:
            print("  (unchanged)")


# ----------------------------------------------------------------- 7) help
def show_help() -> None:
    print(f"""
  What the signal types are
    fixed   Two steady tones, one per bit value (default 2000 Hz = 0, 3000 Hz = 1),
            60 ms per bit. These exact frequencies must match main.c.
    fan     Synthetic airflow + mechanical hum, with smooth data overtones.
            Compatible with main.c; perceived masking needs a listening test.
    random  Every bit gets its own random carrier inside a band (0: 1100-2200 Hz,
            1: 2800-3900 Hz) plus fan-like noise, so it does not sound like beeps.
            This is for offline experiments; main.c requires fixed tones.

  Typical test with the board
    1. Flash the firmware, open Serial Bluetooth Terminal, connect to HC-05.
    2. Menu 1: choose fixed first, then fan for the fan-style demonstration.
    3. OLED: TONE HEARD -> LOCKED -> RECEIVED, OK counter +1, phone shows
       "RX: <text>" (plain) or "AES: <text>" (encrypted).

  Notes
    - WAV files go to the output folder (default ./{SETTINGS['out_dir']}).
    - AES mode takes a fresh nonce per frame from .nonce_state.sqlite3 next to
      ultrasonic_tx.py. Do not delete that file while reusing a key.
    - Messages are limited to {tx.MAX_PAYLOAD} UTF-8 bytes. The OLED scrolls and is upper-case only;
      the phone shows the text exactly as sent.
    - /back or Ctrl+C cancels the current action.""")


# ----------------------------------------------------------------- main loop
MENU = [
    ("1", "Send a message (make a WAV, optionally play it)", action_message),
    ("8", "FAN DEMO - encode an encrypted message in fan sound", action_fan_demo),
    ("2", "Play a test tone", action_tone),
    ("3", "Decode a WAV file offline (CRC-checked)", action_decode),
    ("4", "Inspect the carrier frequencies in a WAV file", action_inspect),
    ("5", "Run the self-test", action_selftest),
    ("6", "Settings", settings_menu),
    ("7", "Help / what works with the firmware", show_help),
    ("0", "Quit", None),
]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Enter a message to save encrypted fan audio")
    options = parser.add_mutually_exclusive_group()
    options.add_argument("--fan-demo", action="store_true", help="create encrypted fan audio (the default)")
    options.add_argument("--advanced", action="store_true", help="open optional settings and diagnostic tools")
    args = parser.parse_args(argv)
    try:
        sys.stdout.reconfigure(errors="replace")
    except (AttributeError, ValueError):
        pass
    if not args.advanced:
        return tx.prompt_fan_message()
    print("=" * 72)
    print("  AegisAcoustic V3 - FAN DEMO available as option 8")
    print("=" * 72)
    print(f"  Files loaded from: {Path(__file__).resolve().parent}")
    print("  Nothing runs until you approve a review screen.  /back or Ctrl+C cancels.")
    while True:
        print(f"\n  Defaults: {SETTINGS['signal']} signal, amplitude {SETTINGS['amp']:g}, "
              f"output folder {SETTINGS['out_dir']}")
        for key, label, _ in MENU:
            print(f"  {key}) {label}")
        try:
            choice = _read("\nChoose an option: ")
        except (Cancel, KeyboardInterrupt):
            print("\nBye.")
            return 0
        entry = next((m for m in MENU if m[0] == choice), None)
        if entry is None:
            print("  ! choose one of the numbers above")
            continue
        if entry[2] is None:
            print("Bye.")
            return 0
        try:
            entry[2]()
        except Cancel:
            print("  Cancelled.")
        except KeyboardInterrupt:
            print("\n  Cancelled.")


if __name__ == "__main__":
    raise SystemExit(main())
