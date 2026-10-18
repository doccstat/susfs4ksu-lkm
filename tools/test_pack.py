# SPDX-License-Identifier: GPL-2.0-only
"""Host-only packaging/installer tests; fake ELF payloads are never executed."""
import hashlib
import io
import json
import os
from pathlib import Path
import struct
import subprocess
import sys
import tarfile
import tempfile
import unittest
import zipfile

ROOT = Path(__file__).resolve().parents[1]


def elf(kind):
    data = bytearray(64)
    data[:7] = b"\x7fELF\x02\x01\x01"
    struct.pack_into("<HHI", data, 16, kind, 183, 1)
    struct.pack_into("<HHHHHH", data, 52, 64, 56, 0, 64, 0, 0)
    return data


def relocated_loader():
    data = elf(3)
    struct.pack_into("<Q", data, 40, 64)  # section table
    struct.pack_into("<H", data, 60, 4)
    data.extend(bytes(64))  # null section
    for kind, offset, size, link, info, entry in ((4, 320, 24, 2, 0, 24),
                                                (11, 344, 24, 3, 1, 24),
                                                (3, 368, 1, 0, 0, 0)):
        data.extend(struct.pack("<IIQQQQIIQQ", 0, kind, 2, 0, offset, size, link, info, 8, entry))
    data.extend(struct.pack("<QQq", 0, 1027, 0))  # R_AARCH64_RELATIVE
    data.extend(bytes(25))  # null dynamic symbol and string
    return data


class PackagingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.work = Path(self.tmp.name)
        self.assets = self.work / "assets"
        self.assets.mkdir()
        self.ko = self.assets / "susfs_guard_lkm-android16-6.12.ko"
        self.ko.write_bytes(elf(1))
        (self.assets / "susfs_insmod").write_bytes(elf(3))
        self.out = self.work / "module.zip"

    def pack(self, *args):
        return subprocess.run([sys.executable, str(ROOT / "tools/pack_module.py"),
                               "--tag", "ci-test", "--out", str(self.out), *args],
                              text=True, capture_output=True)

    def legacy(self, *args):
        return self.pack("--ko-dir", str(self.assets), "--insmod",
                         str(self.assets / "susfs_insmod"), "--epoch", "1792280135", *args)

    def receipt(self):
        paths = sorted(p for p in self.assets.iterdir() if p.name != "SHA256SUMS")
        (self.assets / "SHA256SUMS").write_text("".join(
            f"{hashlib.sha256(p.read_bytes()).hexdigest()}  ./{p.name}\n" for p in paths))

    def pinned(self, altered_source=None):
        pin = next(line.split("=", 1)[1] for line in (ROOT / "config/build.env").read_text().splitlines()
                   if line.startswith("DDK_IMAGE="))
        (self.assets / "provenance.txt").write_text(
            f"source_commit={'a' * 40}\nsource_tree={'b' * 40}\nddk_image={pin}\n"
            "kmi=android16-6.12\nsource_date_epoch=1792280135\n"
            "reproduction=two-clean-containers-byte-identical\ndevice_validation=not-performed\n")
        paths = sorted(p for p in (ROOT / "kernel").rglob("*") if p.is_file())
        paths += sorted((ROOT / "module").glob("*.sh")) + [ROOT / "tools/susfs_insmod.c"]
        with tarfile.open(self.assets / "source.tar", "w") as archive:
            for path in paths:
                name = path.relative_to(ROOT).as_posix()
                data = b"wrong source" if name == altered_source else path.read_bytes()
                info = tarfile.TarInfo(name)
                info.size = len(data)
                archive.addfile(info, io.BytesIO(data))
        self.receipt()

    def test_legacy_layout_modes_checksums_and_determinism(self):
        kmis = ["android12-5.10", "android15-6.6", "android16-6.12", "android17-6.18"]
        for kmi in kmis:
            (self.assets / f"susfs_guard_lkm-{kmi}.ko").write_bytes(elf(1))
        first = self.legacy()
        self.assertEqual(first.returncode, 0, first.stderr)
        original = self.out.read_bytes()
        with zipfile.ZipFile(self.out) as z:
            names = z.namelist()
            self.assertEqual(names, sorted(set(names)))
            for absent in ("early_load", "disable", "late-load.sh", "service.sh"):
                self.assertNotIn(absent, names)
            self.assertEqual(z.read("kmis").decode().splitlines(), kmis)
            required = {"post-fs-data.sh", "customize.sh", "kmi.sh", "early-dispatch.sh", "load.sh", "uninstall.sh",
                        "kmis", "module.prop", "bin/susfs_insmod", "provenance.json", "LICENSE", "SHA256SUMS"}
            required.update(f"lib/susfs_guard_lkm-{kmi}.ko" for kmi in kmis)
            self.assertEqual(set(names), required)
            for field in (b"id=susfs_guard_lkm\n", b"version=ci-test\n", b"versionCode=1792280135\n"):
                self.assertIn(field, z.read("module.prop"))
            for line in z.read("SHA256SUMS").decode().splitlines():
                digest, name = line.split("  ")
                self.assertEqual(hashlib.sha256(z.read(name)).hexdigest(), digest)
            self.assertEqual(len(z.read("SHA256SUMS").splitlines()), len(names) - 1)
            self.assertEqual(z.getinfo("bin/susfs_insmod").external_attr >> 16, 0o100755)
            self.assertEqual(z.getinfo("module.prop").external_attr >> 16, 0o100644)
        self.out = self.work / "second.zip"
        os.utime(self.ko, (100000, 100000))
        self.assertEqual(self.legacy().returncode, 0)
        self.assertEqual(self.out.read_bytes(), original)

    def test_pinned_build_includes_source_and_exact_provenance(self):
        self.pinned()
        result = self.pack("--build-artifacts", str(self.assets))
        self.assertEqual(result.returncode, 0, result.stderr)
        with zipfile.ZipFile(self.out) as z:
            p = json.loads(z.read("provenance.json"))
            self.assertEqual(p["build"]["source_commit"], "a" * 40)
            self.assertEqual(p["device_validation"], "not-performed")
            self.assertEqual(p["repository"], "https://github.com/doccstat/susfs4ksu-lkm")
            self.assertEqual(z.read("source.tar"), (self.assets / "source.tar").read_bytes())
            self.assertEqual(z.read("packaging/pack_module.py"), (ROOT / "tools/pack_module.py").read_bytes())

    def test_corrupt_unlisted_and_non_regular_assets_refused(self):
        for fault in ("corrupt", "unlisted", "symlink", "duplicate", "traversal"):
            with self.subTest(fault=fault):
                self.pinned()
                sums = self.assets / "SHA256SUMS"
                if fault == "corrupt":
                    self.ko.write_bytes(b"corrupt")
                elif fault == "unlisted":
                    (self.assets / "unexpected").write_text("not covered")
                elif fault == "symlink":
                    self.ko.unlink()
                    target = self.work / "external"
                    target.write_bytes(elf(1))
                    self.ko.symlink_to(target)
                else:
                    with sums.open("a") as f:
                        f.write(sums.read_text().splitlines()[0].replace("  ./", "  ") + "\n" if fault == "duplicate"
                                else "0" * 64 + "  ../outside\n")
                self.assertNotEqual(self.pack("--build-artifacts", str(self.assets)).returncode, 0)
                self.assertFalse(self.out.exists())
                self.ko.unlink()
                self.ko.write_bytes(elf(1))
                (self.assets / "unexpected").unlink(missing_ok=True)

    def test_wrong_runtime_source_and_unaccepted_build_refused(self):
        for source in ("kernel/susfs_main.c", "tools/susfs_insmod.c", "module/load.sh"):
            with self.subTest(source=source):
                self.pinned(altered_source=source)
                result = self.pack("--build-artifacts", str(self.assets))
                self.assertIn("runtime source differs", result.stderr)
                self.assertFalse(self.out.exists())
        self.pinned()
        prov = self.assets / "provenance.txt"
        prov.write_text(prov.read_text().replace("two-clean-containers-byte-identical", "unchecked"))
        self.receipt()
        self.assertIn("not an accepted", self.pack("--build-artifacts", str(self.assets)).stderr)

    def test_source_archive_rejects_traversal_links_and_duplicate_names(self):
        for kind in ("traversal", "symlink", "duplicate"):
            with self.subTest(kind=kind):
                self.pinned()
                with tarfile.open(self.assets / "source.tar", "a") as archive:
                    name = "../outside" if kind == "traversal" else "kernel/susfs_main.c"
                    entry = tarfile.TarInfo(name)
                    if kind == "symlink":
                        entry.name = "link"
                        entry.type = tarfile.SYMTYPE
                        entry.linkname = "outside"
                    archive.addfile(entry)
                self.receipt()
                result = self.pack("--build-artifacts", str(self.assets))
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(self.out.exists())

    def test_invalid_input_and_overwrite_refused(self):
        self.assertNotEqual(self.legacy("--tag", "bad\nid=other").returncode, 0)
        self.assertNotEqual(self.legacy("--epoch", "0").returncode, 0)
        self.ko.write_bytes(b"not ELF")
        self.assertNotEqual(self.legacy().returncode, 0)
        self.ko.write_bytes(elf(1))
        self.out.write_bytes(b"keep this")
        self.assertNotEqual(self.legacy().returncode, 0)
        self.assertEqual(self.out.read_bytes(), b"keep this")

    def test_loader_runtime_relocations_and_interpreter_refused(self):
        interpreter = elf(3)
        struct.pack_into("<Q", interpreter, 32, 64)
        struct.pack_into("<H", interpreter, 56, 1)
        interpreter.extend(struct.pack("<IIQQQQQQ", 3, 4, 120, 0, 0, 7, 7, 1))
        interpreter.extend(b"/ld.so\0")
        for data in (relocated_loader(), interpreter):
            with self.subTest(kind="relocation" if data != interpreter else "interpreter"):
                (self.assets / "susfs_insmod").write_bytes(data)
                result = self.legacy()
                self.assertIn("loader needs runtime relocations or an interpreter", result.stderr)
                self.assertFalse(self.out.exists())
        # Kernel modules legitimately carry relocations; only the loader forbids them.
        module = relocated_loader()
        struct.pack_into("<H", module, 16, 1)
        self.ko.write_bytes(module)
        (self.assets / "susfs_insmod").write_bytes(elf(3))
        self.assertEqual(self.legacy().returncode, 0)

    def test_installer_stages_without_running_loader(self):
        module = self.work / "module"
        module.mkdir()
        for p in (ROOT / "module").glob("*.sh"):
            (module / p.name).write_bytes(p.read_bytes())
        (module / "lib").mkdir()
        (module / "lib" / self.ko.name).write_bytes(self.ko.read_bytes())
        (module / "bin").mkdir()
        (module / "bin/susfs_insmod").write_text(f"#!/bin/sh\ntouch '{self.work}/loader-ran'\nexit 99\n")
        (module / "bin/susfs_insmod").chmod(0o755)
        (module / "kmis").write_text("android16-6.12\n")
        kernel = self.work / "osrelease"
        kernel.write_text("6.12.69-android16-6-example-4k\n")
        script = (module / "customize.sh").read_text().replace("/data/adb", str(self.work / "adb"))
        script = script.replace("/proc/sys/kernel/osrelease", str(kernel))
        harness = 'abort() { echo "$*" >&2; exit 1; }; ui_print() { echo "$*"; }; set_perm() { :; }; set_perm_recursive() { :; };\n'
        for ksu, arch, expected in (("false", "arm64", 1), ("true", "x86_64", 1), ("true", "arm64", 0)):
            result = subprocess.run(["sh", "-c", harness + script], env=os.environ | {
                "KSU": ksu, "ARCH": arch, "MODPATH": str(module)}, capture_output=True, text=True)
            self.assertEqual(result.returncode, expected, result.stderr)
        self.assertTrue((module / "skip_mount").is_file())
        self.assertFalse((module / "load.log").exists())
        self.assertFalse((self.work / "loader-ran").exists())
        self.assertFalse((module / "early_load").exists())
        hook = self.work / "adb/post-fs-data.d/00-susfs-guard-lkm.sh"
        self.assertEqual(hook.read_bytes(), (ROOT / "module/early-dispatch.sh").read_bytes())
        hook.write_text("another owner's hook\n")
        result = subprocess.run(["sh", "-c", harness + script], env=os.environ | {
            "KSU": "true", "ARCH": "arm64", "MODPATH": str(module)}, capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(hook.read_text(), "another owner's hook\n")


if __name__ == "__main__":
    unittest.main()
