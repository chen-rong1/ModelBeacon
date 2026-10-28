import platform
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(platform.system() == "Darwin" and shutil.which("xcrun"), "Native snapshot checks require the macOS Swift compiler")
class ModelSnapshotTests(unittest.TestCase):
    def test_numeric_state_history_and_send_eligibility(self):
        with tempfile.TemporaryDirectory(prefix="beacon-snapshot-tests-") as temporary:
            directory = Path(temporary)
            sdk = subprocess.check_output(["xcrun", "--show-sdk-path"], text=True).strip()
            command = ["xcrun", "swiftc", "-D", "MODEL_BEACON_MODEL_TESTS", "-sdk", sdk,
                       "-module-cache-path", str(directory / "module-cache"),
                       str(ROOT / "macos" / "FloatingGeometry.swift"),
                       str(ROOT / "macos" / "PassiveModelMonitor.swift"),
                       str(ROOT / "tests" / "swift" / "ModelSnapshotChecks.swift"), "-o", str(directory / "checks")]
            compilation = subprocess.run(command, text=True, capture_output=True, timeout=90)
            self.assertEqual(compilation.returncode, 0, compilation.stderr)
            result = subprocess.run([str(directory / "checks")], text=True, capture_output=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("snapshot assertions passed", result.stdout)


if __name__ == "__main__":
    unittest.main()
