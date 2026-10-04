"""Private local pilot setup; no overwrites, source edits or external actions."""
import os
import re
import secrets
import stat
from pathlib import Path


def prepare_pilot(path):
    path = Path(path)
    try:
        fd = os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    except FileExistsError:
        metadata = path.lstat()
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError('Pilot configuration must be a regular private file')
        if metadata.st_mode & 0o077:
            raise ValueError('Existing pilot credential file must have mode 600')
        if metadata.st_size > 4096:
            raise ValueError('Pilot configuration exceeds size limit')
        values = {}
        for line in path.read_text().splitlines():
            if not line.strip() or line.lstrip().startswith('#'):
                continue
            name, separator, value = line.partition('=')
            if not separator or name in values:
                raise ValueError('Invalid private pilot configuration')
            values[name] = value
        passwords = [values.get(name, '') for name in
                     ('LISA_PILOT_ADMIN_PASSWORD', 'LISA_PILOT_DATABASE_PASSWORD')]
        if any(not re.fullmatch(r'[A-Za-z0-9_-]{32,128}', password) for password in passwords) or passwords[0] == passwords[1]:
            raise ValueError('Pilot configuration needs distinct URL-safe passwords of at least 32 characters')
        return path
    with os.fdopen(fd,'w') as stream:
        stream.write('# Private isolated local PostgreSQL pilot credentials.\n')
        stream.write('LISA_PILOT_ADMIN_PASSWORD='+secrets.token_hex(32)+'\n')
        stream.write('LISA_PILOT_DATABASE_PASSWORD='+secrets.token_hex(32)+'\n')
        stream.flush()
        os.fsync(stream.fileno())
    return path
