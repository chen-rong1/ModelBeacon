#!/usr/bin/env python3
"""Build Model Beacon's native shell and optional self-contained local reader.

Development builds retain the source reader for quick local iteration. Release
builds freeze it with the build environment's Python, so recipients need neither
Python nor Xcode. Run release builds on the architecture being distributed.
"""
from __future__ import annotations

import argparse
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import plistlib
import shutil
import subprocess
import sys
import tempfile
from typing import Any, Optional

ROOT = Path(__file__).resolve().parent
PROJECT_FILE = ROOT / "project.json"
READER_NAME = "model-beacon-reader"
PYINSTALLER_VERSION = "6.22.3"
MACHO_MAGIC = {b"\xfe\xed\xfa\xce", b"\xce\xfa\xed\xfe", b"\xfe\xed\xfa\xcf", b"\xcf\xfa\xed\xfe", b"\xca\xfe\xba\xbe", b"\xbe\xba\xfe\xca"}


def read_project(path: Path = PROJECT_FILE) -> dict[str, str]:
    with path.open(encoding="utf-8") as stream:
        project = json.load(stream)
    fields = ("product_name", "display_name", "slug", "version", "bundle_identifier", "executable_name", "minimum_macos")
    if not isinstance(project, dict) or any(not isinstance(project.get(key), str) or not project[key] for key in fields):
        raise RuntimeError("project.json 缺少有效的产品、版本或平台字段。")
    for key in ("product_name", "slug", "executable_name"):
        if any(character in project[key] for character in ("/", "\\", "\x00")) or project[key] in (".", ".."):
            raise RuntimeError("project.json 的文件名称字段无效：" + key)
    return project


PROJECT = read_project()
APP_PATH = ROOT / "dist" / (PROJECT["product_name"] + ".app")
INPUTS = (
    ROOT / "macos" / "PassiveModelMonitor.swift",
    ROOT / "passive_monitor.py",
    ROOT / "models.json",
    ROOT / "fingerprints.json",
    ROOT / "attribution.py",
    ROOT / "monitor_runtime.py",
    ROOT / "codex_sampler.py",
    ROOT / "fingerprint_reference.py",
    ROOT / "assets" / "fingerprint-gpt-bank.json",
    PROJECT_FILE,
    ROOT / "assets" / "AppIcon.icns",
    Path(__file__).resolve(),
    ROOT / "packaging" / "requirements-build.txt",
    ROOT / "macos" / "FloatingGeometry.swift",
)


def build_metadata(project: dict[str, str], release: bool, architecture: str) -> dict[str, Any]:
    return {
        "product_name": project["product_name"], "version": project["version"],
        "mode": "release" if release else "development", "architecture": architecture,
        "reader": "frozen" if release else "source", "python_version": platform.python_version(),
    }


def bundle_metadata(project: dict[str, str]) -> dict[str, Any]:
    return {
        "CFBundleName": project["product_name"], "CFBundleDisplayName": project["display_name"],
        "CFBundleIdentifier": project["bundle_identifier"], "CFBundleExecutable": project["executable_name"],
        "CFBundlePackageType": "APPL", "CFBundleShortVersionString": project["version"],
        "CFBundleVersion": project["version"], "CFBundleIconFile": "AppIcon.icns",
        "LSMinimumSystemVersion": project["minimum_macos"], "LSUIElement": True,
        "NSHighResolutionCapable": True, "NSSupportsAutomaticTermination": False,
        "CFBundleURLTypes": [{"CFBundleURLName": project["bundle_identifier"] + ".bind",
                              "CFBundleURLSchemes": ["modelbeacon"]}],
    }


def needs_build(release: bool = False, arch: Optional[str] = None) -> bool:
    executable = APP_PATH / "Contents" / "MacOS" / PROJECT["executable_name"]
    resources = APP_PATH / "Contents" / "Resources"
    try:
        if not executable.is_file():
            return True
        metadata = json.loads((resources / "build-info.json").read_text(encoding="utf-8"))
        if metadata.get("version") != PROJECT["version"] or metadata.get("product_name") != PROJECT["product_name"]:
            return True
        if metadata.get("architecture") != (arch or platform.machine()):
            return True
        if release and (metadata.get("mode") != "release" or not (resources / "reader" / READER_NAME).is_file()):
            return True
        built = executable.stat().st_mtime_ns
        return any(not path.is_file() or path.stat().st_mtime_ns > built for path in INPUTS)
    except (OSError, ValueError, TypeError):
        return True


