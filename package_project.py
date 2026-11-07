"""Create and verify the complete source release; exclude private/runtime data."""
import hashlib
import json
from pathlib import Path
import zipfile

ROOT = Path(__file__).resolve().parent
FILES = (
    "README.md", "FIRST_TIME_SETUP.md", "START_HERE.md", "requirements.txt",
    "check_setup.py", "package_project.py", "main.c", "main.c.txt",
    "aegis_ml_denoise.c", "aegis_ml_adapter.c", "aegis_ml_denoise.h",
    "aegis_ml_adapter.h", "aegis_ml_model_data.h",
    "ultrasonic_tx.py", "aegis_menu.py", "aegis_tools.py",
    "FAN_DEMO.md", "RECEIVER_FIX.md", "fan_check_v3.wav",
    "docs/MODEL_CARD.md", "docs/SOURCES.md", "docs/DATA_MANIFEST.json",
    "docs/ESC50_LICENSE.txt", "docs/VALIDATION.json", "docs/TESTING.md",
    "docs/INTEGRATION_AUDIT.json", "docs/RELEASE_SMOKE_TEST.json",
    "original/main-before-ml.c", "original/main-before-ml.c.txt",
    "original/PRE_ML_SHA256.json", "original/README.md",
    "tests/README.md", "tests/integrated_host.c", "tests/receiver_host.c",
    "tests/validate_integration.py", "tests/test_receiver.py",
    "tests/test_simple_flow.py", "tests/test_fan_acquisition.py",
    "tests/fan_preamble_regression.wav", "tests/smoke_release.py",
)


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def audit():
    baseline = json.loads((ROOT / "original/PRE_ML_SHA256.json").read_text())
    verified = {}
    for name, expected in baseline.items():
        preserved = name
        if name == "main.c":
            preserved = "original/main-before-ml.c"
        elif name == "main.c.txt":
            preserved = "original/main-before-ml.c.txt"
        actual = sha(ROOT / preserved)
        assert actual.upper() == expected, f"Baseline differs: {preserved}"
        verified[name] = dict(preserved_path=preserved, sha256=actual, matches_baseline=True)
    assert (ROOT / "main.c").read_bytes() == (ROOT / "main.c.txt").read_bytes()
    assert sha(ROOT / "main.c").upper() != baseline["main.c"]
    validation = json.loads((ROOT / "docs/VALIDATION.json").read_text())
    for name, expected in validation["source_sha256"].items():
        assert sha(ROOT / name) == expected, f"Source changed since integration validation: {name}"
    # Research directory is deliberately absent from the runtime ZIP.
    training_header = ROOT / "ml_experiment/aegis_ml_model_data.h"
    if training_header.exists():
        # Export copies can use different Windows/POSIX newline conventions.
        assert training_header.read_text() == (ROOT / "aegis_ml_model_data.h").read_text()
    result = dict(
        root_main_is_integrated=True, root_text_mirror_identical=True,
        pre_ml_baseline_preserved=verified,
        trained_model_header_sha256=sha(ROOT / "aegis_ml_model_data.h"),
        trained_model_source_normalized_sha256=hashlib.sha256((ROOT / "aegis_ml_model_data.h").read_text().encode()).hexdigest(),
        validated_firmware_sha256=validation["source_sha256"],
        host_integration_checks=len(validation["checks"]),
        target_build_and_hardware_verified=False,
        excluded_from_release=[".nonce_state.sqlite3", ".venv", "aegis_out",
                               "raw training data", "host DLL/object files", "legacy staging receivers"],
    )
    (ROOT / "docs/INTEGRATION_AUDIT.json").write_text(json.dumps(result, indent=2)+"\n")


def main():
    audit()
    hashes = {name: sha(ROOT / name) for name in FILES}
    (ROOT / "PROJECT_SHA256.json").write_text(json.dumps(hashes, indent=2)+"\n")
    archive = ROOT / "AegisAcoustic_Complete.zip"
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as z:
        for name in (*FILES, "PROJECT_SHA256.json"):
            z.write(ROOT / name, "AegisAcoustic/"+name)
    with zipfile.ZipFile(archive) as z:
        assert z.testzip() is None
        assert len(z.namelist()) == len(FILES)+1
        for name, expected in hashes.items():
            assert hashlib.sha256(z.read("AegisAcoustic/"+name)).hexdigest() == expected
    print(f"PASS complete source archive: {archive} ({archive.stat().st_size:,} bytes; {len(FILES)+1} files)")


if __name__ == "__main__":
    main()
