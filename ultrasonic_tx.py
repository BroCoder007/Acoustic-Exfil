"""AegisAcoustic transmitter: fixed tones for the STM32, optional random carriers.

This file is self-contained: protocol, CRC, AES-CTR framing, WAV generation,
and playback are all here. No separate protocol.py is needed.

Examples:
  python ultrasonic_tx.py  # enter message, save encrypted fan sound
  python ultrasonic_tx.py message --text AEGIS-42 --mode 0 --play
  python ultrasonic_tx.py message --text AEGIS-42 --mode 1 --play
  python ultrasonic_tx.py message --text TEST --seed 42 --out message.wav
  python ultrasonic_tx.py tone --freq 2500 --seconds 3 --play

Profiles: audible (default) uses 2/3 kHz; fan-demo mixes the same data tones
with synthetic airflow and fan hum. Ultrasonic uses 18/19 kHz and requires
USE_ULTRASONIC=1 in main.c. Randomized is for the offline decoder only.
"""
from __future__ import annotations

import argparse
from contextlib import closing
from datetime import datetime
import hashlib
from pathlib import Path
import secrets
import sqlite3
import sys
import wave

import numpy as np
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

FS_DEFAULT = 48000
BIT_MS = 60
PREAMBLE_SYNC = bytes.fromhex("aaaad391")
DEMO_KEY = bytes.fromhex("00112233445566778899aabbccddeeff")
# Fixed profiles match main.c; randomized is for offline experiments.
PROFILES = ("randomized", "audible", "ultrasonic", "fan-demo")
VERSION = "AegisAcoustic V3 (fan-demo enabled)"
DEFAULT_FAN_DATA_LEVEL = 0.85
MAX_PAYLOAD = 255  # one-byte wire length; must match DM_MAX_PAYLOAD in main.c
FREQ_MIN = 1000.0
FREQ_MAX = 4000.0
BIT_BANDS = {0: (1100.0, 2200.0), 1: (2800.0, 3900.0)}


def crc16(data: bytes) -> int:
    """CRC-16/CCITT-FALSE compatible with the original STM32 decoder."""
    crc = 0xFFFF
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            crc = ((crc << 1) ^ (0x1021 if crc & 0x8000 else 0)) & 0xFFFF
    return crc


def crypt(data: bytes, key: bytes, nonce: int) -> bytes:
    """AES-128-CTR using nonce(4-byte BE) || 12 zero bytes as initial counter."""
    if len(key) != 16:
        raise ValueError("AES-CTR key must be exactly 16 bytes")
    if not 0 <= nonce <= 0xFFFFFFFF:
        raise ValueError("nonce must be a 32-bit unsigned integer")
    initial_counter = nonce.to_bytes(4, "big") + bytes(12)
    cipher = Cipher(algorithms.AES(key), modes.CTR(initial_counter))
    worker = cipher.encryptor()
    return worker.update(data) + worker.finalize()


def allocate_nonce(key: bytes, path: Path | None = None) -> int:
    """Persistently allocate a unique nonce for a given key on this computer.

    Do not delete the database while reusing the same AES key. Resetting this
    database can cause nonce reuse, which is unsafe for CTR mode.
    """
    if len(key) != 16:
        raise ValueError("AES-CTR key must be exactly 16 bytes")
    path = path or Path(__file__).with_name(".nonce_state.sqlite3")
    fingerprint = hashlib.sha256(key).hexdigest()
    with closing(sqlite3.connect(path, timeout=15)) as db:
        with db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS counters (key TEXT PRIMARY KEY, next INTEGER)"
            )
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT next FROM counters WHERE key=?", (fingerprint,)
            ).fetchone()
            nonce = row[0] if row else secrets.randbelow(0x80000000)
            if nonce > 0xFFFFFFFF:
                raise ValueError("nonce exhausted; choose a new key")
            db.execute(
                "INSERT OR REPLACE INTO counters (key, next) VALUES (?, ?)",
                (fingerprint, nonce + 1),
            )
    return nonce


