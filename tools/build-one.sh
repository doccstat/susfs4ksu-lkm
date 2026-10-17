#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-2.0-only
# Run by build-pinned.sh inside the pinned, offline DDK container.
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."
source config/build.env
: "${KDIR:?The prepared DDK kernel tree is required}"
: "${SOURCE_DATE_EPOCH:?The source commit timestamp is required}"
[[ -f $KDIR/.config && -f $KDIR/Module.symvers ]] || {
  echo 'Missing DDK configuration or symbol-version manifest.' >&2; exit 1;
}
mkdir -p out "${TMPDIR:?}"
export KBUILD_BUILD_TIMESTAMP="$(date -u -d "@$SOURCE_DATE_EPOCH" '+%a %b %d %H:%M:%S UTC %Y')"
export KBUILD_BUILD_USER=builder KBUILD_BUILD_HOST=builder KBUILD_BUILD_VERSION=1
clang --version > out/compiler.txt
make -C "$KDIR" M="$PWD/kernel" ARCH=arm64 CC=clang LLVM=1 LLVM_IAS=1 modules -j2
ko=kernel/susfs_guard_lkm.ko
readelf -h -n -SW -p .modinfo "$ko" > out/module-info.txt
grep -Eq 'Machine:.*AArch64' out/module-info.txt
grep -Eq 'Type:.*REL ' out/module-info.txt
grep -F "vermagic=$DDK_KERNEL_RELEASE " out/module-info.txt
grep -F 'import_ns=VFS_internal_I_am_really_a_filesystem_and_am_NOT_a_driver' out/module-info.txt
nm -u "$ko" > out/undefined-symbols.txt
[[ -s out/undefined-symbols.txt ]]
if grep -Eq ' (kallsyms_lookup_name|kallsyms_lookup|kallsyms_lookup_size_offset|kallsyms_on_each_symbol|kallsyms_on_each_match_symbol|__stack_chk_guard)$' out/undefined-symbols.txt; then
  echo 'Forbidden link-time symbol dependency; refusing artifact.' >&2; exit 1
fi
# Only these upstream-reviewed sites call module-local functions, not kernel pointers.
python3 tools/cfi_sites.py \
  --allow init_module,susfs_layers_down,susfs_tw_func,susfs_watch_freeing \
  "$ko" | tee out/cfi-sites.txt
clang --target=aarch64-linux-gnu -O2 -nostdlib -static-pie \
  -fno-stack-protector -fno-builtin -fuse-ld=lld -Wl,-e,_start \
  -o out/susfs_insmod tools/susfs_insmod.c
readelf -h -n out/susfs_insmod > out/loader-info.txt
grep -Eq 'Machine:.*AArch64' out/loader-info.txt
cp "$ko" "out/susfs_guard_lkm-$KMI.ko"
cp "$KDIR/.config" out/kernel.config
cp "$KDIR/Module.symvers" out/Module.symvers
printf '%s\n' 'Build/CFI checks only; private ABI, symbol availability and device safety remain unverified.' > out/LIMITATIONS.txt
