"""AegisAcoustic companion tools: offline WAV decoder, analyzer and self-test.

This is the second and only other Python source file in the project. The
transmitter/protocol/crypto implementation lives in ultrasonic_tx.py.

Commands:
  python aegis_tools.py selftest
  python aegis_tools.py inspect message.wav --start-s 0.5
  python aegis_tools.py decode-wav message.wav --start-s 0.5
  python aegis_tools.py decode-wav message.wav --search-start

WAV decoding is an offline diagnostic for audible fixed tones and randomized
carriers. main.c uses fixed tones; this offline decoder does not support ultrasound.
"""
from __future__ import annotations

import argparse
import math
from pathlib import Path
import wave

import numpy as np

import ultrasonic_tx as tx


SYNC_INT = int.from_bytes(tx.PREAMBLE_SYNC, "big")
MIN_FREQ = 1000.0
MAX_FREQ = 4000.0
BIT_THRESHOLD_HZ = 2500.0


def read_wav(path: str | Path) -> tuple[np.ndarray, int]:
    """Read mono 16-bit PCM WAV and return float samples in [-1, 1)."""
    with wave.open(str(path), "rb") as wav:
        channels = wav.getnchannels()
        width = wav.getsampwidth()
        rate = wav.getframerate()
        frames = wav.getnframes()
        if channels != 1:
            raise ValueError("WAV must be mono")
        if width != 2:
            raise ValueError("WAV must be 16-bit PCM")
        if wav.getcomptype() != "NONE":
            raise ValueError("WAV must be uncompressed PCM")
        data = wav.readframes(frames)
    samples = np.frombuffer(data, dtype="<i2").astype(np.float64) / 32768.0
    return samples, rate


def estimate_frequency(samples: np.ndarray, fs: int) -> float:
    """Estimate the dominant 1–4 kHz frequency from one symbol's sample slice.

    A Hann-windowed FFT is used for a robust offline estimate. It is not the
    algorithm used in the bare-metal C receiver, but it uses the same bit-band
    threshold and is useful for checking generated WAVs and captured recordings.
    """
    if len(samples) < 16:
        raise ValueError("Not enough audio samples to estimate frequency")

    x = np.asarray(samples, dtype=np.float64)
    x = x - np.mean(x)
    x *= np.hanning(len(x))

    nfft = 1 << (len(x) - 1).bit_length()
    spectrum = np.abs(np.fft.rfft(x, n=nfft))
    hz = np.fft.rfftfreq(nfft, d=1.0 / fs)

    band = (hz >= MIN_FREQ) & (hz <= MAX_FREQ)
    if not np.any(band):
        raise ValueError("No FFT bins in the 1–4 kHz band")

    valid_indices = np.flatnonzero(band)
    peak_idx = int(valid_indices[np.argmax(spectrum[band])])

    # Parabolic interpolation improves the estimate between FFT bins.
    if 1 <= peak_idx < len(spectrum) - 1:
        a = math.log(float(spectrum[peak_idx - 1]) + 1e-15)
        b = math.log(float(spectrum[peak_idx]) + 1e-15)
        c = math.log(float(spectrum[peak_idx + 1]) + 1e-15)
        denom = a - 2.0 * b + c
        if abs(denom) > 1e-12:
            delta = 0.5 * (a - c) / denom
            delta = max(-1.0, min(1.0, delta))
            return float((peak_idx + delta) * fs / nfft)

    return float(hz[peak_idx])


def symbol_frequency(
    samples: np.ndarray,
    fs: int,
    symbol_index: int,
    start_sample: int,
    bit_ms: int = tx.BIT_MS,
) -> float:
    """Estimate the frequency in the stable center of a symbol."""
    symbol_samples = round(fs * bit_ms / 1000.0)
    center = start_sample + symbol_index * symbol_samples + symbol_samples // 2
    # Analyze the center 36 ms, avoiding the 5 ms transition glide on edges.
    width = min(round(fs * 0.036), symbol_samples - 2)
    left = center - width // 2
    right = left + width
    if left < 0 or right > len(samples):
        raise ValueError(f"Audio ended before symbol {symbol_index}")
    return estimate_frequency(samples[left:right], fs)


def classify_frequency(freq_hz: float) -> int:
    """Classify one carrier using the sender's 2.5 kHz decision boundary."""
    if not MIN_FREQ <= freq_hz <= MAX_FREQ:
        raise ValueError(f"Estimated frequency {freq_hz:.1f} Hz is outside 1–4 kHz")
    if 2250.0 < freq_hz < 2750.0:
        raise ValueError(
            f"Estimated frequency {freq_hz:.1f} Hz falls in the bit-decision guard band"
        )
    return 1 if freq_hz >= BIT_THRESHOLD_HZ else 0


