#!/system/bin/sh
# SPDX-License-Identifier: GPL-2.0-only
# Load the susfs_guard_lkm .ko that matches the running GKI kernel, once, at boot.
MODDIR=${0%/*}
[ -f "$MODDIR/disable" ] && exit 0
[ -f "$MODDIR/remove" ] && exit 0
[ "$(getprop persist.sys.safemode)" != 1 ] || exit 0
[ "$(getprop ro.sys.safemode)" != 1 ] || exit 0
umask 077
# A competing invocation must not truncate the owner's diagnostics.
exec >>"$MODDIR/load.log" 2>&1
echo "susfs_guard_lkm loader: $(date)"
case "$MODDIR" in
  /data/adb/modules_update/*) echo "refusing to load a staged update"; exit 1 ;;
esac
if [ -e /data/adb/modules_update/susfs_guard_lkm ] || [ -e "$MODDIR/update" ]; then
  echo "SUSFS update pending; leave activation to a normal reboot"
  exit 1
fi
REL=$(cat /proc/sys/kernel/osrelease) || { echo "cannot read kernel release"; exit 1; }
. "$MODDIR/kmi.sh"
KMI=$(select_kmi "$REL" "$MODDIR") || { echo "no packaged .ko for kernel: $REL"; exit 1; }
KO="$MODDIR/lib/susfs_guard_lkm-$KMI.ko"
[ -s "$KO" ] || { echo "missing .ko for $KMI"; exit 1; }
echo "kernel $REL -> $KMI"
"$MODDIR/bin/susfs_insmod" "$KO" expose_proc=1
rc=$?
if [ "$rc" = 0 ]; then
  # The hide_mounts prefix list is runtime state and this module may load before
  # Vector's dex2oat64 overlay. Arm the non-su hide and add the prefix once the
  # module is live; the attach_recursive_mnt kretprobe records mounts that appear
  # later and matches them by filesystem identity, so boot order is covered.
  if [ -x /data/adb/ksu/bin/ksu_susfs ]; then
    /data/adb/ksu/bin/ksu_susfs hide_sus_mnts_for_non_su_procs 1
  fi
  if [ -w /proc/susfs_hide_mounts ]; then
    printf 'add /apex/com.android.art/bin/dex2oat64\n' > /proc/susfs_hide_mounts
  fi
  echo "loader completed; see above for loaded/skipped status."
else
  echo "susfs_insmod failed: $rc"
fi
exit "$rc"
