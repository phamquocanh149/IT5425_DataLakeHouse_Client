"""OS advisory locks are released by the kernel on process exit, including crashes."""
import os
from contextlib import contextmanager


class JobLocked(RuntimeError):
    pass


@contextmanager
def job_lock(root, job_id):
    directory = root / "data" / "locks"
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / (job_id + ".lock")).open("a+b") as stream:
        if stream.tell() == 0:
            stream.write(b"0"); stream.flush()
        stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise JobLocked("Job is being processed") from None
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == "nt":
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
