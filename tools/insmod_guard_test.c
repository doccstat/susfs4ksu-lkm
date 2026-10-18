// SPDX-License-Identifier: GPL-2.0
/* Host-only: real file locks, but every reboot/module/kernel-state syscall is mocked. */
#include <assert.h>
#include <errno.h>
#include <fcntl.h>
#include <setjmp.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/file.h>
#include <sys/wait.h>
#include <unistd.h>

enum { HOST_OPEN_FLAGS = O_WRONLY | O_CREAT | O_NOFOLLOW | O_CLOEXEC };
#undef AT_FDCWD
#undef O_RDONLY
#undef O_WRONLY
#undef O_NONBLOCK
#undef O_CREAT
#undef O_NOFOLLOW
#undef O_CLOEXEC
#define SUSFS_INSMOD_HOSTTEST 1
#include "susfs_insmod.c"

static jmp_buf exit_trap;
static const char *lock_path;
static int lock_fd = -1, locked, uid, open_error, flock_error, exit_code;
static int probes, image_opens, reply_err[2], reply_byte[2];
static sysarg reply_ret[2];

sysarg sys6(sysarg n, sysarg a, sysarg b, sysarg c, sysarg d, sysarg e, sysarg f)
{
	(void)e; (void)f;
	switch (n) {
	case SYS_geteuid:
		return uid;
	case SYS_openat:
		if (!strcmp((const char *)b, "/data/adb/.susfs_guard_lkm.lock")) {
			assert(!probes && !image_opens);
			assert(c == (O_WRONLY | O_CREAT | O_NOFOLLOW | O_CLOEXEC));
			assert(d == 0600 && O_NOFOLLOW == 0100000);
			if (open_error)
				return -open_error;
			lock_fd = open(lock_path, HOST_OPEN_FLAGS, 0600);
			return lock_fd < 0 ? -errno : lock_fd;
		}
		/* Stop before reading even a fake module; kernel state must stay untouched. */
		assert(!strcmp((const char *)b, "test.ko"));
		assert(locked && probes == 2);
		image_opens++;
		return -2;
	case SYS_flock:
		assert(a == lock_fd && b == (LOCK_EX | LOCK_NB) && !probes);
		if (flock_error)
			return -flock_error;
		if (flock(lock_fd, (int)b))
			return -errno;
		locked = 1;
		return 0;
	case SYS_reboot: {
		char *reply = (char *)d;
		int error, i = probes++;
		assert(locked && !image_opens && i < 2);
		assert(a == 0xdeadbeef && b == 0xfafafafa);
		assert(c == (i ? 0x555e3 : 0x555e1));
		memcpy(&error, reply + 16, sizeof(error));
		assert(error == 126 && !memcmp(reply, (char[16]){0}, 16));
		memcpy(reply + 16, &reply_err[i], sizeof(error));
		if (reply_byte[i] >= 0)
			reply[reply_byte[i]] = 'x';
		return reply_ret[i];
	}
	case SYS_write:
		assert(a == 1); /* only diagnostic output, never a sysctl write */
		return c;
	case SYS_exit:
		exit_code = (int)a;
		longjmp(exit_trap, 1);
	default:
		assert(!"unexpected syscall: no real kernel operations are allowed");
		return -1;
	}
}

static void reset(void)
{
	if (lock_fd >= 0)
		close(lock_fd);
	lock_fd = -1;
	locked = uid = open_error = flock_error = probes = image_opens = 0;
	out_len = 0;
	kptr_saved = 0;
	reply_ret[0] = reply_ret[1] = -22;
	reply_err[0] = reply_err[1] = 126;
	reply_byte[0] = reply_byte[1] = -1;
}

static int run_loader(void)
{
	char *args[] = { "susfs_insmod", "test.ko" };
	if (!setjmp(exit_trap)) {
		insmod_main(2, args);
		assert(!"loader returned without exiting");
	}
	return exit_code;
}

int main(int argc, char **argv)
{
	int i, ready[2], status;
	pid_t child;
	char byte;
	assert(argc == 2);
	lock_path = argv[1];
	reset();
	assert(run_loader() == 2 && probes == 2 && image_opens == 1);

	for (i = 0; i < 2; i++) {
		reset();
		reply_err[i] = 0;
		assert(run_loader() == 0 && !image_opens);
		reset();
		reply_err[i] = -12; /* existing interface error still forbids stacking */
		assert(run_loader() == 0 && !image_opens);
		reset();
		reply_byte[i] = 15; /* do not inspect only the first byte */
		assert(run_loader() == 0 && !image_opens);
	}
	for (i = 0; i < 2; i++) {
		const sysarg ambiguous[] = { 0, -1, -13, -38, -14 };
		unsigned j;
		for (j = 0; j < sizeof(ambiguous) / sizeof(ambiguous[0]); j++) {
			reset();
			reply_ret[i] = ambiguous[j];
			assert(run_loader() == 1 && !image_opens);
		}
	}
	reset(); uid = 2000;
	assert(run_loader() == 1 && lock_fd == -1 && !probes);
	reset(); open_error = 13;
	assert(run_loader() == 13 && !probes && !image_opens);
	reset(); flock_error = 38;
	assert(run_loader() == 38 && !probes && !image_opens);

	/* A killed owner releases the kernel lock without deleting any stale marker. */
	reset();
	assert(pipe(ready) == 0);
	child = fork();
	assert(child >= 0);
	if (!child) {
		close(ready[0]);
		assert(run_loader() == 2 && locked);
		assert(write(ready[1], "x", 1) == 1);
		for (;;)
			pause();
	}
	close(ready[1]);
	assert(read(ready[0], &byte, 1) == 1);
	close(ready[0]);
	assert(run_loader() == EWOULDBLOCK && !probes && !image_opens);
	assert(kill(child, SIGKILL) == 0);
	assert(waitpid(child, &status, 0) == child && WIFSIGNALED(status));
	reset();
	assert(run_loader() == 2 && probes == 2 && image_opens == 1);
	reset();
	puts("load guards: absent/present/ambiguous/root/errors/contention/SIGKILL checks passed");
	return 0;
}
