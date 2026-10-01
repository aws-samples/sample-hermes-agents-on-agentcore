"""Trusted final stage: close mount-source descriptors BEFORE starting any worker."""

import os


def main() -> None:
    # bubblewrap preserves caller descriptors. Leaving even the selected directory
    # open would let openat(fd, '..') bypass the mount namespace root.
    for entry in os.listdir('/proc/self/fd'):
        fd = int(entry)
        if fd > 2:
            try:
                os.close(fd)
            except OSError:
                # The descriptor used by listdir itself has already closed.
                if os.path.exists(f'/proc/self/fd/{fd}'):
                    raise
    os.execve('/usr/local/bin/python',
              ['python', '-I', '/app/inspect_sandbox.py'], dict(os.environ))


if __name__ == '__main__':
    main()
