#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Unsigned host fixtures and command spies; never use Docker or load a module."""
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
IMAGE = "ghcr.io/ylarod/ddk-min@sha256:fd3ec5cc71d7f2716212e9df8d5674a9afe68fb769282fcac87b83f207f949ee"
KMI = "android16-6.12"

DOCKER = r'''
import json, os, pathlib, sys
args = sys.argv[1:]
with open(os.environ['CALLS'], 'a') as f:
    f.write(json.dumps(args) + '\n')
if args[0] == 'pull':
    assert args[1:] == ['--platform', 'linux/amd64', os.environ['IMAGE']]
    sys.exit(0)
assert args[0] == 'run'
for flag in ('--rm', '--cap-drop=ALL', '--security-opt=no-new-privileges'):
    assert flag in args
assert args[args.index('--network') + 1] == 'none'
assert args[args.index('--platform') + 1] == 'linux/amd64'
assert args[args.index('--workdir') + 1] == '/build'
assert args[args.index('--user') + 1] == f'{os.getuid()}:{os.getgid()}'
assert 'SOURCE_DATE_EPOCH=1792195200' in args
assert '--privileged' not in args
assert args[-3:] == [os.environ['IMAGE'], 'bash', 'tools/build-one.sh']
mount = args[args.index('--mount') + 1]
assert mount.startswith('type=bind,source=') and mount.endswith(',target=/build')
work = pathlib.Path(mount[len('type=bind,source='):-len(',target=/build')])
assert not (work / 'out').exists()
assert not (work / '.git').exists()
assert not (work / 'untracked').exists()
assert (work / 'tools/build-one.sh').is_file()
if work.name == os.environ.get('FAIL_PASS'):
    print('simulated compiler failure')
    sys.exit(42)
out = work / 'out'; out.mkdir()
for name in ('susfs_guard_lkm-android16-6.12.ko', 'susfs_insmod'):
    data = b'deterministic test bytes\n'
    if work.name == '2' and os.environ.get('MISMATCH') == name:
        data += b'different'
    (out / name).write_bytes(data)
(out / 'compiler.txt').write_text('mock compiler\n')
'''

TOOLS = r'''
import os, pathlib, struct, sys
name = pathlib.Path(sys.argv[0]).name
args = sys.argv[1:]
mode = os.environ.get('FAULT', '')
if mode == name:
    print('simulated ' + name + ' failure')
    sys.exit(43)
if name == 'make':
    pathlib.Path('kernel').mkdir(exist_ok=True)
    pathlib.Path('kernel/susfs_guard_lkm.ko').write_bytes(b'mock ko')
elif name == 'clang':
    if args == ['--version']:
        print('mock clang')
    else:
        loader = bytearray(64)
        loader[:7] = b'\x7fELF\x02\x01\x01'
        struct.pack_into('<HH', loader, 16, 3, 183)
        pathlib.Path(args[args.index('-o')+1]).write_bytes(loader)
elif name == 'readelf':
    machine = 'X86-64' if mode == 'architecture' else 'AArch64'
    print('Machine: ' + machine)
    print('Type: REL (Relocatable file)')
    release = 'wrong' if mode == 'vermagic' else '6.12.76-4k'
    print('vermagic=' + release + ' SMP preempt mod_unload modversions aarch64')
    if mode != 'namespace':
        print('import_ns=VFS_internal_I_am_really_a_filesystem_and_am_NOT_a_driver')
    if args[-1] == 'out/susfs_insmod':
        if mode == 'loader-relocations':
            print('00002700  000000000403 R_AARCH64_RELATIVE  9d4')
        if mode == 'loader-interpreter':
            print('  INTERP  0x000000  0x000000')
elif name == 'nm':
    if mode != 'empty-symbols':
        print(' U ' + ('kallsyms_lookup_name' if mode == 'forbidden-symbol' else 'fput'))
elif name == 'python3':
    if args[0] == '-c':
        os.execv(sys.executable, [sys.executable, *args])
    assert args == ['tools/cfi_sites.py', '--allow',
                    'init_module,susfs_layers_down,susfs_tw_func,susfs_watch_freeing',
                    'kernel/susfs_guard_lkm.ko']
    print('mock CFI checks passed')
else:
    raise AssertionError(name)
'''


class BuildTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.repo = self.base / 'repo'
        self.repo.mkdir()
        for name in ('tools/build-pinned.sh', 'tools/build-one.sh', 'tools/pack_module.py', 'config/build.env'):
            path = self.repo / name
            path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ROOT / name, path)
        self.bin = self.base / 'bin'
        self.bin.mkdir()
        self.env = {k: v for k, v in os.environ.items() if not k.startswith('GIT_')}
        self.env.update(HOME=str(self.base), XDG_CACHE_HOME=str(self.base / 'cache'),
                        PATH=str(self.bin) + os.pathsep + os.environ['PATH'],
                        GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_SYSTEM=os.devnull,
                        GIT_AUTHOR_NAME='Test', GIT_AUTHOR_EMAIL='test@example.invalid',
                        GIT_COMMITTER_NAME='Test', GIT_COMMITTER_EMAIL='test@example.invalid',
                        GIT_AUTHOR_DATE='2026-10-17T00:00:00Z',
                        GIT_COMMITTER_DATE='2026-10-17T00:00:00Z',
                        GITHUB_OUTPUT=str(self.base / 'output'),
                        CALLS=str(self.base / 'calls'), IMAGE=IMAGE)
        self.git('init', '-q')
        self.git('add', '.')
        self.git('-c', 'commit.gpgsign=false', '-c', 'core.hooksPath=/dev/null',
                 'commit', '-qm', 'unsigned fixture')
        self.spy('docker', DOCKER)
        (self.bin / 'uname').write_text('#!/bin/sh\n[ "$1" = -s ] && echo Linux || echo x86_64\n')
        (self.bin / 'uname').chmod(0o755)

    def git(self, *args):
        return subprocess.check_output(['git', *args], cwd=self.repo, env=self.env, text=True)

    def spy(self, name, code):
        path = self.bin / name
        path.write_text('#!' + sys.executable + '\n' + code)
        path.chmod(0o755)

    def run_build(self, script='build-pinned.sh', **extra):
        return subprocess.run(['bash', 'tools/' + script], cwd=self.repo,
                              env=self.env | extra, text=True, capture_output=True)

    def test_two_clean_builds_and_provenance(self):
        (self.repo / 'untracked').write_text('not a build input')
        result = self.run_build()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        work = Path((self.base / 'output').read_text().strip().removeprefix('work='))
        calls = [json.loads(line) for line in (self.base / 'calls').read_text().splitlines()]
        self.assertEqual([c[0] for c in calls], ['pull', 'run', 'run'])
        out = work / 'artifacts'
        prov = (out / 'provenance.txt').read_text()
        self.assertIn('source_commit=' + self.git('rev-parse', 'HEAD').strip(), prov)
        self.assertIn('ddk_image=' + IMAGE, prov)
        self.assertIn('device_validation=not-performed', prov)
        self.assertIn('reproduction=two-clean-containers-byte-identical', prov)
        subprocess.run(['sha256sum', '-c', 'SHA256SUMS'], cwd=out, check=True,
                       capture_output=True)
        self.assertNotIn('SHA256SUMS', (out / 'SHA256SUMS').read_text())
        self.assertFalse(list(out.glob('*.zip')))
        self.assertEqual(self.git('diff', '--name-only'), '')

    def test_mismatch_and_compile_failure_never_publish_artifacts(self):
        for extra in ({'MISMATCH': f'susfs_guard_lkm-{KMI}.ko'},
                      {'MISMATCH': 'susfs_insmod'}, {'FAIL_PASS': '2'}):
            with self.subTest(extra=extra):
                result = self.run_build(**extra)
                self.assertNotEqual(result.returncode, 0)
        self.assertFalse(list((self.base / 'cache').glob('**/artifacts')))

    def test_dirty_source_and_mutable_image_refused_before_docker(self):
        (self.repo / 'tools/build-one.sh').write_text('dirty\n')
        self.assertIn('Commit tracked changes', self.run_build().stderr)
        self.git('checkout', '--', 'tools/build-one.sh')
        pin = self.repo / 'config/build.env'
        pin.write_text(pin.read_text().replace(IMAGE, 'ghcr.io/ylarod/ddk-min:latest'))
        self.git('add', '.')
        self.git('-c', 'commit.gpgsign=false', '-c', 'core.hooksPath=/dev/null',
                 'commit', '-qm', 'unsigned bad pin')
        self.assertIn('pinned by digest', self.run_build().stderr)
        self.assertFalse((self.base / 'calls').exists())

    def test_wrong_host_is_refused(self):
        (self.bin / 'uname').write_text('#!/bin/sh\necho aarch64\n')
        result = self.run_build()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('x86-64', result.stderr)
        self.assertFalse((self.base / 'calls').exists())

    def test_artifact_gates_fail_closed(self):
        kdir = self.base / 'kdir'
        kdir.mkdir()
        (kdir / '.config').write_text('CONFIG_CFI_CLANG=y\n')
        (kdir / 'Module.symvers').write_text('mock symvers\n')
        for name in ('make', 'clang', 'readelf', 'nm', 'python3'):
            self.spy(name, TOOLS)
        env = dict(KDIR=str(kdir), SOURCE_DATE_EPOCH='1792195200',
                   TMPDIR=str(self.base / 'tmp'))
        result = self.run_build('build-one.sh', **env)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        for fault in ('make', 'clang', 'readelf', 'nm', 'python3', 'architecture',
                      'vermagic', 'namespace', 'empty-symbols', 'forbidden-symbol',
                      'loader-relocations', 'loader-interpreter'):
            with self.subTest(fault=fault):
                result = self.run_build('build-one.sh', FAULT=fault, **env)
                self.assertNotEqual(result.returncode, 0, fault)
                if fault == 'python3':
                    self.assertIn('simulated python3 failure', result.stdout)
        (kdir / 'Module.symvers').unlink()
        self.assertIn('Missing DDK', self.run_build('build-one.sh', **env).stderr)

    def test_workflow_boundaries_and_pins(self):
        workflows = ROOT / '.github/workflows'
        for path in workflows.glob('*.yml'):
            text = path.read_text()
            if path.name == 'build-pinned.yml':
                self.assertIn('persist-credentials: false', text)
                self.assertIn('contents: read', text)
                self.assertIn('ubuntu-24.04', text)
                self.assertNotIn('pull_request_target', text)
                self.assertNotIn('secrets.', text)
                self.assertNotIn('--privileged', text)
                package_step = text.index('name: Package the accepted build (no device operations)')
                self.assertLess(text.index('run: bash tools/build-pinned.sh'), package_step)
                self.assertLess(package_step, text.index('name: Upload installable experimental module'))
                self.assertIn("--build-artifacts '${{ steps.build.outputs.work }}/artifacts'", text)
                for action in re.findall(r'uses: (\S+)', text):
                    self.assertRegex(action, r'@[0-9a-f]{40}$')
            else:
                jobs = re.findall(r'^  [\w-]+:\n(.*?)(?=^  [\w-]+:|\Z)',
                                  text.split('jobs:\n', 1)[1], re.M | re.S)
                self.assertTrue(jobs, path.name)
                for job in jobs:
                    self.assertIn("if: github.repository == 'inforcqb/susfs4ksu-lkm'", job)
        self.assertIn('DDK_IMAGE=' + IMAGE, (ROOT / 'config/build.env').read_text())
        for path in (ROOT / 'tools').glob('build-*.sh'):
            subprocess.run(['bash', '-n', str(path)], check=True)


if __name__ == '__main__':
    unittest.main()
