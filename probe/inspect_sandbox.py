"""Runs INSIDE the sandbox. Only fixed tests are executed, never caller-supplied code."""

import ctypes
import errno
import fcntl
import json
import os
import socket
import struct
import sys
from pathlib import Path


def unreadable(path: str) -> bool:
    try:
        Path(path).read_bytes()
        return False
    except (FileNotFoundError, PermissionError, NotADirectoryError):
        return True


def cannot_connect(ip: str, port: int) -> bool:
    with socket.socket() as sock:
        sock.settimeout(1)
        try:
            sock.connect((ip, port))
            return False
        except OSError:
            return True


def main() -> None:
    args = json.load(sys.stdin)
    status = dict(line.split(':', 1) for line in Path('/proc/self/status').read_text().splitlines())
    namespaces = {key: os.readlink(f'/proc/self/ns/{key}') for key in args['namespaces']}
    interfaces = socket.if_nameindex()
    # Linux can create DOWN tunnel templates in every fresh network namespace.
    # Check IFF_UP rather than incorrectly treating those templates as connectivity.
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        interface_flags = {
            name: struct.unpack_from('H', fcntl.ioctl(
                sock.fileno(), 0x8913, struct.pack('256s', name.encode())), 16)[0]
            for _, name in interfaces
        }
    checks = {
        'private_namespaces': all(namespaces[k] != v for k, v in args['namespaces'].items()),
        'no_capabilities': int(status['CapEff'].strip(), 16) == 0,
        'no_new_privileges': status['NoNewPrivs'].strip() == '1',
        'outer_efs_hidden': not Path(args['root']).exists(),
        'sibling_secret_hidden': unreadable(args['sibling_secret']),
        'supervisor_root_hidden': unreadable(f"/proc/{args['supervisor_pid']}/root{args['sibling_secret']}"),
        'symlink_escape_blocked': unreadable('/workspace/escape'),
        'no_aws_credentials': not any(k.startswith('AWS_') for k in os.environ),
        'metadata_network_blocked': cannot_connect('169.254.169.254', 80),
        'container_metadata_blocked': cannot_connect('169.254.170.2', 80),
        'direct_internet_blocked': cannot_connect('1.1.1.1', 443),
        'no_active_non_loopback_interfaces': all(
            name == 'lo' or not flags & 1 for name, flags in interface_flags.items()),
    }
    for index, address in enumerate(args['nfs_addresses']):
        checks[f'nfs_target_{index}_blocked'] = cannot_connect(address, 2049)
    inherited = []
    for entry in Path('/proc/self/fd').iterdir():
        if int(entry.name) <= 2:
            continue
        try:
            inherited.append(os.readlink(entry))
        except FileNotFoundError:
            pass
    checks['no_inherited_fds'] = not inherited
    # Even with an NFS client/library available, mounting must not be possible.
    libc = ctypes.CDLL(None, use_errno=True)
    result = libc.mount(b'none', b'/tmp', b'tmpfs', 0, None)
    checks['mount_denied'] = result == -1 and ctypes.get_errno() == errno.EPERM
    marker = Path('/workspace/persistence-marker')
    previous = marker.read_text() if marker.exists() else None
    if args['operation'] == 'write':
        with marker.open('w') as handle:
            handle.write(args['marker'])
            handle.flush()
            os.fsync(handle.fileno())
    checks['own_workspace_read_write'] = marker.exists()
    if args['operation'] == 'read':
        checks['marker_survived_session'] = previous == args['marker']
    print(json.dumps({'passed': all(checks.values()), 'checks': checks,
                      'namespaces': namespaces, 'inherited_fds': inherited,
                      'interfaces': interface_flags}))


if __name__ == '__main__':
    main()