def read_bits(
    samples: np.ndarray,
    fs: int,
    start_sample: int,
    count: int,
) -> tuple[list[int], list[float]]:
    bits: list[int] = []
    freqs: list[float] = []
    for i in range(count):
        freq = symbol_frequency(samples, fs, i, start_sample)
        freqs.append(freq)
        bits.append(classify_frequency(freq))
    return bits, freqs


def bits_to_bytes(bits: list[int]) -> bytes:
    if len(bits) % 8:
        raise ValueError("Bit count must be a multiple of eight")
    out = bytearray()
    for pos in range(0, len(bits), 8):
        value = 0
        for bit in bits[pos:pos + 8]:
            value = (value << 1) | (bit & 1)
        out.append(value)
    return bytes(out)


def parse_frame_from_audio(
    samples: np.ndarray,
    fs: int,
    start_sample: int,
    key: bytes = tx.DEMO_KEY,
) -> tuple[bytes, bytes, int, int | None, list[float]]:
    """Recover and validate one whole frame from a known symbol start.

    Returns (frame, payload, mode, nonce, estimated_frequencies).
    """
    # Sync + mode + len gives enough information to determine total frame size.
    header_bits, header_freqs = read_bits(samples, fs, start_sample, 48)
    header = bits_to_bytes(header_bits)
    if header[:4] != tx.PREAMBLE_SYNC:
        raise ValueError(
            f"Sync mismatch: got {header[:4].hex()}, expected {tx.PREAMBLE_SYNC.hex()}; "
            "check --start-s or use --search-start"
        )

    mode, payload_len = header[4], header[5]
    if mode not in (0, 1):
        raise ValueError(f"Invalid mode field: {mode}")
    if not 1 <= payload_len <= tx.MAX_PAYLOAD:
        raise ValueError(f"Invalid payload length field: {payload_len}")

    frame_len = 4 + 2 + (4 if mode == 1 else 0) + payload_len + 2
    bit_count = frame_len * 8
    all_bits, all_freqs = read_bits(samples, fs, start_sample, bit_count)
    frame = bits_to_bytes(all_bits)

    body = frame[4:]
    payload, parsed_mode, nonce = tx.parse_body(body, key=key)
    if parsed_mode != mode:
        raise ValueError("Internal mode mismatch while parsing frame")

    return frame, payload, mode, nonce, all_freqs


def find_frame_start(
    samples: np.ndarray,
    fs: int,
    approximate_start_s: float,
    search_ms: float = 160.0,
) -> int:
    """Search nearby symbol start offsets for the sync word.

    Candidate starts are tested on a 2 ms grid. This is intended for offline
    troubleshooting, not real-time embedded use.
    """
    if search_ms < 0 or search_ms > 2000:
        raise ValueError("search-ms must be between 0 and 2000")

    symbols = round(fs * tx.BIT_MS / 1000.0)
    approx = round(approximate_start_s * fs)
    radius = round(search_ms * fs / 1000.0)
    step = max(1, round(fs * 0.002))

    # Search the approximate time first, then offsets to either side.
    offsets = [0]
    for i in range(step, radius + 1, step):
        offsets.extend((-i, i))

    last_error: Exception | None = None
    for offset in offsets:
        start = approx + offset
        if start < 0 or start + 48 * symbols > len(samples):
            continue
        try:
            bits, _ = read_bits(samples, fs, start, 32)
            if bits_to_bytes(bits) == tx.PREAMBLE_SYNC:
                return start
        except (ValueError, IndexError) as exc:
            last_error = exc

    message = "Could not find sync preamble near the requested start time"
    if last_error:
        message += f" ({last_error})"
    raise ValueError(message)


