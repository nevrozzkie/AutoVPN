#!/usr/bin/env python3
"""Assemble an immutable, public AutoVPN OpenWrt release directory.

This deliberately does *not* build, sign, upload, or publish packages.  It
checks APKs produced by an OpenWrt SDK and copies the exact artifacts into a
new directory that can then be attached to a GitHub release by a human.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlparse


APP_VERSION = "0.14.6"
ALLOWED_PACKAGES = {"autovpn-controller", "kmod-amneziawg", "amneziawg-tools"}
RELEASE_RE = re.compile(r"25\.12\.\d+")
SAFE_OWNER_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]*")
SAFE_REPO_TAG_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
SAFE_TARGET_RE = re.compile(r"[a-z0-9_]+/[a-z0-9_-]+")
# OpenWrt's actual Filogic package architecture is aarch64_cortex-a53.
SAFE_ARCH_RE = re.compile(r"[a-z0-9_-]+")


class ReleaseError(Exception):
    pass


def die(message: str) -> None:
    raise ReleaseError(message)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def regular_file(path: Path, label: str) -> Path:
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        die(f"{label} does not exist: {path}")
    if stat.S_ISLNK(mode) or not stat.S_ISREG(mode):
        die(f"{label} must be a regular non-symlink file: {path}")
    return path.resolve()


def safe_filename(path: Path) -> str:
    name = path.name
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._+~-]*\.apk", name):
        die(f"unsafe APK filename: {name}")
    return name


def release_url(value: str) -> str:
    parsed = urlparse(value)
    if parsed.scheme != "https" or parsed.netloc != "github.com" or parsed.params or parsed.query or parsed.fragment:
        die("--release-base must be an https://github.com/OWNER/REPO/releases/download/TAG URL")
    parts = parsed.path.split("/")
    if len(parts) != 6 or parts[0] or parts[3:5] != ["releases", "download"]:
        die("--release-base must be an immutable GitHub release download URL")
    owner, repo, tag = parts[1], parts[2], parts[5]
    if not SAFE_OWNER_RE.fullmatch(owner) or not all(SAFE_REPO_TAG_RE.fullmatch(item) for item in (repo, tag)):
        die("GitHub owner, repository and tag must use safe ASCII release segments")
    if tag.lower() in {"latest", "main", "master", "head"}:
        die("release tag must be immutable; latest/main/master/head are forbidden")
    return f"https://github.com/{owner}/{repo}/releases/download/{tag}"


def command(argv: list[str], description: str) -> str:
    try:
        process = subprocess.run(argv, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except OSError as exc:
        die(f"{description} could not start: {exc}")
    if process.returncode:
        detail = process.stderr.strip() or process.stdout.strip()
        die(f"{description} failed: {detail}")
    return process.stdout


def apk_metadata(apk: Path, package: Path, key: Path) -> dict:
    # APK v3's adbdump is the authoritative parser for package metadata.  Do
    # not unpack the opaque ADB archive with tar or guess its wire format.
    with tempfile.TemporaryDirectory(prefix="autovpn-apk-keys-") as tmp:
        keys = Path(tmp)
        shutil.copyfile(key, keys / "autovpn-signing.pem")
        command([str(apk), "verify", "--keys-dir", str(keys), str(package)], f"APK verification for {package.name}")
        text = command([str(apk), "adbdump", "--format", "json", str(package)], f"APK metadata read for {package.name}")
    try:
        root = json.loads(text)
    except json.JSONDecodeError as exc:
        die(f"APK metadata is not JSON for {package.name}: {exc}")
    info = root.get("info") if isinstance(root, dict) else None
    if not isinstance(info, dict):
        die(f"APK metadata has no info object for {package.name}")
    return info


def string_list(value: object, label: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        die(f"APK {label} must be an array of strings")
    return value


def validate_metadata(info: dict, expected_name: str, architecture: str, kernel_package: str, app_version: str) -> None:
    name = info.get("name")
    arch = info.get("arch")
    if name != expected_name:
        die(f"APK filename says {expected_name}, metadata says {name!r}")
    if expected_name == "autovpn-controller":
        # APK v3 reports OpenWrt's PKGARCH:=all as ``noarch``.  Older
        # controller releases used ``all``, and a target-specific controller
        # remains valid for the selected release only.
        if arch not in {"noarch", "all", architecture}:
            die(f"APK autovpn-controller has arch {arch!r}; expected noarch, all or {architecture}")
    elif arch != architecture:
        # The AWG module/tools contain native code.  Do not let the
        # controller's noarch compatibility broaden their ABI boundary.
        die(f"APK {expected_name} has arch {arch!r}; expected {architecture}")
    depends = string_list(info.get("depends"), f"{expected_name} depends")
    if expected_name == "autovpn-controller":
        version = info.get("version")
        if not isinstance(version, str) or not re.fullmatch(re.escape(app_version) + r"-r[0-9]+", version):
            die(f"autovpn-controller version must be {app_version}-rNUMBER, got {version!r}")
    exact_dependency = f"kernel={kernel_package}"
    if expected_name == "kmod-amneziawg" and exact_dependency not in depends:
        die(f"kmod-amneziawg must depend on the exact SDK kernel package {exact_dependency!r}")


def package_name_from_filename(filename: str) -> str:
    # Names may contain hyphens. Metadata, not the filename, is authoritative;
    # this just prevents accidental non-AutoVPN artifacts before invoking APK.
    for name in sorted(ALLOWED_PACKAGES, key=len, reverse=True):
        if filename == name + ".apk" or filename.startswith(name + "-"):
            return name
    die(f"only AutoVPN custom packages are allowed: {filename}")
    raise AssertionError("unreachable")


def replace_template(template: Path, destination: Path, release_base: str, key_hash: str, manifest_hash: str) -> None:
    text = template.read_text(encoding="utf-8")
    if (text.count("@AUTOVPN_RELEASE_BASE@") != 1 or
            text.count("@AUTOVPN_SIGNING_KEY_SHA256@") != 1 or
            text.count("@AUTOVPN_MANIFEST_SHA256@") != 1):
        die("installer template must contain each AutoVPN release placeholder exactly once")
    text = (text.replace("@AUTOVPN_RELEASE_BASE@", release_base)
                .replace("@AUTOVPN_SIGNING_KEY_SHA256@", key_hash)
                .replace("@AUTOVPN_MANIFEST_SHA256@", manifest_hash))
    if "@AUTOVPN_" in text:
        die("installer template contains an unresolved AutoVPN placeholder")
    destination.write_text(text, encoding="utf-8")
    destination.chmod(0o755)


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apk", required=True, help="OpenWrt SDK host apk (APK v3) executable")
    parser.add_argument("--release", required=True, help="OpenWrt release, exactly 25.12.x")
    parser.add_argument("--target", required=True, help="OpenWrt target, e.g. mediatek/filogic")
    parser.add_argument("--architecture", required=True, help="OpenWrt package architecture")
    parser.add_argument("--kernel-release", required=True, help="target uname -r")
    parser.add_argument("--kernel-package", required=True, help="exact installed kernel package version from APK query")
    parser.add_argument("--release-base", required=True, help="immutable GitHub release download base")
    parser.add_argument("--signing-key", required=True, help="public APK signing key PEM")
    parser.add_argument("--package", action="append", required=True, help="signed custom APK; repeat for each package")
    parser.add_argument("--min-free-kib", required=True, type=int, help="measured full install footprint plus reserve")
    parser.add_argument("--min-tmp-kib", required=True, type=int, help="measured download/temp budget")
    parser.add_argument("--output", required=True, help="new release output directory")
    parser.add_argument("--install-template", default=str(Path(__file__).resolve().with_name("install.sh")), help="installer template with release placeholders (defaults to the adjacent install.sh)")
    parser.add_argument("--app-version", default=APP_VERSION)
    return parser.parse_args(argv)


def require_new_output(path: Path) -> Path:
    """Reject an existing/dangling output; canonicalize any system temp alias."""
    try:
        output = path.parent.resolve(strict=True) / path.name
    except OSError as exc:
        die(f"cannot resolve output parent: {exc}")
    if os.path.lexists(output):
        die(f"output must be a new directory and must not already exist: {output}")
    return output


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    try:
        if not RELEASE_RE.fullmatch(args.release):
            die("--release must be exactly 25.12.x")
        if not SAFE_TARGET_RE.fullmatch(args.target):
            die("--target must have the form family/subtarget")
        if not SAFE_ARCH_RE.fullmatch(args.architecture):
            die("unsafe --architecture")
        if not args.kernel_release or any(char.isspace() for char in args.kernel_release):
            die("--kernel-release must be a non-empty uname -r value without whitespace")
        if not args.kernel_package or args.kernel_package.startswith("kernel=") or any(char.isspace() for char in args.kernel_package):
            die("--kernel-package must be the exact no-whitespace installed kernel version (without kernel=)")
        if not (4096 <= args.min_free_kib <= 9_999_999 and 4096 <= args.min_tmp_kib <= 9_999_999):
            die("minimum free and tmp KiB budgets must be measured values between 4096 and 9999999")
        release_base = release_url(args.release_base)
        apk = regular_file(Path(args.apk), "APK v3 CLI")
        if not os.access(apk, os.X_OK):
            die(f"APK v3 CLI is not executable: {apk}")
        key = regular_file(Path(args.signing_key), "signing key")
        key_bytes = key.read_bytes()
        if b"PRIVATE KEY" in key_bytes or b"ENCRYPTED PRIVATE" in key_bytes:
            die("--signing-key must be the public APK key, never a private key")
        if not re.search(br"-----BEGIN PUBLIC KEY-----\s+.+?\s+-----END PUBLIC KEY-----", key_bytes, flags=re.DOTALL):
            die("--signing-key must contain one public PEM key")
        key_hash = hashlib.sha256(key_bytes).hexdigest()
        template = regular_file(Path(args.install_template), "installer template")
        output = require_new_output(Path(args.output))

        seen_filenames: set[str] = set()
        packages: list[dict] = []
        seen_names: set[str] = set()
        for package_arg in args.package:
            package = regular_file(Path(package_arg), "APK")
            filename = safe_filename(package)
            if filename in seen_filenames:
                die(f"duplicate published APK filename: {filename}")
            expected_name = package_name_from_filename(filename)
            info = apk_metadata(apk, package, key)
            validate_metadata(info, expected_name, args.architecture, args.kernel_package, args.app_version)
            if expected_name in seen_names:
                die(f"duplicate package name: {expected_name}")
            seen_filenames.add(filename)
            seen_names.add(expected_name)
            entry = {"name": expected_name, "filename": filename,
                     "sha256": sha256(package), "source": package}
            if expected_name == "autovpn-controller":
                version = info.get("version")
                if not isinstance(version, str) or not version or any(char.isspace() for char in version):
                    die("autovpn-controller has no safe metadata version")
                # A LuCI update approval is bound to this exact metadata
                # version, not a filename that could be misleading after a
                # release asset is republished.
                entry["version"] = version
            packages.append(entry)

        if "autovpn-controller" not in seen_names:
            die("release must contain autovpn-controller")
        awg_names = {"kmod-amneziawg", "amneziawg-tools"}
        if seen_names & awg_names and not awg_names <= seen_names:
            die("AmneziaWG is all-or-nothing: include both kmod-amneziawg and amneziawg-tools")

        try:
            output.mkdir(mode=0o755)
        except OSError as exc:
            die(f"cannot create new output directory {output}: {exc}")
        try:
            shutil.copyfile(key, output / "autovpn-signing.pem")
            (output / "autovpn-signing.pem").chmod(0o644)
            for entry in packages:
                shutil.copyfile(entry.pop("source"), output / entry["filename"])
                (output / entry["filename"]).chmod(0o644)
            target_name = args.target.replace("/", "-")
            manifest = {
                "schema_version": 1,
                "version": args.app_version,
                "release": args.release,
                "target": args.target,
                "architecture": args.architecture,
                "kernel_release": args.kernel_release,
                "kernel_package": args.kernel_package,
                "min_free_kib": args.min_free_kib,
                "min_tmp_kib": args.min_tmp_kib,
                "signing_key": "autovpn-signing.pem",
                "signing_key_sha256": key_hash,
                "packages": packages,
                "capabilities": {"amneziawg": awg_names <= seen_names},
            }
            manifest_name = f"manifest-{args.release}-{target_name}-{args.architecture}.json"
            manifest_path = output / manifest_name
            manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            replace_template(template, output / "install.sh", release_base, key_hash, sha256(manifest_path))
            repair_template = Path(__file__).resolve().with_name("repair-bootstrap.sh")
            replace_template(regular_file(repair_template, "repair template"), output / "repair-bootstrap.sh", release_base, key_hash, sha256(manifest_path))
        except Exception:
            shutil.rmtree(output, ignore_errors=True)
            raise
        return 0
    except ReleaseError as exc:
        print(f"prepare-release: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