def validate_architecture(architecture: str) -> None:
    if architecture not in ("arm64", "x86_64"):
        raise RuntimeError("只支持 arm64 或 x86_64 构建目标。")
    if architecture != platform.machine():
        raise RuntimeError("目标架构 {} 与构建 Python 的架构 {} 不同；请在对应架构的 macOS/Python 环境构建，不能将本机产物标成另一架构。".format(architecture, platform.machine()))


def pyinstaller_command(staging: Path, architecture: str, identity: str = "") -> list[str]:
    command = [
        sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--onedir", "--console",
        "--name", READER_NAME, "--target-arch", architecture,
        "--contents-directory", "_internal", "--optimize", "1",
        "--add-data", str(ROOT / "models.json") + os.pathsep + ".",
        "--add-data", str(ROOT / "fingerprints.json") + os.pathsep + ".",
        "--add-data", str(ROOT / "assets" / "fingerprint-gpt-bank.json") + os.pathsep + "assets",
        "--distpath", str(staging / "reader-dist"), "--workpath", str(staging / "reader-work"),
        "--specpath", str(staging / "reader-spec"), "--log-level", "WARN",
    ]
    if identity:
        command.extend(["--codesign-identity", identity])
    command.append(str(ROOT / "passive_monitor.py"))
    return command


def freeze_reader(staging: Path, resources: Path, architecture: str, identity: str) -> None:
    try:
        installed = importlib.metadata.version("pyinstaller")
    except importlib.metadata.PackageNotFoundError as error:
        raise RuntimeError("发布构建需要独立构建环境：python3 -m pip install -r packaging/requirements-build.txt") from error
    if installed != PYINSTALLER_VERSION:
        raise RuntimeError("请安装固定的 PyInstaller {}；当前为 {}。".format(PYINSTALLER_VERSION, installed))
    environment = os.environ.copy()
    environment["PYINSTALLER_CONFIG_DIR"] = str(staging / "pyinstaller-cache")
    subprocess.run(pyinstaller_command(staging, architecture, identity), env=environment, check=True)
    frozen = staging / "reader-dist" / READER_NAME
    if not (frozen / READER_NAME).is_file() or any(not (frozen / "_internal" / name).is_file() for name in ("models.json", "fingerprints.json", "assets/fingerprint-gpt-bank.json")):
        raise RuntimeError("冻结后端缺少可执行文件或型号配置。")
    shutil.copytree(frozen, resources / "reader", symlinks=True)


def macho_files(root: Path) -> list[Path]:
    binaries = []
    for path in root.rglob("*"):
        if path.is_symlink() or not path.is_file():
            continue
        with path.open("rb") as stream:
            if stream.read(4) in MACHO_MAGIC:
                binaries.append(path)
    return binaries


def version_tuple(value: str) -> tuple[int, ...]:
    pieces = tuple(int(part) for part in value.split("."))
    return pieces + (0,) * max(0, 3 - len(pieces))


def inspect_binary(path: Path, architecture: str, minimum_macos: str) -> None:
    architectures = subprocess.check_output(["/usr/bin/lipo", "-archs", str(path)], text=True).strip().split()
    if architectures != [architecture]:
        raise RuntimeError("二进制架构与安装包声明不一致：{} ({})".format(path.name, ", ".join(architectures)))
    dependencies = subprocess.check_output(["/usr/bin/otool", "-L", str(path)], text=True)
    for line in dependencies.splitlines()[1:]:
        dependency = line.strip().split(" (", 1)[0]
        if dependency.startswith("/") and not dependency.startswith(("/usr/lib/", "/System/Library/")):
            raise RuntimeError("发现依赖构建机路径的动态库：{} -> {}".format(path.name, dependency))
    commands = subprocess.check_output(["/usr/bin/otool", "-l", str(path)], text=True)
    command = ""
    for raw in commands.splitlines():
        line = raw.strip()
        if line.startswith("cmd "):
            command = line.split(maxsplit=1)[1]
        elif command == "LC_RPATH" and line.startswith("path "):
            rpath = line.split(" (offset", 1)[0][5:]
            if rpath.startswith("/") and not rpath.startswith(("/usr/lib/", "/System/Library/")):
                raise RuntimeError("发现指向构建机的绝对 RPATH：{} -> {}".format(path.name, rpath))
        elif (command == "LC_BUILD_VERSION" and line.startswith("minos ")) or (command == "LC_VERSION_MIN_MACOSX" and line.startswith("version ")):
            target = line.split()[1]
            if version_tuple(target) > version_tuple(minimum_macos):
                raise RuntimeError("{} 需要 macOS {}，高于项目声明的 {}；请使用兼容的 Python 构建环境。".format(path.name, target, minimum_macos))


