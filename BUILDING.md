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
kernel code. The first real CI build is still required: configured gates are
not evidence that compilation or reproduction has already passed.

Two identical outputs establish reproduction for those fixed inputs, **not**
universal reproducibility across other kernels/toolchains, ABI compatibility,
complete SUSFS functionality or device safety. `SHA256SUMS` identifies the
uncompressed payloads; GitHub's artifact transport ZIP is not compared. No
installer ZIP or GitHub release is generated, and nothing is installed or flashed.

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
