import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import build_macos as builder


class BuildConfigurationTests(unittest.TestCase):
    def test_project_is_single_source_for_product_and_versions(self):
        info = builder.bundle_metadata(builder.PROJECT)
        release = builder.build_metadata(builder.PROJECT, True, "arm64")
        self.assertEqual(info["CFBundleShortVersionString"], builder.PROJECT["version"])
        self.assertEqual(info["CFBundleVersion"], builder.PROJECT["version"])
        self.assertEqual(info["CFBundleExecutable"], builder.PROJECT["executable_name"])
        self.assertEqual(info["CFBundleIconFile"], "AppIcon.icns")
        self.assertEqual(release["version"], info["CFBundleVersion"])
        self.assertEqual(release["mode"], "release")
        self.assertEqual(release["reader"], "frozen")

    def test_pyinstaller_uses_onedir_embedded_config_and_current_python(self):
        command = builder.pyinstaller_command(Path("/tmp/local-build"), "arm64", "Developer ID Application: Example")
        self.assertEqual(command[:3], [sys.executable, "-m", "PyInstaller"])
        self.assertIn("--onedir", command)
        self.assertNotIn("--onefile", command)
        self.assertEqual(command[command.index("--target-arch") + 1], "arm64")
        self.assertEqual(command[command.index("--contents-directory") + 1], "_internal")
        self.assertEqual(command[command.index("--add-data") + 1], str(ROOT / "models.json") + os.pathsep + ".")
        data = {command[index + 1] for index, flag in enumerate(command) if flag == "--add-data"}
        self.assertEqual(data, {str(ROOT / "models.json") + os.pathsep + ".",
                                str(ROOT / "fingerprints.json") + os.pathsep + ".",
                                str(ROOT / "assets" / "fingerprint-gpt-bank.json") + os.pathsep + "assets"})
        self.assertEqual(command[command.index("--codesign-identity") + 1], "Developer ID Application: Example")
        requirements = (ROOT / "packaging/requirements-build.txt").read_text()
        self.assertIn("PyInstaller==" + builder.PYINSTALLER_VERSION, requirements)

    def test_cross_architecture_cannot_be_mislabeled(self):
        with patch.object(builder.platform, "machine", return_value="arm64"):
            builder.validate_architecture("arm64")
            with self.assertRaisesRegex(RuntimeError, "不同"):
                builder.validate_architecture("x86_64")

    def test_project_rejects_path_traversal_in_bundle_filename(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "project.json"
            project = dict(builder.PROJECT, executable_name="../outside")
            path.write_text(json.dumps(project))
            with self.assertRaises(RuntimeError):
                builder.read_project(path)

    def test_release_requires_frozen_reader_and_dev_launcher_accepts_release(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            app = root / "Model Beacon.app"
            resources = app / "Contents/Resources"
            executable = app / "Contents/MacOS" / builder.PROJECT["executable_name"]
            executable.parent.mkdir(parents=True)
            resources.mkdir()
            executable.touch()
            (resources / "build-info.json").write_text(json.dumps(builder.build_metadata(builder.PROJECT, False, "arm64")))
            with patch.object(builder, "APP_PATH", app), patch.object(builder, "INPUTS", ()), patch.object(builder.platform, "machine", return_value="arm64"):
                self.assertFalse(builder.needs_build())
                self.assertTrue(builder.needs_build(release=True))
                (resources / "build-info.json").write_text(json.dumps(builder.build_metadata(builder.PROJECT, True, "arm64")))
                (resources / "reader").mkdir()
                (resources / "reader" / builder.READER_NAME).touch()
                self.assertFalse(builder.needs_build(release=True))
                self.assertFalse(builder.needs_build())


class PortabilityInspectionTests(unittest.TestCase):
    def inspect(self, dependencies=None, load_commands=None, architecture="arm64"):
        default_dependencies = "reader:\n\t@rpath/libpython3.12.dylib (compatibility version 3.12.0, current version 3.12.0)\n\t/usr/lib/libSystem.B.dylib (compatibility version 1.0.0, current version 1.0.0)\n"
        default_load_commands = "Load command 0\n cmd LC_RPATH\n cmdsize 48\n path @executable_path/_internal (offset 12)\nLoad command 1\n cmd LC_BUILD_VERSION\n minos 11.0\n sdk 15.0\n"
        with patch.object(builder.subprocess, "check_output", side_effect=[architecture + "\n", dependencies or default_dependencies, load_commands or default_load_commands]):
            builder.inspect_binary(Path("reader"), "arm64", "13.0")

    def test_relative_runtime_links_and_system_libraries_are_portable(self):
        self.inspect()

    def test_external_python_dependency_is_rejected(self):
        with self.assertRaisesRegex(RuntimeError, "构建机路径"):
            self.inspect(dependencies="reader:\n\t/Users/build/python/lib/libpython3.12.dylib (compatibility version 3.12.0)\n")

    def test_external_rpath_is_rejected(self):
        with self.assertRaisesRegex(RuntimeError, "RPATH"):
            self.inspect(load_commands="cmd LC_RPATH\npath /opt/homebrew/lib (offset 12)\n")

    def test_runtime_minimum_above_product_declaration_is_rejected(self):
        with self.assertRaisesRegex(RuntimeError, "高于项目声明"):
            self.inspect(load_commands="cmd LC_BUILD_VERSION\nminos 15.0\nsdk 27.0\n")

    def test_mismatched_binary_architecture_is_rejected(self):
        with self.assertRaisesRegex(RuntimeError, "架构"):
            self.inspect(architecture="x86_64")


if __name__ == "__main__":
    unittest.main()
