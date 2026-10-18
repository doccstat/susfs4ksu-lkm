// SPDX-License-Identifier: GPL-2.0
/* Run without libc/CRT relocation, like the loader. All kernel-state IO is fake. */
#define SUSFS_INSMOD_HOSTTEST 1
#include "susfs_insmod.c"

static int writes, reports;

/* Only stdout and process exit reach the host kernel. */
static sysarg host_call(sysarg n, sysarg a, sysarg b, sysarg c)
{
#if defined(__aarch64__)
	register sysarg x0 __asm__("x0") = a;
	register sysarg x1 __asm__("x1") = b;
	register sysarg x2 __asm__("x2") = c;
	register sysarg x8 __asm__("x8") = n;
	__asm__ volatile("svc #0" : "+r"(x0) : "r"(x1), "r"(x2), "r"(x8) : "memory", "cc");
	return x0;
#elif defined(__x86_64__)
	sysarg result;
	__asm__ volatile("syscall" : "=a"(result)
		: "a"(n == SYS_exit ? 60 : 1), "D"(a), "S"(b), "d"(c)
		: "rcx", "r11", "memory", "cc");
	return result;
#else
#error "This host test supports Linux arm64 and x86-64"
#endif
}

sysarg sys6(sysarg n, sysarg a, sysarg b, sysarg c, sysarg d, sysarg e, sysarg f)
{
	(void)d; (void)e; (void)f;
	if (n == SYS_openat &&
	    s_len((const char *)b) == sizeof("/proc/sys/kernel/kptr_restrict") - 1 &&
	    s_eq_n((const char *)b, "/proc/sys/kernel/kptr_restrict", sizeof("/proc/sys/kernel/kptr_restrict")))
		return 7;
	if (n == SYS_close && a == 7)
		return 0;
	if (n == SYS_read && a == 7 && c >= 2) {
		copy_n((char *)b, "2\n", 2);
		return 2;
	}
	if (n == SYS_write && a == 7) {
		if ((!writes && c == 1 && s_eq_n((const char *)b, "1", 1)) ||
		    (writes == 1 && c == 2 && s_eq_n((const char *)b, "2\n", 2))) {
			writes++;
			return c;
		}
	}
	if (n == SYS_write && a == 1) {
		if (find_sub((const char *)b, (u64)c, "kmsg| ", 6) >= 0)
			reports++;
		return host_call(n, a, b, c);
	}
	if (n == SYS_exit && a == 22 && writes == 2 && reports == 8)
		return host_call(SYS_exit, 0, 0, 0);
	return host_call(SYS_exit, 99, 0, 0); /* unexpected IO or failed assertion */
}

void test_main(void)
{
	static const char records[] =
		"Unknown symbol example\n"
		"version magic 'old' should be 'new'\n"
		"does not import it\n"
		"disagrees about version\n"
		"no symbol version\n"
		"verification failed\n"
		"invalid module format\n"
		"module_layout\n"
		"unrelated message\n";
	u64 length = 0;
	const char *required;

	kptr_relax();
	copy_n(kbuf, records, sizeof(records));
	kbuf_len = sizeof(records) - 1;
	kmsg_report();
	required = kmsg_required_vermagic(&length);
	if (!required || length != 3 || !s_eq_n(required, "new", 3))
		finish(99);
	finish(22); /* an ordinary load error must still restore the saved setting */
}

#if defined(__aarch64__)
__asm__(".text\n.global _start\n_start:\nbl test_main\nbrk #0\n");
#elif defined(__x86_64__)
__asm__(".text\n.global _start\n_start:\nandq $-16, %rsp\ncall test_main\nud2\n");
#endif
