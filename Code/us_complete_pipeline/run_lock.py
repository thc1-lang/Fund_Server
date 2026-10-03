"""Process locks on macOS, Linux and Windows; released when the process exits."""

import os


def acquire_lock(handle):
    if os.name == "nt":
        import msvcrt

        handle.seek(0)
        handle.write("0")
        handle.flush()
        handle.seek(0)
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            raise BlockingIOError("Another run is active") from exc
    else:
        import fcntl

        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
