"""Private, atomic provider credential overrides shared by web and workers.

Secrets stay outside telemetry/audit tables. Deploy this file on a private shared
volume, with the same application UID across processes; encrypt the volume when
encrypted storage is required. Reads never return keys to an HTTP endpoint.
"""
import fcntl
import json
import os
import tempfile
from pathlib import Path

FIELDS = {'oddspapi': 'oddspapi_key', 'oddspapi_rapidapi': 'oddspapi_rapidapi_key', 'api_football': 'api_football_key',
          'allsports': 'allsports_api_key', 'football_data': 'football_data_token',
          'sharpapi': 'sharpapi_key', 'the_odds_api': 'odds_api_key'}

CREDENTIAL_DDL = """
CREATE TABLE IF NOT EXISTS provider_credentials (
    field TEXT PRIMARY KEY,
    ciphertext TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


def credential_store(settings, storage=None):
    if settings.provider_credentials_backend == 'file':
        return CredentialStore(settings.provider_credentials_path)
    if settings.provider_credentials_backend == 'postgres':
        return EncryptedCredentialStore(storage, settings.credential_encryption_key)
    raise ValueError('Unsupported credential storage backend')


class EncryptedCredentialStore:
    """Durable overrides; the encryption key stays in deployment secrets.

    Empty overrides disable a source; deleting an override restores its
    environment value. A bad key or corrupt row fails closed.
    """
    def __init__(self, storage, encryption_key):
        if storage is None or not hasattr(storage, '_tx'):
            raise ValueError('Database credential storage requires relational storage')
        from cryptography.fernet import Fernet
        try:
            self.cipher = Fernet(encryption_key.encode('ascii'))
        except (ValueError, UnicodeError, AttributeError):
            raise ValueError('Configure a valid LISA_CREDENTIAL_ENCRYPTION_KEY') from None
        self.storage = storage

    def read(self):
        with self.storage._tx() as conn:
            rows = conn.execute('SELECT field,ciphertext FROM provider_credentials').fetchall()
        values = {}
        for row in rows:
            try:
                if row['field'] not in FIELDS.values():
                    raise ValueError()
                value = self.cipher.decrypt(row['ciphertext'].encode('ascii')).decode('utf-8')
                if len(value) > 4096 or any(ord(c) < 32 for c in value):
                    raise ValueError()
                values[row['field']] = value
            except Exception:
                raise ValueError('Provider credential storage could not be decrypted') from None
        return values

    def update(self, provider, value, *, inherit=False):
        if provider not in FIELDS:
            raise ValueError('Unsupported provider')
        if not isinstance(value, str) or len(value) > 4096 or any(ord(c) < 32 for c in value):
            raise ValueError('Credential must be text without control characters, up to 4096 characters')
        from datetime import datetime, timezone
        with self.storage._tx() as conn:
            if inherit:
                conn.execute('DELETE FROM provider_credentials WHERE field=?', (FIELDS[provider],))
            else:
                encrypted = self.cipher.encrypt(value.strip().encode('utf-8')).decode('ascii')
                conn.execute('INSERT INTO provider_credentials VALUES (?, ?, ?) ON CONFLICT(field) '
                    'DO UPDATE SET ciphertext=excluded.ciphertext,updated_at=excluded.updated_at',
                    (FIELDS[provider],encrypted,datetime.now(timezone.utc).isoformat()))


class CredentialStore:
    def __init__(self, path):
        self.path = Path(path)

    def read(self):
        try:
            stat = self.path.stat()
        except FileNotFoundError:
            return {}
        if stat.st_mode & 0o077:
            raise ValueError('Credential file must be private to the application user')
        if stat.st_size > 32768:
            raise ValueError('Credential file exceeds size limit')
        payload = json.loads(self.path.read_text())
        if not isinstance(payload, dict) or any(k not in FIELDS.values() or not isinstance(v, str)
                                               for k, v in payload.items()):
            raise ValueError('Invalid provider credential configuration')
        return payload

    def update(self, provider, value, *, inherit=False):
        if provider not in FIELDS:
            raise ValueError('Unsupported provider')
        if not isinstance(value, str) or len(value) > 4096 or any(ord(c) < 32 for c in value):
            raise ValueError('Credential must be text without control characters, up to 4096 characters')
        self.path.parent.mkdir(parents=True, exist_ok=True)
        lock_fd = os.open(str(self.path) + '.lock', os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            payload = self.read()
            if inherit:
                payload.pop(FIELDS[provider], None)
            else:
                payload[FIELDS[provider]] = value.strip()
            fd, temp = tempfile.mkstemp(dir=self.path.parent, prefix='.provider-credentials-')
            try:
                with os.fdopen(fd, 'w') as handle:
                    json.dump(payload, handle)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temp, self.path)
                directory = os.open(self.path.parent, os.O_RDONLY)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
            finally:
                if os.path.exists(temp):
                    os.unlink(temp)
        finally:
            os.close(lock_fd)
