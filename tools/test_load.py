# SPDX-License-Identifier: GPL-2.0-only
"""Exercise the real stage scripts in a cache sandbox with a non-loading spy."""
import os
from pathlib import Path
import re
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class LoadTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.work = Path(self.tmp.name)
        self.adb = self.work / 'adb'
        self.module = self.adb / 'modules/susfs_guard_lkm'
        self.module.mkdir(parents=True)
        self.staged = self.adb / 'modules_update/susfs_guard_lkm'
        kernel = self.work / 'osrelease'
        kernel.write_text('6.12.69-android16-6-example-4k\n')
        for path in (ROOT / 'module').glob('*.sh'):
            text = path.read_text().replace('/data/adb', str(self.adb))
            text = text.replace('/proc/sys/kernel/osrelease', str(kernel))
            text = text.replace('/system/bin/sh', '/bin/sh')
            (self.module / path.name).write_text(text)
        (self.module / 'kmis').write_text('android16-6.12\n')
        (self.module / 'lib').mkdir()
        (self.module / 'lib/susfs_guard_lkm-android16-6.12.ko').write_text('not executable')
        binaries = self.module / 'bin'
        binaries.mkdir()
        loader = binaries / 'susfs_insmod'
        loader.write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> "$TRACE"\nexit "${LOADER_STATUS:-0}"\n')
        loader.chmod(0o755)
        getprop = binaries / 'getprop'
        getprop.write_text('#!/bin/sh\n[ "$1" != "${SAFE_PROPERTY:-}" ] || echo 1\n')
        getprop.chmod(0o755)
        self.trace = self.work / 'calls'
        self.env = os.environ | {'PATH': str(binaries) + os.pathsep + os.environ['PATH'],
                                 'TRACE': str(self.trace)}

    def stage(self, name, **env):
        return subprocess.run(['sh', str(self.module / name)], env=self.env | env,
                              text=True, capture_output=True, timeout=10)

    def test_repeated_stages_all_use_the_native_guard(self):
        (self.module / 'early_load').touch()
        for script in ('early-dispatch.sh', 'post-fs-data.sh', 'post-fs-data.sh'):
            result = self.stage(script)
            self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.trace.read_text().splitlines()
        self.assertEqual(len(calls), 3)
        self.assertTrue(all(line.endswith('.ko expose_proc=1') for line in calls))
        log = (self.module / 'load.log').read_text()
        self.assertEqual(log.count('susfs_guard_lkm loader:'), 3)
        self.assertNotIn('\nloaded.\n', log)

    def test_disabled_removed_and_safe_mode_never_invoke_loader(self):
        (self.module / 'early_load').touch()
        for flag in ('disable', 'remove'):
            (self.module / flag).touch()
            for script in ('early-dispatch.sh', 'post-fs-data.sh', 'load.sh'):
                self.assertEqual(self.stage(script).returncode, 0)
            (self.module / flag).unlink()
        for prop in ('persist.sys.safemode', 'ro.sys.safemode'):
            for script in ('early-dispatch.sh', 'post-fs-data.sh', 'load.sh'):
                self.assertEqual(self.stage(script, SAFE_PROPERTY=prop).returncode, 0)
        self.assertFalse(self.trace.exists())

    def test_pending_update_is_not_loaded_by_either_stage_or_directly(self):
        self.staged.mkdir(parents=True)
        (self.module / 'early_load').touch()
        for script in ('early-dispatch.sh', 'post-fs-data.sh', 'load.sh'):
            self.assertNotEqual(self.stage(script).returncode, 0)
        (self.staged / 'load.sh').write_bytes((self.module / 'load.sh').read_bytes())
        result = subprocess.run(['sh', str(self.staged / 'load.sh')], env=self.env,
                                capture_output=True, timeout=10)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('refusing to load a staged update', (self.staged / 'load.log').read_text())
        self.assertFalse(self.trace.exists())

    def test_update_marker_alone_refuses_loading(self):
        (self.module / 'update').touch()
        self.assertNotEqual(self.stage('post-fs-data.sh').returncode, 0)
        self.assertFalse(self.trace.exists())

    def test_early_loading_remains_opt_in_and_never_selects_staging(self):
        self.staged.mkdir(parents=True)
        (self.staged / 'early_load').touch()
        (self.staged / 'load.sh').write_text('exit 99\n')
        self.assertEqual(self.stage('early-dispatch.sh').returncode, 0)
        self.assertFalse(self.trace.exists())

    def test_loader_failure_is_visible_without_a_retry(self):
        self.assertEqual(self.stage('post-fs-data.sh', LOADER_STATUS='13').returncode, 13)
        self.assertEqual(len(self.trace.read_text().splitlines()), 1)
        self.assertIn('susfs_insmod failed: 13', (self.module / 'load.log').read_text())

    def test_native_guard_order_and_no_lifecycle_commands(self):
        source = (ROOT / 'tools/susfs_insmod.c').read_text()
        source = re.sub(r'/\*.*?\*/', '', source, flags=re.S)
        main = source.split('void insmod_main(long argc, char **argv)\n{', 1)[1]
        self.assertLess(main.index('guard_load();'), main.index('sys6(SYS_openat'))
        self.assertLess(main.index('guard_load();'), main.index('kptr_relax();'))
        self.assertGreater(main.index('if (susfs_present() != 1)'), main.rindex('sys6(SYS_init_module'))
        self.assertLess(main.index('if (susfs_present() != 1)'), main.index('finish(0);'))
        for path in (ROOT / 'module').glob('*.sh'):
            commands = '\n'.join(line for line in path.read_text().splitlines() if not line.lstrip().startswith('#'))
            self.assertNotRegex(commands, re.compile(r'^\s*(?:stop|start|reboot|rmmod|killall)\b', re.M), path.name)


if __name__ == '__main__':
    unittest.main()