def build_frame(payload: bytes, mode: int = 1, nonce: int = 0,
                key: bytes = DEMO_KEY) -> bytes:
    """Wire frame = preamble || MODE LEN [NONCE] PAYLOAD CRC16."""
    if not 1 <= len(payload) <= MAX_PAYLOAD:
        raise ValueError(f"payload must be 1..{MAX_PAYLOAD} UTF-8 bytes")
    if mode not in (0, 1):
        raise ValueError("mode must be 0 (plain) or 1 (AES-CTR)")
    body = bytes((mode, len(payload)))
    if mode:
        body += nonce.to_bytes(4, "big") + crypt(payload, key, nonce)
    else:
        body += payload
    body += crc16(body).to_bytes(2, "big")
    return PREAMBLE_SYNC + body


def to_bits(frame: bytes) -> list[int]:
    """MSB-first bit stream."""
    return [(byte >> bit) & 1 for byte in frame for bit in range(7, -1, -1)]


def parse_body(body: bytes, key: bytes = DEMO_KEY) -> tuple[bytes, int, int | None]:
    """Validate/decrypt a frame body excluding the 4-byte sync preamble."""
    if len(body) < 5:
        raise ValueError("frame body too short")
    mode, length = body[0], body[1]
    if mode not in (0, 1) or not 1 <= length <= MAX_PAYLOAD:
        raise ValueError("invalid mode or length")
    offset = 6 if mode else 2
    if len(body) != offset + length + 2:
        raise ValueError("invalid frame length")
    expected = int.from_bytes(body[-2:], "big")
    if crc16(body[:-2]) != expected:
        raise ValueError("CRC error")
    nonce = int.from_bytes(body[2:6], "big") if mode else None
    payload = body[offset:-2]
    if mode:
        payload = crypt(payload, key, nonce)
    return payload, mode, nonce


def _smoothstep(x: np.ndarray) -> np.ndarray:
    x = np.clip(x, 0.0, 1.0)
    return x * x * (3.0 - 2.0 * x)