def command_selftest() -> None:
    """Test frame protocol, crypto round-trips and end-to-end generated audio."""
    checks = 0
    key = tx.DEMO_KEY

    # Plain protocol round trip.
    plain = b"AEGIS-42"
    frame0 = tx.build_frame(plain, mode=0, nonce=0, key=key)
    decoded0, mode0, nonce0 = tx.parse_body(frame0[4:], key=key)
    assert decoded0 == plain and mode0 == 0 and nonce0 is None
    checks += 1

    # AES-CTR protocol round trip, using an explicitly known unique test nonce.
    encrypted_payload = b"AES TEST 123"
    frame1 = tx.build_frame(encrypted_payload, mode=1, nonce=0x10203040, key=key)
    decoded1, mode1, nonce1 = tx.parse_body(frame1[4:], key=key)
    assert decoded1 == encrypted_payload and mode1 == 1 and nonce1 == 0x10203040
    checks += 1

    # CRC tampering must be rejected.
    bad = bytearray(frame0[4:])
    bad[-1] ^= 0x01
    try:
        tx.parse_body(bytes(bad), key=key)
    except ValueError as exc:
        assert "CRC" in str(exc)
    else:
        raise AssertionError("Modified frame passed CRC validation")
    checks += 1

    # Generate audio with deterministic random carriers and decode it offline.
    payload = b"PY2FILES"
    frame = tx.build_frame(payload, mode=0, key=key)
    audio = tx.render(
        frame,
        fs=48000,
        profile="randomized",
        amplitude=0.5,
        lead=0.0,
        seed=13579,
        fan_texture=True,
    )
    frame_rx, payload_rx, mode_rx, nonce_rx, freqs = parse_frame_from_audio(
        audio, 48000, start_sample=0
    )
    assert frame_rx == frame
    assert payload_rx == payload and mode_rx == 0 and nonce_rx is None
    assert all(1000 <= f <= 4000 for f in freqs)
    checks += 1

    # Assert the frequency sequence actually varies, not a two-tone fixed pair.
    assert len(set(round(f / 50) for f in freqs[32:])) > 2
    checks += 1

    print(f"SELFTEST PASS: {checks} checks")
    print("  plain framing round trip: PASS")
    print("  AES-128-CTR round trip: PASS")
    print("  CRC tampering rejection: PASS")
    print("  generated audio -> offline decode: PASS")
    print("  randomized 1–4 kHz carriers: PASS")
    print("Note: this does not compile or validate the STM32 C firmware or hardware path.")


def command_inspect(args: argparse.Namespace) -> None:
    samples, fs = read_wav(args.wav)
    start_sample = round(args.start_s * fs)
    if args.count < 1 or args.count > (12 + tx.MAX_PAYLOAD) * 8:
        raise ValueError(f"count must be in 1..{(12 + tx.MAX_PAYLOAD) * 8} symbols")

    print(f"File: {args.wav}")
    print(f"Sample rate: {fs} Hz; duration: {len(samples)/fs:.3f} s")
    print(f"Symbol start: {args.start_s:.4f} s; bit duration: {tx.BIT_MS} ms")
    print("index   frequency_Hz   class")

    for index in range(args.count):
        freq = symbol_frequency(samples, fs, index, start_sample)
        if freq < 2250:
            label = "0"
        elif freq > 2750:
            label = "1"
        else:
            label = "GUARD"
        print(f"{index:5d}   {freq:12.1f}   {label}")


def command_decode(args: argparse.Namespace) -> None:
    samples, fs = read_wav(args.wav)
    approximate = args.start_s

    if args.search_start:
        start_sample = find_frame_start(
            samples,
            fs,
            approximate,
            search_ms=args.search_ms,
        )
    else:
        start_sample = round(approximate * fs)

    try:
        key = bytes.fromhex(args.key)
    except ValueError as exc:
        raise ValueError("--key must be hex-encoded") from exc
    if len(key) != 16:
        raise ValueError("--key must contain exactly 32 hex digits")

    frame, payload, mode, nonce, freqs = parse_frame_from_audio(
        samples,
        fs,
        start_sample,
        key=key,
    )

    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError:
        text = repr(payload)

    print(f"File: {args.wav}")
    print(f"Frame start: {start_sample/fs:.6f} s")
    print(f"Frame bytes: {len(frame)}")
    print(f"Mode: {'AES-128-CTR' if mode else 'plain'}")
    print(f"Nonce: {nonce if nonce is not None else 'n/a'}")
    print(f"Payload: {text}")
    print(f"Estimated carrier span: {min(freqs):.1f}..{max(freqs):.1f} Hz")
    print("CRC: PASS")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="AegisAcoustic offline decoder and project diagnostics"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("selftest", help="run protocol and generated-audio tests")

    inspect_parser = sub.add_parser(
        "inspect", help="show estimated carrier per symbol from a WAV"
    )
    inspect_parser.add_argument("wav", type=Path)
    inspect_parser.add_argument("--start-s", type=float, default=0.5)
    inspect_parser.add_argument("--count", type=int, default=48)

    decode_parser = sub.add_parser(
        "decode-wav", help="offline-decode and CRC-check one frame"
    )
    decode_parser.add_argument("wav", type=Path)
    decode_parser.add_argument("--start-s", type=float, default=0.5)
    decode_parser.add_argument(
        "--search-start",
        action="store_true",
        help="search for the sync preamble near --start-s",
    )
    decode_parser.add_argument("--search-ms", type=float, default=160.0)
    decode_parser.add_argument(
        "--key",
        default=tx.DEMO_KEY.hex(),
        help="32 hex digits for decoding AES mode",
    )

    args = parser.parse_args()
    try:
        if args.command == "selftest":
            command_selftest()
        elif args.command == "inspect":
            command_inspect(args)
        elif args.command == "decode-wav":
            command_decode(args)
    except (OSError, ValueError, AssertionError) as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    main()