def copy_legal(resources: Path) -> None:
    legal = resources / "Legal"
    for filename in ("LICENSE", "THIRD_PARTY_NOTICES.md"):
        source = ROOT / filename
        if source.is_file():
            legal.mkdir(exist_ok=True)
            shutil.copy2(source, legal / filename)
    if (ROOT / "packaging" / "licenses").is_dir():
        shutil.copytree(ROOT / "packaging" / "licenses", legal / "licenses", dirs_exist_ok=True)


def build(release: bool = False, arch: Optional[str] = None) -> Path:
    if platform.system() != "Darwin":
        raise RuntimeError("构建原生悬浮窗口需要 macOS 与 Xcode Command Line Tools。")
    architecture = arch or platform.machine()
    validate_architecture(architecture)
    for path in INPUTS:
        if not path.is_file():
            raise RuntimeError("缺少构建文件：{}".format(path))
    compiler = subprocess.check_output(["/usr/bin/xcrun", "--find", "swiftc"], text=True).strip()
    sdk = subprocess.check_output(["/usr/bin/xcrun", "--show-sdk-path"], text=True).strip()
    build_root = ROOT / ".build"
    build_root.mkdir(exist_ok=True)
    APP_PATH.parent.mkdir(exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix="app-", dir=str(build_root)))
    bundle = staging / APP_PATH.name
    contents = bundle / "Contents"
    resources = contents / "Resources"
    binaries = contents / "MacOS"
    resources.mkdir(parents=True)
    binaries.mkdir()
    executable = binaries / PROJECT["executable_name"]
    identity = os.environ.get("CODE_SIGN_IDENTITY", "").strip()
    try:
        subprocess.run([
            compiler, "-O", "-framework", "AppKit", "-sdk", sdk,
            "-target", "{}-apple-macosx{}".format(architecture, PROJECT["minimum_macos"]),
            "-module-cache-path", str(build_root / "swift-module-cache"),
            str(ROOT / "macos" / "FloatingGeometry.swift"), str(INPUTS[0]), "-o", str(executable),
        ], check=True)
        shutil.copy2(ROOT / "assets" / "AppIcon.icns", resources / "AppIcon.icns")
        if release:
            freeze_reader(staging, resources, architecture, identity)
        else:
            for filename in ("passive_monitor.py", "models.json", "attribution.py", "monitor_runtime.py", "fingerprints.json", "codex_sampler.py", "fingerprint_reference.py"):
                shutil.copy2(ROOT / filename, resources / filename)
            (resources / "assets").mkdir(exist_ok=True)
            shutil.copy2(ROOT / "assets" / "fingerprint-gpt-bank.json", resources / "assets" / "fingerprint-gpt-bank.json")
        copy_legal(resources)
        (resources / "build-info.json").write_text(json.dumps(build_metadata(PROJECT, release, architecture), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        with (contents / "Info.plist").open("wb") as stream:
            plistlib.dump(bundle_metadata(PROJECT), stream)
        for binary in macho_files(bundle):
            inspect_binary(binary, architecture, PROJECT["minimum_macos"])
        # PyInstaller signs collected libraries and its reader bootloader first.
        # The outer bundle is signed only after every resource is in place.
        sign = ["/usr/bin/codesign", "--force", "--sign", identity or "-"]
        if identity:
            sign.extend(["--options", "runtime", "--timestamp"])
        subprocess.run([*sign, str(bundle)], check=True)
        subprocess.run(["/usr/bin/codesign", "--verify", "--deep", "--strict", "--verbose=2", str(bundle)], check=True)
        if APP_PATH.exists():
            shutil.rmtree(APP_PATH)
        shutil.move(str(bundle), str(APP_PATH))
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    print("构建完成：{} ({}, {})".format(APP_PATH, architecture, "独立发布版" if release else "开发版"))
    return APP_PATH


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="构建 Model Beacon macOS 应用。发布版内置 Python 后端。")
    parser.add_argument("--release", action="store_true", help="冻结 Python 后端，生成不依赖系统 Python 的发布版")
    parser.add_argument("--arch", choices=("arm64", "x86_64"), default=None, help="必须与运行构建 Python 的架构相同")
    args = parser.parse_args(argv)
    try:
        build(release=args.release, arch=args.arch)
    except (OSError, RuntimeError, subprocess.CalledProcessError) as error:
        print("构建失败：{}".format(error), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
