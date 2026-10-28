import importlib.util
import io
import json
from pathlib import Path
import plistlib
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("macos_package", ROOT / "scripts" / "package_macos.py")
package = importlib.util.module_from_spec(spec)
spec.loader.exec_module(package)


class ReleaseTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.project = package.load_project()
        self.app = self.root / (self.project["product_name"] + ".app")
        resources = self.app / "Contents" / "Resources"
        resources.mkdir(parents=True)
        self.build_info = resources / "build-info.json"
        self.build = {
            "mode": "release", "architecture": "arm64", "reader": "frozen",
            "version": self.project["version"], "product_name": self.project["product_name"],
            "python_version": "3.12.13",
        }
        self.build_info.write_text(json.dumps(self.build), encoding="utf-8")
        info = {
            "CFBundleName": self.project["product_name"],
            "CFBundleDisplayName": self.project["display_name"],
            "CFBundleIdentifier": self.project["bundle_identifier"],
            "CFBundleExecutable": self.project["executable_name"],
            "CFBundlePackageType": "APPL", "CFBundleIconFile": "AppIcon.icns",
            "CFBundleShortVersionString": self.project["version"],
            "LSMinimumSystemVersion": self.project["minimum_macos"],
            "CFBundleURLTypes": [{"CFBundleURLName": self.project["bundle_identifier"] + ".bind", "CFBundleURLSchemes": ["modelbeacon"]}],
        }
        with (self.app / "Contents" / "Info.plist").open("wb") as handle:
            plistlib.dump(info, handle)
        self.reader = resources / "reader" / "model-beacon-reader"
        main = self.app / "Contents" / "MacOS" / self.project["executable_name"]
        for path in (main, self.reader):
            path.parent.mkdir(exist_ok=True)
            path.write_bytes(b"synthetic Mach-O fixture")
            path.chmod(0o755)
        shutil.copy2(ROOT / "assets" / "AppIcon.icns", resources / "AppIcon.icns")
        (self.reader.parent / "_internal").mkdir()
        shutil.copy2(ROOT / "models.json", self.reader.parent / "_internal" / "models.json")
        shutil.copy2(ROOT / "fingerprints.json", self.reader.parent / "_internal" / "fingerprints.json")
        (self.reader.parent / "_internal" / "assets").mkdir()
        shutil.copy2(ROOT / "assets" / "fingerprint-gpt-bank.json", self.reader.parent / "_internal" / "assets" / "fingerprint-gpt-bank.json")
        legal = resources / "Legal"
        legal.mkdir()
        for name in ("LICENSE", "THIRD_PARTY_NOTICES.md"):
            if (ROOT / name).is_file():
                shutil.copy2(ROOT / name, legal / name)
        if (ROOT / "packaging" / "licenses").is_dir():
            shutil.copytree(ROOT / "packaging" / "licenses", legal / "licenses")

    def validate(self):
        with patch.object(package, "architectures", return_value={"arm64"}), \
             patch.object(package, "signing_status", return_value="ad-hoc"), \
             patch.object(package, "check_frozen_runtime"):
            return package.validate_app(self.app, self.project)

    def test_version_tag_and_manual_input_must_match_project(self):
        version = self.project["version"]
        self.assertEqual(package.validate_version(self.project, version, "v" + version), version)
        for requested, tag in (("9.9.9", None), (None, "v9.9.9"), ("v" + version, None)):
            with self.subTest(requested=requested, tag=tag), self.assertRaises(ValueError):
                package.validate_version(self.project, requested, tag)

    def test_frozen_reader_and_release_marker_are_both_required(self):
        self.assertEqual(self.validate()["signature"], "ad-hoc")
        for field, value in (("mode", "development"), ("reader", "source"), ("architecture", "x86_64"), ("version", "9.9.9")):
            self.build_info.write_text(json.dumps({**self.build, field: value}), encoding="utf-8")
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.validate()
        self.build_info.write_text(json.dumps(self.build), encoding="utf-8")
        self.reader.unlink()
        with self.assertRaises(ValueError):
            self.validate()

    def test_bundle_version_and_actual_architecture_are_checked(self):
        with patch.object(package, "architectures", return_value={"x86_64"}), self.assertRaises(ValueError):
            package.validate_app(self.app, self.project)
        info_path = self.app / "Contents" / "Info.plist"
        with info_path.open("rb") as handle:
            info = plistlib.load(handle)
        info["CFBundleShortVersionString"] = "9.9.9"
        with info_path.open("wb") as handle:
            plistlib.dump(info, handle)
        with self.assertRaises(ValueError):
            self.validate()

    def test_release_reference_assets_have_one_unified_bank(self):
        assets = self.reader.parent / "_internal" / "assets"
        (assets / "unused-reference.json").write_text("{}")
        with self.assertRaisesRegex(ValueError, "统一参考库"):
            self.validate()

    def test_source_reader_or_test_data_cannot_enter_release(self):
        resources = self.app / "Contents" / "Resources"
        source = resources / "passive_monitor.py"
        source.touch()
        with self.assertRaises(ValueError):
            self.validate()
        source.unlink()
        (resources / "fixtures").mkdir()
        with self.assertRaises(ValueError):
            self.validate()

    def test_brand_name_and_frozen_model_resources_cannot_be_stale(self):
        models = self.reader.parent / "_internal" / "models.json"
        models.write_text('{"schema_version":1,"families":[],"aliases":{}}', encoding="utf-8")
        with self.assertRaises(ValueError):
            self.validate()
        shutil.copy2(ROOT / "models.json", models)
        info_path = self.app / "Contents" / "Info.plist"
        with info_path.open("rb") as handle:
            info = plistlib.load(handle)
        info["CFBundleDisplayName"] = "旧产品名称"
        with info_path.open("wb") as handle:
            plistlib.dump(info, handle)
        with self.assertRaises(ValueError):
            self.validate()

    def test_packaging_preserves_app_root_and_applications_link(self):
        commands = []

        def simulate(command):
            commands.append(command)
            if command[0] == "/usr/bin/ditto" and "-c" in command:
                Path(command[-1]).write_bytes(b"synthetic ZIP")
            elif command[0] == "/usr/bin/ditto":
                Path(command[-1]).mkdir()
            elif command[0] == "/usr/bin/hdiutil":
                image_root = Path(command[command.index("-srcfolder") + 1])
                self.assertEqual((image_root / "Applications").readlink(), Path("/Applications"))
                self.assertTrue((image_root / self.app.name).is_dir())
                self.assertIn("UDZO", command)
                Path(command[-1]).write_bytes(b"synthetic DMG")
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

        with patch.object(package.platform, "system", return_value="Darwin"), \
             patch.object(package.platform, "machine", return_value="arm64"), \
             patch.object(package, "architectures", return_value={"arm64"}), \
             patch.object(package, "signing_status", return_value="ad-hoc"), \
             patch.object(package, "check_frozen_runtime"), \
             patch.object(package, "checked", side_effect=simulate), \
             patch.dict(package.os.environ, {}, clear=True):
            paths = package.package(self.app, self.root / "packages", self.project)
        archive_command = next(command for command in commands if "-c" in command)
        self.assertIn("--sequesterRsrc", archive_command)
        self.assertIn("--keepParent", archive_command)
        prefix = f'{self.project["slug"]}-{self.project["version"]}-macos-arm64'
        self.assertEqual({path.name for path in paths}, {prefix + ".zip", prefix + ".dmg", prefix + "-manifest.json", "SHA256SUMS.txt"})
        manifest = json.loads(next(path for path in paths if path.suffix == ".json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["signing"], {"kind": "ad-hoc", "notarized": False})
        self.assertEqual(manifest["architecture"], "arm64")
        sums = (self.root / "packages" / "SHA256SUMS.txt").read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(sums), 3)
        for line in sums:
            digest, name = line.split("  ", 1)
            self.assertEqual(digest, package.sha256(self.root / "packages" / name))

    def test_adhoc_notarization_is_rejected_before_network_submission(self):
        with patch.object(package.platform, "system", return_value="Darwin"), \
             patch.object(package.platform, "machine", return_value="arm64"), \
             patch.object(package, "architectures", return_value={"arm64"}), \
             patch.object(package, "signing_status", return_value="ad-hoc"), \
             patch.object(package, "check_frozen_runtime"), \
             patch.object(package, "notarize") as notarize, self.assertRaises(ValueError):
            package.package(self.app, self.root / "packages", self.project, notarized=True)
        notarize.assert_not_called()

    def test_runtime_help_is_offline_and_needs_no_external_python(self):
        success = subprocess.CompletedProcess([], 0, stdout="--watch --sessions-dir --control-stdin", stderr="")
        smoke = subprocess.CompletedProcess([], 0, stdout=json.dumps({"schema_version": 2, "attribution": {"available": False}}), stderr="")
        with patch.object(package.subprocess, "run", side_effect=[success, smoke]) as run:
            package.check_frozen_runtime(self.reader)
        args, kwargs = run.call_args_list[0]
        self.assertEqual(args[0], [str(self.reader), "--help"])
        self.assertEqual(kwargs["env"]["PATH"], "")
        self.assertEqual(kwargs["env"]["PYTHONHOME"], "/nonexistent")
        self.assertEqual(kwargs["timeout"], 30)
        self.assertIn("--sessions-dir", run.call_args_list[1].args[0])
        with patch.object(package.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, stdout="", stderr="")), self.assertRaises(ValueError):
            package.check_frozen_runtime(self.reader)

    def test_tool_failure_diagnostics_are_bounded_without_credentials(self):
        failure = subprocess.CalledProcessError(1, ["/usr/bin/hdiutil", "create", "private-argument"],
                                                stderr="Cannot create disk image. secret-id\n" + "x" * 10000)
        captured = io.StringIO()
        with patch.object(package, "package", side_effect=failure), \
             patch.dict(package.os.environ, {"APPLE_NOTARY_KEY_ID": "secret-id"}), \
             patch.object(package.sys, "stderr", captured):
            self.assertEqual(package.main([]), 1)
        message = captured.getvalue()
        self.assertIn("hdiutil", message)
        self.assertIn("Cannot create disk image.", message)
        self.assertIn("[已隐藏]", message)
        self.assertNotIn("secret-id", message)
        self.assertNotIn("private-argument", message)
        self.assertLess(len(message), 8500)


if __name__ == "__main__":
    unittest.main()
