"""Linux-only no-network launcher; restrictions survive exec and child processes."""

import ctypes
import ctypes.util
import errno
import os
import socket
import sys


def main():
    library = ctypes.util.find_library("seccomp")
    if not library:
        raise SystemExit("libseccomp is required; refusing an unguarded recipe run")
    seccomp = ctypes.CDLL(library, use_errno=True)
    seccomp.seccomp_init.argtypes = [ctypes.c_uint32]
    seccomp.seccomp_init.restype = ctypes.c_void_p
    seccomp.seccomp_syscall_resolve_name.argtypes = [ctypes.c_char_p]
    seccomp.seccomp_rule_add.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int, ctypes.c_uint]
    seccomp.seccomp_load.argtypes = [ctypes.c_void_p]
    seccomp.seccomp_release.argtypes = [ctypes.c_void_p]
    context = seccomp.seccomp_init(0x7FFF0000)
    assert context

    class Argument(ctypes.Structure):
        _fields_ = [
            ("arg", ctypes.c_uint),
            ("op", ctypes.c_uint),
            ("datum_a", ctypes.c_uint64),
            ("datum_b", ctypes.c_uint64),
        ]

    number = seccomp.seccomp_syscall_resolve_name(b"socket")
    assert number >= 0
    for family in (socket.AF_INET, socket.AF_INET6):
        argument = Argument(0, 4, family, 0)
        assert seccomp.seccomp_rule_add(context, 0x50000 | errno.EPERM, number, 1, argument) == 0
    assert seccomp.seccomp_load(context) == 0
    seccomp.seccomp_release(context)
    for family in (socket.AF_INET, socket.AF_INET6):
        try:
            socket.socket(family)
        except OSError as error:
            assert error.errno == errno.EPERM
        else:
            raise SystemExit("network guard failed")
    os.execv(sys.executable, [sys.executable, *sys.argv[1:]])


if __name__ == "__main__":
    main()