def _render_fan_demo(bits: np.ndarray, fs: int, amplitude: float, lead: float,
                     rng: np.random.Generator, data_level: float) -> np.ndarray:
    """Synthetic fan plus the existing 2/3 kHz protocol for a receiver demo.

    data_level is approximately carrier RMS / background RMS, before the final
    shared peak normalization. Zero produces a background-only control. This
    sound is not a recording and audibility must be assessed by listening.
    """
    symbol_n = round(fs * BIT_MS / 1000)
    lead_n, tail_n = round(lead * fs), round(0.5 * fs)
    data_n = len(bits) * symbol_n
    count = lead_n + data_n + tail_n
    t = np.arange(count, dtype=np.float64) / fs

    # Broad airflow with more low-frequency energy and gradual high-end rolloff.
    noise = rng.normal(0, 1, count)
    spectrum = np.fft.rfft(noise)
    hz = np.fft.rfftfreq(count, 1 / fs)
    shape = np.power(1 + np.square(hz / 700.0), -0.30)
    shape *= 1 - np.exp(-np.square(hz / 55.0))
    shape *= np.exp(-np.power(hz / 9500.0, 4))
    noise = np.fft.irfft(spectrum * shape, n=count)
    noise /= max(float(np.sqrt(np.mean(noise * noise))), 1e-12)
    # Gentle mechanical hum / blade harmonics, without data-driven pauses.
    blade_phase = 2 * np.pi * 117 * t + .09 * np.sin(2 * np.pi * .43 * t)
    background = (.88 * noise + .31 * np.sin(blade_phase)
                  + .13 * np.sin(2 * blade_phase + .4)
                  + .07 * np.sin(3 * blade_phase + .9))
    background *= 1 + .035 * np.sin(2 * np.pi * .7 * t) + .018 * np.sin(2 * np.pi * 23 * t)
    background /= max(float(np.sqrt(np.mean(background * background))), 1e-12)

    balance = np.repeat(np.where(bits, 1.0, -1.0), symbol_n)
    # Keep both mechanical overtones running. Smoothly change their balance
    # instead of switching one whistle completely on and the other off.
    half = max(1, round(.004 * fs))
    for i in range(1, len(bits)):
        if bits[i] == bits[i - 1]:
            continue
        mid = i * symbol_n
        blend = _smoothstep(np.linspace(0, 1, 2 * half))
        before, after = balance[mid - half - 1], balance[mid + half]
        balance[mid - half:mid + half] = before + (after - before) * blend
    data_t = np.arange(data_n, dtype=np.float64) / fs
    # A quiet residual overtone avoids complete on/off switching. Give the data
    # tone a stronger lead than V2 so unequal speaker/mic frequency response does
    # not reverse the detector's energy comparison.
    carrier = ((.8 - .65 * balance) * np.sin(2 * np.pi * 2000 * data_t)
               + (.8 + .65 * balance) * np.sin(2 * np.pi * 3000 * data_t + .37)) / np.sqrt(.8 ** 2 + .65 ** 2)
    ramp_n = min(round(.008 * fs), data_n // 2)
    ramp = _smoothstep(np.linspace(0, 1, ramp_n))
    carrier[:ramp_n] *= ramp
    carrier[-ramp_n:] *= ramp[::-1]
    audio = background.copy()
    audio[lead_n:lead_n + data_n] += data_level * carrier
    fade_n = min(round(.06 * fs), count // 2)
    fade = _smoothstep(np.linspace(0, 1, fade_n))
    audio[:fade_n] *= fade
    audio[-fade_n:] *= fade[::-1]
    audio *= amplitude / max(float(np.max(np.abs(audio))), 1e-12)
    return audio


def render(frame: bytes, fs: int = FS_DEFAULT, profile: str = "audible",
           amplitude: float = 0.5, lead: float = 0.5, seed: int | None = None,
           fan_texture: bool = True, data_level: float = DEFAULT_FAN_DATA_LEVEL) -> np.ndarray:
    """Render fixed firmware-compatible tones, or optional randomized carriers.

    In the randomized profile, frequencies vary inside disjoint bit bands, rather than
    uniformly across 1–4 kHz. The bands leave a wide guard around 2.5 kHz. That band mapping is what lets a simple embedded
    decoder infer each bit without knowing the randomly chosen exact frequency.
    The oscillator phase is continuous; soft frequency glides and low-level
    harmonics/colored noise make it less like a sequence of isolated beeps.
    """
    if fs not in (44100, 48000):
        raise ValueError("sample rate must be 44100 or 48000 Hz")
    if profile not in PROFILES:
        raise ValueError(f"profile must be one of: {', '.join(PROFILES)}")
    if not 0 < amplitude <= 0.8:
        raise ValueError("amplitude must be >0 and <=0.8")
    if lead < 0:
        raise ValueError("lead must be >=0")
    if not 0 <= data_level <= 1:
        raise ValueError("data-level must be 0..1 (0 = fan-only control)")

    rng = np.random.default_rng(seed)
    bits = np.asarray(to_bits(frame), dtype=np.uint8)
    samples_per_symbol = round(fs * BIT_MS / 1000)
    if profile == "fan-demo":
        return _render_fan_demo(bits, fs, amplitude, lead, rng, data_level)
    if profile == "randomized":
        symbols_hz = np.array([rng.uniform(*BIT_BANDS[int(b)]) for b in bits])
    else:
        f0, f1 = (2000.0, 3000.0) if profile == "audible" else (18000.0, 19000.0)
        symbols_hz = np.where(bits == 0, f0, f1).astype(np.float64)
        audio = np.sin(2 * np.pi * np.cumsum(np.repeat(symbols_hz, samples_per_symbol)) / fs)
        ramp_n = min(round(0.003 * fs), len(audio) // 2)
        ramp = 0.5 - 0.5 * np.cos(np.linspace(0.0, np.pi, ramp_n))
        audio[:ramp_n] *= ramp
        audio[-ramp_n:] *= ramp[::-1]
        return np.concatenate((np.zeros(round(lead * fs)), amplitude * audio,
                               np.zeros(round(0.5 * fs))))
    f = np.repeat(symbols_hz, samples_per_symbol).astype(np.float64)

    # Smooth each transition over 5 ms while preserving the symbol timeline.
    glide_n = max(8, round(0.005 * fs))
    half = glide_n // 2
    for symbol in range(1, len(symbols_hz)):
        boundary = symbol * samples_per_symbol
        lo = max(0, boundary - half)
        hi = min(len(f), boundary + (glide_n - half))
        if hi > lo:
            x = np.linspace(0.0, 1.0, hi - lo)
            f[lo:hi] = symbols_hz[symbol - 1] + (
                symbols_hz[symbol] - symbols_hz[symbol - 1]
            ) * _smoothstep(x)

    t = np.arange(len(f), dtype=np.float64) / fs
    instantaneous_hz = np.clip(
        f + 9.0 * np.sin(2 * np.pi * 2.1 * t)
          + 2.5 * np.sin(2 * np.pi * 6.7 * t + 0.4),
        FREQ_MIN, FREQ_MAX,
    )
    phase = 2 * np.pi * np.cumsum(instantaneous_hz) / fs
    # Low harmonic creates a more machine-like timbre without adding a second carrier.
    audio = np.sin(phase) + 0.07 * np.sin(2 * phase + 0.25)
    if fan_texture:
        # Low-level colored noise and amplitude flutter add an airflow-like texture.
        noise = rng.normal(0.0, 1.0, len(audio))
        alpha = min(0.22, 2 * np.pi * 1100.0 / fs)
        for i in range(1, len(noise)):
            noise[i] = noise[i - 1] + alpha * (noise[i] - noise[i - 1])
        noise /= max(float(np.max(np.abs(noise))), 1e-12)
        flutter = 0.97 + 0.025 * np.sin(2 * np.pi * 23.0 * t)
        audio = audio * flutter + 0.018 * noise

    ramp_n = min(round(0.008 * fs), max(1, len(audio) // 4))
    ramp = 0.5 - 0.5 * np.cos(np.linspace(0.0, np.pi, ramp_n))
    audio[:ramp_n] *= ramp
    audio[-ramp_n:] *= ramp[::-1]
    peak = float(np.max(np.abs(audio)))
    if peak:
        audio *= amplitude / peak

    return np.concatenate((np.zeros(round(lead * fs)), audio,
                           np.zeros(round(0.5 * fs))))


def write_wav(path: str | Path, audio: np.ndarray, fs: int) -> None:
    if not len(audio) or float(np.max(np.abs(audio))) > 1.0:
        raise ValueError("empty audio or WAV amplitude would clip")
    with wave.open(str(path), "wb") as out:
        out.setparams((1, 2, fs, 0, "NONE", "not compressed"))
        out.writeframes(np.rint(audio * 32767).astype("<i2").tobytes())


def play(path: str | Path) -> None:
    print("Playing WAV; keep playback volume moderate.")
    try:
        import sounddevice as sd
    except ImportError:
        import winsound
        winsound.PlaySound(str(Path(path).resolve()), winsound.SND_FILENAME)
    else:
        with wave.open(str(path), "rb") as src:
            samples = np.frombuffer(src.readframes(src.getnframes()), "<i2").astype(float) / 32768.0
            sd.play(samples, src.getframerate())
            sd.wait()


def create_encrypted_fan(text: str, out_dir: Path | None = None) -> Path:
    """Save one AES fan-sound WAV using the receiver-compatible defaults."""
    payload = text.encode("utf-8")
    if not 1 <= len(payload) <= MAX_PAYLOAD:
        raise ValueError(f"Message must be 1..{MAX_PAYLOAD} UTF-8 bytes.")
    folder = Path(out_dir) if out_dir is not None else Path(__file__).resolve().parent / "aegis_out"
    folder.mkdir(parents=True, exist_ok=True)
    nonce = allocate_nonce(DEMO_KEY)
    frame = build_frame(payload, mode=1, nonce=nonce, key=DEMO_KEY)
    audio = render(frame, profile="fan-demo", data_level=DEFAULT_FAN_DATA_LEVEL)
    path = folder / f"fan_message_{datetime.now():%Y%m%d_%H%M%S_%f}.wav"
    write_wav(path, audio, FS_DEFAULT)
    return path.resolve()


def prompt_fan_message() -> int:
    """One message prompt; save the sound without setup or approval questions."""
    try:
        text = input("Message: ")
        path = create_encrypted_fan(text)
    except (EOFError, KeyboardInterrupt):
        print("\nCancelled.")
        return 0
    except (ValueError, OSError) as exc:
        print(f"Could not create sound: {exc}", file=sys.stderr)
        return 1
    print(f"Saved: {path}")
    return 0


def main(argv: list[str] | None = None) -> int | None:
    argv = sys.argv[1:] if argv is None else argv
    if not argv:
        return prompt_fan_message()
    p = argparse.ArgumentParser(description="AegisAcoustic transmitter (audible profile matches main.c)")
    p.add_argument("--version", action="version", version=f"{VERSION}\nSource: {Path(__file__).resolve()}")
    sub = p.add_subparsers(dest="command", required=True)
    m = sub.add_parser("message", help="frame and render a message")
    m.add_argument("--text", default="AEGIS-42")
    m.add_argument("--mode", choices=("0", "1"), default="1", help="0=plain, 1=AES-CTR")
    m.add_argument("--key", default=DEMO_KEY.hex(), help="32 hex chars for AES-128")
    m.add_argument("--repeat", type=int, default=1)
    m.add_argument("--gap-s", type=float, default=2.0)
    m.add_argument("--profile", choices=PROFILES, default="audible",
                   help="audible=2/3 kHz; fan-demo=fan sound + 2/3 kHz data; ultrasonic=18/19 kHz; randomized=offline only")
    m.add_argument("--data-level", type=float, default=DEFAULT_FAN_DATA_LEVEL,
                   help=f"fan-demo carrier/background RMS ratio, 0..1 (0 = fan-only control); default {DEFAULT_FAN_DATA_LEVEL}")
    m.add_argument("--seed", type=int, default=None, help="deterministic test seed")
    m.add_argument("--no-fan-texture", action="store_true")
    tone = sub.add_parser("tone", help="generate a fixed test tone")
    tone.add_argument("--freq", type=float, default=2500.0)
    tone.add_argument("--seconds", type=float, default=3.0)
    for cmd in (m, tone):
        cmd.add_argument("--out", default="message.wav" if cmd is m else "tone.wav")
        cmd.add_argument("--fs", type=int, choices=(44100, 48000), default=48000)
        cmd.add_argument("--amp", type=float, default=0.5)
        cmd.add_argument("--play", action="store_true")
    args = p.parse_args(argv)

    try:
        if not 0 < args.amp <= 0.8:
            raise ValueError("amp must be >0 and <=0.8")
        if args.command == "message":
            if not 1 <= args.repeat <= 20:
                raise ValueError("repeat must be 1..20")
            if not 0.5 <= args.gap_s <= 30:
                raise ValueError("gap-s must be 0.5..30")
            key = bytes.fromhex(args.key)
            if len(key) != 16:
                raise ValueError("key needs exactly 32 hex digits")
            payload = args.text.encode("utf-8")
            if not 1 <= len(payload) <= MAX_PAYLOAD:
                raise ValueError(f"text must occupy 1..{MAX_PAYLOAD} UTF-8 bytes")
            segments = []
            for i in range(args.repeat):
                nonce = allocate_nonce(key) if args.mode == "1" else 0
                frame = build_frame(payload, int(args.mode), nonce, key)
                # Separate non-deterministic carrier sequences by repeat even with a supplied test seed.
                seed = None if args.seed is None else args.seed + i
                segments.append(render(frame, args.fs, args.profile, args.amp,
                                       lead=0.5 if i == 0 else 0.0,
                                       seed=seed,
                                       fan_texture=not args.no_fan_texture,
                                       data_level=args.data_level))
                if i != args.repeat - 1:
                    segments.append(np.zeros(round(args.gap_s * args.fs)))
                print(f"frame={len(frame)} bytes, {len(frame)*8*BIT_MS/1000:.2f}s data, mode={args.mode}, nonce={nonce}")
            # render() adds a trailing 0.5 s pad per repeat; concatenate as rendered.
            audio = np.concatenate(segments)
        else:
            if not 1000 <= args.freq <= 4000:
                raise ValueError("tone frequency must be 1000..4000 Hz")
            if not 0.1 <= args.seconds <= 30:
                raise ValueError("tone duration must be 0.1..30 s")
            count = round(args.fs * args.seconds)
            t = np.arange(count) / args.fs
            audio = args.amp * np.sin(2 * np.pi * args.freq * t)
            ramp_n = min(round(args.fs * 0.008), count // 4)
            ramp = 0.5 - 0.5 * np.cos(np.linspace(0, np.pi, ramp_n))
            audio[:ramp_n] *= ramp
            audio[-ramp_n:] *= ramp[::-1]
        write_wav(args.out, audio, args.fs)
        print(f"Saved {args.out}: {len(audio)/args.fs:.2f}s, peak={np.max(np.abs(audio)):.3f}")
        if args.play:
            play(args.out)
    except (ValueError, OSError) as exc:
        p.error(str(exc))


if __name__ == "__main__":
    raise SystemExit(main())
