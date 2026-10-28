#!/usr/bin/env python3
"""Validate and package the self-contained Apple Silicon release application.

No build, download or publication happens here. Notarization is opt-in and needs
an existing Developer ID signature plus App Store Connect API credentials.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import plistlib
import re
import shutil
import subprocess
import sys
import tempfile
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
VERSION_PATTERN = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z.-]+)?")
FORBIDDEN_PARTS = {".git", ".build", "__pycache__", "tests", "fixtures", ".pytest_cache"}


def load_project(path: Path = ROOT / "project.json") -> dict[str, Any]:
    project = json.loads(path.read_text(encoding="utf-8"))
    required = ("product_name", "display_name", "slug", "version", "bundle_identifier", "executable_name", "minimum_macos")
    if not isinstance(project, dict) or any(not isinstance(project.get(key), str) or not project[key] for key in required):
        raise ValueError("project.json 缺少发布所需的项目字段。")
    if not VERSION_PATTERN.fullmatch(project["version"]):
        raise ValueError("project.json version 必须为可用于发布文件名的版本号。")
    if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", project["slug"]):
        raise ValueError("project.json slug 必须为小写字母、数字和连字符。")
    for key in ("product_name", "executable_name"):
        if any(char in project[key] for char in ("/", "\\", "\n", "\r")) or project[key] in (".", ".."):
            raise ValueError("project.json 中的产品名称或可执行文件名不合法。")
    return project


def validate_version(project: dict[str, Any], requested: str | None = None, tag: str | None = None) -> str:
    version = project["version"]
    if requested and requested != version:
        raise ValueError(f"要求发布 {requested}，但 project.json 的版本是 {version}。")
    if tag and tag != f"v{version}":
        raise ValueError(f"标签 {tag} 与 project.json 版本 v{version} 不一致。")
    return version


def checked(command: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, check=True, text=True, capture_output=True)


def architectures(executable: Path) -> set[str]:
    return set(checked(["/usr/bin/lipo", "-archs", str(executable)]).stdout.split())


def signing_status(app: Path) -> str:
    checked(["/usr/bin/codesign", "--verify", "--deep", "--strict", "--verbose=2", str(app)])
    result = checked(["/usr/bin/codesign", "--display", "--verbose=4", str(app)])
    description = result.stdout + result.stderr
    if "Signature=adhoc" in description:
        return "ad-hoc"
    if "Authority=Developer ID Application:" in description:
        return "developer-id"
    return "other-valid-signature"


def check_frozen_runtime(reader: Path) -> None:
    # --help exits before constructing a monitor or reading any conversation.
    # A release must run without a system Python or build-machine module path.
    result = subprocess.run(
        [str(reader), "--help"], check=True, text=True, capture_output=True, timeout=30,
        env={"PATH": "", "PYTHONHOME": "/nonexistent", "PYTHONPATH": "/nonexistent", "LANG": "en_US.UTF-8"},
    )
    if any(flag not in result.stdout for flag in ("--sessions-dir", "--watch", "--control-stdin")):
        raise ValueError("冻结读取程序未提供预期的离线命令接口。")
    with tempfile.TemporaryDirectory(prefix="beacon-frozen-check-") as temporary:
        # A missing fixture directory deliberately yields an idle state and
        # never reads real Codex data or creates a sampling context.
        smoke = subprocess.run(
            [str(reader), "--sessions-dir", str(Path(temporary) / "empty")],
            check=True, text=True, capture_output=True, timeout=30,
            env={"PATH": "", "PYTHONHOME": "/nonexistent", "PYTHONPATH": "/nonexistent", "LANG": "en_US.UTF-8"},
        )
        state = json.loads(smoke.stdout)
        if state.get("schema_version") != 2 or state.get("attribution", {}).get("available") is not False:
            raise ValueError("冻结读取程序未能离线加载监测控制器、归因模块与参考库。")


def validate_app(app: Path, project: dict[str, Any], arch: str = "arm64") -> dict[str, Any]:
    if arch != "arm64":
        raise ValueError("本发布流程仅验证 macOS Apple Silicon arm64。")
    if not app.is_dir() or app.suffix != ".app":
        raise ValueError(f"找不到待打包的应用：{app}")
    if app.name != f'{project["product_name"]}.app':
        raise ValueError("应用文件名与 project.json 的产品名称不一致。")
    contents = app / "Contents"
    with (contents / "Info.plist").open("rb") as handle:
        info = plistlib.load(handle)
    expected = {
        "CFBundleName": project["product_name"],
        "CFBundleDisplayName": project["display_name"],
        "CFBundleIdentifier": project["bundle_identifier"],
        "CFBundleExecutable": project["executable_name"],
        "CFBundlePackageType": "APPL",
        "CFBundleIconFile": "AppIcon.icns",
        "CFBundleShortVersionString": project["version"],
        "LSMinimumSystemVersion": project["minimum_macos"],
        "CFBundleURLTypes": [{"CFBundleURLName": project["bundle_identifier"] + ".bind", "CFBundleURLSchemes": ["modelbeacon"]}],
    }
    for key, value in expected.items():
        if info.get(key) != value:
            raise ValueError(f"应用 {key} 与 project.json 不一致。")
    resources = contents / "Resources"
    build = json.loads((resources / "build-info.json").read_text(encoding="utf-8"))
    expected_build = {
        "mode": "release", "architecture": arch, "version": project["version"],
        "reader": "frozen", "product_name": project["product_name"],
    }
    for key, value in expected_build.items():
        if build.get(key) != value:
            raise ValueError(f"只能发布自带读取程序的 release 应用；build-info.json 的 {key} 不匹配。")
    binaries = (contents / "MacOS" / project["executable_name"], resources / "reader" / "model-beacon-reader")
    for executable in binaries:
        if not executable.is_file() or not os.access(executable, os.X_OK):
            raise ValueError(f"发布应用缺少可执行文件：{executable.relative_to(app)}")
        if arch not in architectures(executable):
            raise ValueError(f"可执行文件不包含 {arch} 架构：{executable.relative_to(app)}")
    if (resources / "passive_monitor.py").exists():
        raise ValueError("发布应用仍依赖源码读取程序，请使用 --release 重新构建。")
    resource_pairs = [
        (ROOT / "assets" / "AppIcon.icns", resources / "AppIcon.icns"),
        (ROOT / "models.json", resources / "reader" / "_internal" / "models.json"),
        (ROOT / "fingerprints.json", resources / "reader" / "_internal" / "fingerprints.json"),
        (ROOT / "assets" / "fingerprint-gpt-bank.json", resources / "reader" / "_internal" / "assets" / "fingerprint-gpt-bank.json"),
    ]
    for name in ("LICENSE", "THIRD_PARTY_NOTICES.md"):
        if (ROOT / name).is_file():
            resource_pairs.append((ROOT / name, resources / "Legal" / name))
    license_root = ROOT / "packaging" / "licenses"
    if license_root.is_dir():
        resource_pairs.extend((source, resources / "Legal" / "licenses" / source.relative_to(license_root))
                              for source in license_root.rglob("*") if source.is_file())
    for source, bundled in resource_pairs:
        if not source.is_file() or not bundled.is_file() or sha256(source) != sha256(bundled):
            raise ValueError(f"发布资源缺失或与当前源码不一致，请重新构建：{bundled.relative_to(app)}")
    reference_assets = resources / "reader" / "_internal" / "assets"
    if {path.name for path in reference_assets.iterdir()} != {"fingerprint-gpt-bank.json"}:
        raise ValueError("归因资源只应包含统一参考库，请重新构建。")
    for path in app.rglob("*"):
        relative = path.relative_to(app)
        if any(part in FORBIDDEN_PARTS for part in relative.parts):
            raise ValueError(f"应用混入开发文件：{relative}")
    check_frozen_runtime(binaries[1])
    return {"build": build, "signature": signing_status(app)}


def make_zip(app: Path, destination: Path) -> None:
    checked(["/usr/bin/ditto", "-c", "-k", "--sequesterRsrc", "--keepParent", str(app), str(destination)])


def notarize(path: Path) -> None:
    names = ("APPLE_NOTARY_KEY_PATH", "APPLE_NOTARY_KEY_ID", "APPLE_NOTARY_ISSUER_ID")
    if any(not os.environ.get(name) for name in names):
        raise ValueError("公证需要 APPLE_NOTARY_KEY_PATH、APPLE_NOTARY_KEY_ID 和 APPLE_NOTARY_ISSUER_ID。")
    key = Path(os.environ[names[0]])
    if not key.is_file():
        raise ValueError("找不到 App Store Connect 公证私钥文件。")
    result = checked([
        "/usr/bin/xcrun", "notarytool", "submit", str(path), "--key", str(key),
        "--key-id", os.environ[names[1]], "--issuer", os.environ[names[2]],
        "--wait", "--output-format", "json",
    ])
    response = json.loads(result.stdout)
    if response.get("status") != "Accepted":
        raise RuntimeError("Apple 公证未通过；不会生成声称已公证的发布包。")


def staple(path: Path) -> None:
    checked(["/usr/bin/xcrun", "stapler", "staple", str(path)])
    checked(["/usr/bin/xcrun", "stapler", "validate", str(path)])


def make_dmg(app: Path, destination: Path, project: dict[str, Any], staging: Path) -> None:
    image_root = staging / "image"
    image_root.mkdir()
    checked(["/usr/bin/ditto", str(app), str(image_root / app.name)])
    (image_root / "Applications").symlink_to("/Applications", target_is_directory=True)
    checked([
        "/usr/bin/hdiutil", "create", "-volname", f'{project["product_name"]} {project["version"]}',
        "-srcfolder", str(image_root), "-format", "UDZO", "-fs", "HFS+", "-ov", str(destination),
    ])
    identity = os.environ.get("CODE_SIGN_IDENTITY")
    if identity:
        checked(["/usr/bin/codesign", "--force", "--timestamp", "--sign", identity, str(destination)])


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_manifests(directory: Path, artifacts: list[Path], project: dict[str, Any], validation: dict[str, Any], *, notarized: bool) -> list[Path]:
    prefix = f'{project["slug"]}-{project["version"]}-macos-arm64'
    manifest_path = directory / f"{prefix}-manifest.json"
    manifest = {
        "schema_version": 1, "product_name": project["product_name"], "display_name": project["display_name"],
        "version": project["version"], "platform": "macos", "architecture": "arm64",
        "minimum_macos": project["minimum_macos"], "bundle_identifier": project["bundle_identifier"],
        "application": f'{project["product_name"]}.app',
        "signing": {"kind": validation["signature"], "notarized": notarized},
        "build": validation["build"],
        "artifacts": [{"name": path.name, "bytes": path.stat().st_size, "sha256": sha256(path)} for path in artifacts],
    }
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    files = [*artifacts, manifest_path]
    sums = directory / "SHA256SUMS.txt"
    sums.write_text("".join(f"{sha256(path)}  {path.name}\n" for path in sorted(files)), encoding="utf-8")
    return [*files, sums]


def package(app: Path, output: Path, project: dict[str, Any], *, arch: str = "arm64", notarized: bool = False, zip_only: bool = False) -> list[Path]:
    if platform.system() != "Darwin" or platform.machine() != "arm64":
        raise RuntimeError("发布包必须在 macOS arm64 上构建和验证。")
    app, output = app.resolve(), output.resolve()
    if app == output or app in output.parents:
        raise ValueError("发布目录不能位于应用内部。")
    validation = validate_app(app, project, arch)
    if notarized and validation["signature"] != "developer-id":
        raise ValueError("公证需要 Developer ID Application 签名；临时签名无法公证。")
    prefix = f'{project["slug"]}-{project["version"]}-macos-{arch}'
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="model-beacon-package-") as temp:
        staging = Path(temp)
        if notarized:
            submission = staging / "notary-submission.zip"
            make_zip(app, submission)
            notarize(submission)
            staple(app)
            validation = validate_app(app, project, arch)
        zip_path = staging / f"{prefix}.zip"
        make_zip(app, zip_path)
        artifacts = [zip_path]
        if not zip_only:
            dmg = staging / f"{prefix}.dmg"
            make_dmg(app, dmg, project, staging)
            if notarized:
                notarize(dmg)
                staple(dmg)
            artifacts.append(dmg)
        generated = write_manifests(staging, artifacts, project, validation, notarized=notarized)
        published = []
        for path in generated:
            destination = output / path.name
            shutil.move(str(path), str(destination))
            published.append(destination)
    return published


def report_failure(error: Exception) -> None:
    if not isinstance(error, (subprocess.CalledProcessError, subprocess.TimeoutExpired)):
        print(f"打包失败：{error}", file=sys.stderr)
        return
    command = error.cmd
    tool = Path(command[0]).name if isinstance(command, (list, tuple)) and command else "外部工具"
    reason = "超时" if isinstance(error, subprocess.TimeoutExpired) else f"退出码 {error.returncode}"
    # Report the tool, not its argument vector: a notary invocation includes
    # credential identifiers and a private-key path. Tool diagnostics are bounded.
    print(f"打包失败：{tool} 执行失败（{reason}）。", file=sys.stderr)
    details = error.stderr or error.stdout or ""
    if isinstance(details, bytes):
        details = details.decode("utf-8", errors="replace")
    for name in ("APPLE_NOTARY_KEY_ID", "APPLE_NOTARY_ISSUER_ID", "APPLE_NOTARY_KEY_PATH", "CODE_SIGN_IDENTITY"):
        value = os.environ.get(name)
        if value:
            details = details.replace(value, "[已隐藏]")
    details = re.sub(r"-----BEGIN [^-]*PRIVATE KEY-----.*?-----END [^-]*PRIVATE KEY-----", "[私钥已隐藏]", details, flags=re.DOTALL)
    if details:
        print(details[:8192].rstrip(), file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="验证 release app 并创建 ZIP、DMG、清单与 SHA-256。")
    parser.add_argument("--app", type=Path, help="要发布的自带读取程序的 .app")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "dist" / "packages")
    parser.add_argument("--arch", choices=("arm64",), default="arm64")
    parser.add_argument("--version", help="必须与 project.json 完全一致；用于 CI 防止错发版本")
    parser.add_argument("--notarize", action="store_true", help="使用已配置的 App Store Connect API 凭据提交 Apple 公证")
    parser.add_argument("--zip-only", action="store_true", help="仅生成 ZIP（默认同时生成 ZIP 与 DMG）")
    args = parser.parse_args(argv)
    try:
        project = load_project()
        validate_version(project, args.version)
        app = args.app or ROOT / "dist" / f'{project["product_name"]}.app'
        generated = package(app, args.output_dir, project, arch=args.arch, notarized=args.notarize, zip_only=args.zip_only)
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
        report_failure(error)
        return 1
    print("已生成：")
    for path in generated:
        print(path)
    if not args.notarize:
        print("未经过 Apple 公证；实际签名类型记录于发布清单。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
