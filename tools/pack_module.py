#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Package an experimental KernelSU installer; never execute its payloads.

Prefer --build-artifacts for pinned-build receipts and corresponding source.
Legacy --ko-dir/--insmod inputs have ELF validation, not pinned-build provenance.
"""
import argparse
import datetime
import hashlib
import json
from pathlib import Path
import re
import struct
import tarfile
import zipfile

ROOT = Path(__file__).resolve().parents[1]


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def checked_build(directory):
    """Check a pinned build's complete receipt and unchanged runtime source."""
    entries = {}
    if (directory / "SHA256SUMS").is_symlink():
        raise SystemExit("build checksum receipt must not be a symlink")
    for line in (directory / "SHA256SUMS").read_text().splitlines():
        match = re.fullmatch(r"([0-9a-f]{64})  (?:\./)?([A-Za-z0-9_.-]+)", line)
        if not match or match[2] in entries or match[2] == "SHA256SUMS":
            raise SystemExit("invalid or duplicate build checksum entry")
        entries[match[2]] = match[1]
    if set(entries) != {p.name for p in directory.iterdir()} - {"SHA256SUMS"}:
        raise SystemExit("build checksum coverage differs from artifact directory")
    for name, digest in entries.items():
        path = directory / name
        if path.is_symlink() or not path.is_file() or sha256(path) != digest:
            raise SystemExit(f"build checksum mismatch or non-regular file: {name}")
    if not {"source.tar", "provenance.txt", "susfs_insmod"} <= entries.keys():
        raise SystemExit("missing build source, provenance or loader")
    provenance = {}
    for line in (directory / "provenance.txt").read_text().splitlines():
        key, separator, value = line.partition("=")
        if not separator or key in provenance:
            raise SystemExit("invalid or duplicate build provenance entry")
        provenance[key] = value
    pin = re.search(r"^DDK_IMAGE=(.+)$", (ROOT / "config/build.env").read_text(), re.M)[1]
    if (provenance.get("ddk_image") != pin
            or provenance.get("kmi") != "android16-6.12"
            or provenance.get("reproduction") != "two-clean-containers-byte-identical"
            or provenance.get("device_validation") != "not-performed"):
        raise SystemExit("not an accepted pinned experimental build")
    for key in ("source_commit", "source_tree"):
        if not re.fullmatch(r"[0-9a-f]{40}", provenance.get(key, "")):
            raise SystemExit(f"invalid immutable {key}")
    if not re.fullmatch(r"[0-9]+", provenance.get("source_date_epoch", "")):
        raise SystemExit("invalid build epoch")
    expected = {p.relative_to(ROOT).as_posix(): p for p in (ROOT / "kernel").rglob("*") if p.is_file()}
    expected.update({p.relative_to(ROOT).as_posix(): p for p in (ROOT / "module").glob("*.sh")})
    expected["tools/susfs_insmod.c"] = ROOT / "tools/susfs_insmod.c"
    with tarfile.open(directory / "source.tar", "r:") as source:
        members = source.getmembers()
        if len({m.name for m in members}) != len(members):
            raise SystemExit("duplicate source archive entries")
        if any(m.name.startswith("/") or ".." in m.name.split("/") or "\\" in m.name
               or not (m.isfile() or m.isdir()) for m in members):
            raise SystemExit("unsafe source archive entry")
        actual = {m.name for m in members if m.name.startswith("kernel/") and m.isfile()}
        if actual != {name for name in expected if name.startswith("kernel/")}:
            raise SystemExit("kernel source file set differs from the build")
        for name, path in expected.items():
            try:
                member = source.getmember(name)
            except KeyError:
                raise SystemExit(f"missing build source: {name}")
            if not member.isfile() or source.extractfile(member).read() != path.read_bytes():
                raise SystemExit(f"runtime source differs from the build: {name}")
    return provenance


