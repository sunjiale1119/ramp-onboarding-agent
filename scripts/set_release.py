"""Persist the exact image/revision for subsequent Compose restarts, without printing secrets."""
import os
from pathlib import Path
import re
import tempfile


def main(path, revision):
    target = Path(path)
    if target.name != '.env' or not target.is_file() or not re.fullmatch(r'[0-9a-f]{40}', revision):
        raise ValueError('Expected an existing .env and full commit SHA')
    original = target.read_text(encoding='utf-8-sig')
    lines = [line for line in original.splitlines() if not line.startswith(('RAMP_IMAGE=', 'RAMP_RELEASE='))]
    data = '\n'.join(lines) + f'\nRAMP_IMAGE=ramp:{revision[:7]}\nRAMP_RELEASE={revision}\n'
    fd, temporary = tempfile.mkstemp(prefix='.env.release-', dir=target.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8', newline='\n') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
        os.chmod(target, 0o600)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)  # Only this invocation's exact temporary file.
    print('Pinned deployment revision:', revision)


if __name__ == '__main__':
    import sys
    main(sys.argv[1], sys.argv[2])
