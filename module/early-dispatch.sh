#!/system/bin/sh
INSTALLED=/data/adb/modules/susfs_guard_lkm
[ "$(getprop persist.sys.safemode)" != 1 ] || exit 0
[ "$(getprop ro.sys.safemode)" != 1 ] || exit 0
MODDIR=$INSTALLED
[ -d "$MODDIR" ] || exit 0
[ ! -f "$MODDIR/disable" ] && [ ! -f "$MODDIR/remove" ] || exit 0
[ -f "$MODDIR/early_load" ] || exit 0
exec /system/bin/sh "$MODDIR/load.sh"