def check_elf(path, elf_type):
    data = path.read_bytes()
    if (path.is_symlink() or len(data) < 64 or data[:7] != b"\x7fELF\x02\x01\x01"
            or struct.unpack_from("<HH", data, 16) != (elf_type, 183)):
        raise SystemExit(f"not the expected ELF64 little-endian AArch64 payload: {path.name}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--build-artifacts", type=Path, help="verified pinned build directory; includes its source and provenance")
    ap.add_argument("--ko-dir", help="legacy directory holding susfs_guard_lkm-*.ko")
    ap.add_argument("--insmod", help="legacy path to the susfs_insmod binary")
    ap.add_argument("--epoch", type=int, help="ZIP timestamp/versionCode; defaults to build epoch or current UTC time")
    ap.add_argument("--tag", required=True, help="release tag, used as the module version")
    ap.add_argument("--out", required=True, help="output ZIP path")
    args = ap.parse_args()

    if args.build_artifacts:
        if args.ko_dir or args.insmod:
            ap.error("--build-artifacts cannot be combined with --ko-dir or --insmod")
        build = checked_build(args.build_artifacts)
        ko_dir = args.build_artifacts
        insmod = ko_dir / "susfs_insmod"
    else:
        if not args.ko_dir or not args.insmod:
            ap.error("choose --build-artifacts or both --ko-dir and --insmod")
        build = None
        ko_dir = Path(args.ko_dir)
        insmod = Path(args.insmod)
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._+-]*", args.tag):
        ap.error("tag must be a single version token; it is not normalized")
    kos = sorted(ko_dir.glob("susfs_guard_lkm-*.ko"))
    if not kos:
        raise SystemExit(f"no susfs_guard_lkm-*.ko in {ko_dir}")
    if not insmod.is_file():
        raise SystemExit(f"loader not found: {insmod}")

    for path in kos:
        if not re.fullmatch(r"susfs_guard_lkm-android[0-9]+-[0-9]+\.[0-9]+\.ko", path.name):
            ap.error(f"invalid KMI filename: {path.name}")
        check_elf(path, 1)
    check_elf(insmod, 3)
    kmis = [re.sub(r"^susfs_guard_lkm-|\.ko$", "", p.name) for p in kos]
    if build and kmis != [build["kmi"]]:
        ap.error("packaged KMIs differ from the pinned build")
    version_code = args.epoch
    if version_code is None:
        version_code = int(build["source_date_epoch"]) if build else int(datetime.datetime.now(datetime.timezone.utc).timestamp())
    if not 315532800 <= version_code <= 2147483647:
        ap.error("epoch must be between 1980 and the signed 32-bit versionCode limit")
    timestamp = datetime.datetime.fromtimestamp(version_code, datetime.timezone.utc).timetuple()[:6]
    low = min(kmis, key=lambda k: tuple(int(n) for n in k.split("-", 1)[1].split("."))).split("-", 1)[1]
    high = max(kmis, key=lambda k: tuple(int(n) for n in k.split("-", 1)[1].split("."))).split("-", 1)[1]

    module_prop = (
        "id=susfs_guard_lkm\n"
        "name=SUSFS Guard (LKM)\n"
        f"version={args.tag}\n"
        f"versionCode={version_code}\n"
        "author=inforcqb\n"
        f"description=Loads the matching susfs_guard_lkm .ko for the device kernel on boot "
        f"across {len(kmis)} arm64 KMIs ({low}-{high}). Loader only, no rules. "
        "KernelSU-compatible. Reboot required.\n"
    )
    provenance = {
        "repository": "https://github.com/doccstat/susfs4ksu-lkm" if build else "https://github.com/inforcqb/susfs4ksu-lkm",
        "build": build,
        "packager_sha256": sha256(Path(__file__)),
        "device_validation": "not-performed",
        "tag": args.tag,
        "architecture": "arm64",
        "supported_kmis": kmis,
        "assets_sha256": {p.name: sha256(p) for p in kos} | {insmod.name: sha256(insmod)},
    }

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    contents = {name: ((ROOT / "module" / name).read_bytes(), 0o755)
                for name in ("post-fs-data.sh", "customize.sh", "kmi.sh", "early-dispatch.sh", "load.sh", "uninstall.sh")}
    contents.update({"kmis": ("".join(k + "\n" for k in kmis).encode(), 0o644),
                     "module.prop": (module_prop.encode(), 0o644),
                     "bin/susfs_insmod": (insmod.read_bytes(), 0o755),
                     "LICENSE": ((ROOT / "LICENSE").read_bytes(), 0o644),
                     "provenance.json": ((json.dumps(provenance, indent=2) + "\n").encode(), 0o644)})
    contents.update({"lib/" + p.name: (p.read_bytes(), 0o644) for p in kos})
    if build:
        contents["source.tar"] = ((ko_dir / "source.tar").read_bytes(), 0o644)
        contents["packaging/pack_module.py"] = (Path(__file__).read_bytes(), 0o644)
        contents["BUILDING.md"] = ((ROOT / "BUILDING.md").read_bytes(), 0o644)
    checksums = "".join(f"{hashlib.sha256(data).hexdigest()}  {name}\n"
                        for name, (data, _) in sorted(contents.items()))
    contents["SHA256SUMS"] = (checksums.encode(), 0o644)
    # Never overwrite a user's earlier artifact; ZIP metadata is input-independent.
    with zipfile.ZipFile(out, "x", compression=zipfile.ZIP_DEFLATED) as z:
        for name, (data, mode) in sorted(contents.items()):
            info = zipfile.ZipInfo(name, timestamp)
            info.create_system = 3
            info.external_attr = (0o100000 | mode) << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            z.writestr(info, data)
    print(out)
    print("sha256:", sha256(out))


if __name__ == "__main__":
    main()
