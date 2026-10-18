# Pinned experimental LKM builds

This fork starts at upstream `9f6ddfccb04517b6f798807ae05dc7a81ce545fa`,
including the post-r18.3 O_PATH descriptor-race fix. Kernel and loader sources
are unchanged by the initial fork import. Building from a reviewed commit does
not require waiting for an upstream release.

## GitHub Actions

**Build pinned SUSFS LKM** runs on relevant `main` pushes, pull requests and
manual dispatch. It builds only **android16-6.12**. The existing upstream
multi-KMI, release-ZIP and CodeQL jobs are retained but restricted to the
upstream repository; they do not run in this fork.

The workflow follows the pinned-source/metadata/artifact pattern used by
[gki-kernelsu](https://github.com/doccstat/gki-kernelsu), but does not reuse its
full-kernel build: this produces a standalone `.ko`, **not** `Image` or
`boot.img`. The prepared DDK is 6.12.76-4k, not an exact vendor-kernel rebuild.

[`config/build.env`](config/build.env) pins the linux/amd64 DDK image by digest,
including prepared kernel headers and Clang r536225. Actions are commit-pinned;
checkout credentials are not retained. Two fresh, unprivileged containers build
the same committed source at the same internal path, with networking disabled
and fixed kernel build identity/timestamp. Neither container shares build caches
or outputs with the other. The workflow fails unless both `.ko` files and both
loaders are byte-identical. No accepted artifact directory is produced on a
failed build or comparison.

Artifacts contain:

- `susfs_guard_lkm-android16-6.12.ko` and `susfs_insmod`;
- exact source archive, commit/tree/image provenance and SHA-256 checksums;
- compiler, ELF and CFI reports, kernel config and `Module.symvers`;
- separate build logs, also retained on failure.

The upstream CFI call-site gate, architecture/vermagic/namespace checks and
forbidden-import checks must pass. Host tests use mock commands and never load
kernel code. [Run 39079657465](https://github.com/doccstat/susfs4ksu-lkm/actions/runs/39079657465)
passed the real two-build gate for `5a1bc82d6bd5e163a18e72fc4305f00bad5bddbc`.
Each later revision still needs its own successful run.

Two identical outputs establish reproduction for those fixed inputs, **not**
universal reproducibility across other kernels/toolchains, ABI compatibility,
complete SUSFS functionality or device safety. `SHA256SUMS` identifies the
uncompressed payloads; GitHub's artifact transport ZIP is not compared. The
workflow also packages a separate experimental KernelSU module ZIP after these
gates. No GitHub release, device installation or flashing is performed.

## Local reproduction

Use a clean standalone Git checkout on Linux x86-64 with Git, Docker, Bash,
Python 3, tar and coreutils:

```sh
bash tools/build-pinned.sh
```

Build trees and evidence stay under `${XDG_CACHE_HOME:-$HOME/.cache}/susfs4ksu-lkm/`;
the script prints the unique result directory. It builds only committed files,
rejects tracked changes and does not alter the source checkout. Docker access
must already be configured; the script does not install tools or invoke sudo.

Host checks, with disposable outputs under cache:

```sh
export TMPDIR="$HOME/.cache/susfs4ksu-lkm/tests"
export PYTHONDONTWRITEBYTECODE=1
mkdir -p "$TMPDIR"
python3 -m unittest discover -v -s tools -p 'test_build.py'
python3 tools/test_pack.py
cc -O1 -o "$TMPDIR/insmod_match_test" tools/insmod_match_test.c
"$TMPDIR/insmod_match_test"
```

## Installable module ZIP

Download the separate `susfs-module-android16-6.12-<commit>` artifact and extract
the **inner `*-module.zip`** before selecting it in a KernelSU-compatible manager.
The raw `susfs-android16-6.12-<commit>` artifact is still not an installer.

To package an already verified raw build without rebuilding it, use a source
checkout whose kernel, loader and module scripts match that build:

```sh
python3 tools/pack_module.py \
  --build-artifacts "$HOME/.cache/susfs4ksu-lkm/verified-artifacts" \
  --tag ci-5a1bc82d \
  --out "$HOME/.cache/susfs4ksu-lkm/susfs-android16-6.12-ci-5a1bc82d-module.zip"
```

The packer checks complete input checksums, pinned build provenance, runtime
source identity and ELF headers. It preserves the supplied tag, refuses existing
output files and packages only the recorded KMI. ZIP ordering, modes and timestamps
are deterministic; `versionCode` defaults to the source epoch (`--epoch` explicitly
overrides it). Checksums are integrity checks, **not signatures**: authenticate the
original build/download separately. The legacy `--ko-dir`/`--insmod` interface is
retained, but cannot claim pinned-build verification.

The ZIP includes exact binary source (`source.tar`), GPL license, the packaging
script, build provenance and payload checksums. To reproduce packaging, unpack
`source.tar` into a cache-only checkout and replace its `tools/pack_module.py` with
`packaging/pack_module.py`; use the included `BUILDING.md` and original raw build
artifacts. Identical ZIP bytes require the same Python/zlib implementation too.

**Installation stages files; it does not load the LKM.** Upstream loading behavior
is unchanged: module `post-fs-data` by default, optional common dispatcher only
with the documented `early_load` setting. No `early_load`, rules, `late-load.sh`,
service restart or installer-time loading is added. Existing `early_load` is
preserved by upstream installation. A late-loaded root provider may not execute
ordinary `post-fs-data`; this ZIP does not authorize manually replaying stages.

This is a packaging-validated **experimental** module, not a device-safety
certification. The existing proc/sysfs absence heuristic is not an authoritative
protocol/serialization guard, and the optional early dispatcher can select a
staged update. Do not enable that optional path for an unvalidated lifecycle.
Provider lifetime, feature acceptance and recovery remain requirements below.

## Before any device trial

A matching KMI and a successful build are insufficient: this code uses private
kernel structures and runtime hooks. Compare the built artifact with the exact
running kernel's layouts, prototypes, symbols and CFI configuration. Keep rules,
stage ordering, absence detection and a non-autoload recovery plan explicit.

The current source retains a KernelSU function pointer without pinning its
provider. **Do not unload or hot-replace KernelSU while SUSFS is loaded.** A
load-success message also does not prove that every optional feature initialized.
The retained upstream boot scripts and unload examples are not an approved
integration or recovery procedure for this experimental build. Device changes
require a separate, deliberate test window.
