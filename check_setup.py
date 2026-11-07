"""Optional, noninteractive check of the PC files and encoder dependencies."""
from importlib import import_module
from importlib.metadata import version
from pathlib import Path
import sys


def main():
    root = Path(__file__).resolve().parent
    passed = sys.version_info >= (3, 11)
    print(f"{'PASS' if passed else 'FAIL'} Python {sys.version.split()[0]} (3.11+ required)")
    for name in ("numpy", "cryptography"):
        try:
            import_module(name)
            print(f"PASS {name} {version(name)}")
        except (ImportError, OSError) as exc:
            passed = False
            print(f"FAIL {name}: {exc}")
    files = ("main.c", "aegis_ml_denoise.c", "aegis_ml_adapter.c",
             "aegis_ml_denoise.h", "aegis_ml_adapter.h", "aegis_ml_model_data.h",
             "ultrasonic_tx.py", "aegis_tools.py", "aegis_menu.py")
    for name in files:
        found = (root / name).is_file()
        passed = passed and found
        print(f"{'PASS' if found else 'FAIL'} {name}")
    print("PC setup ready." if passed else "Install requirements.txt and check missing files; see FIRST_TIME_SETUP.md.")
    print("Board wiring, Keil build and microphone reception need the guide's hardware checks.")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
