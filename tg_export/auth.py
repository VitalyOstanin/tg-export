"""Account management with session storage."""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path

import click
import yaml

from tg_export.errors import TgExportError
from tg_export.privacy import restrict_file, tighten_if_loose

logger = logging.getLogger(__name__)


class AccountNameError(TgExportError, ValueError):
    """Raised when an account alias cannot be used as a file name."""


class CredentialsError(TgExportError, ValueError):
    """Raised when api_credentials.yaml is missing required fields or has bad types."""


_ACCOUNT_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def validate_account_name(name: str) -> str:
    """Return the name if it is safe to put into a file path, else raise.

    The alias goes straight into the session and config file names, so a name
    with a separator or a parent reference would place -- or delete -- files
    outside the configuration directory.
    """
    if not isinstance(name, str) or not _ACCOUNT_NAME_RE.match(name) or name in {".", ".."}:
        raise AccountNameError(
            f"Invalid account name {name!r}: use letters, digits, dot, dash and underscore, "
            f"starting with a letter or a digit."
        )
    return name


# Default amount of free disk space an export refuses to go below. Declared
# once: the value used to be repeated as a literal in the caller and as a
# string in the output of `tg-export config`.
DEFAULT_MIN_FREE_SPACE = "20GB"

# How many times the two-factor password is asked before the login is given up.
_PASSWORD_ATTEMPTS = 3


def _notify(message: str) -> None:
    """Print a login status line to stderr.

    stdout carries the machine-readable output of the query commands, so status
    lines of an interactive login belong on the other stream -- the same rule
    the CLI applies through its own ``_diag``.
    """
    click.echo(message, err=True)


# Keys the global config.yaml may carry.
_KNOWN_GLOBAL_KEYS = {"proxy", "min_free_space"}


def default_config_dir() -> Path:
    """Where accounts, sessions and the global config live.

    TG_EXPORT_CONFIG_DIR wins, so separate sets of accounts (work and personal)
    can live side by side; otherwise the XDG Base Directory location is used.
    """
    override = os.environ.get("TG_EXPORT_CONFIG_DIR")
    if override:
        return Path(override).expanduser()
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg).expanduser() if xdg else Path.home() / ".config"
    return base / "tg-export"


class AccountManager:
    def __init__(self, config_dir: Path | None = None):
        self.config_dir = Path(config_dir) if config_dir else default_config_dir()

    def ensure_dirs(self):
        self.config_dir.mkdir(parents=True, exist_ok=True)
        # Why: api_credentials.yaml lives here; its directory should be 0o700
        # so other local users cannot enumerate accounts/credentials.
        try:
            os.chmod(self.config_dir, 0o700)
        except OSError as e:
            logger.debug("ensure_dirs: cannot chmod %s: %s", self.config_dir, e)
        self.sessions_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(self.sessions_dir, 0o700)

    @property
    def sessions_dir(self) -> Path:
        return self.config_dir / "sessions"

    def session_path(self, name: str) -> Path:
        return self.sessions_dir / f"{validate_account_name(name)}.session"

    def config_path(self, name: str) -> Path:
        return self.config_dir / f"{validate_account_name(name)}.yaml"

    def resolve_config(self, account: str, config_override: str | None = None) -> Path:
        """Return config path: explicit override or convention-based."""
        if config_override:
            return Path(config_override)
        return self.config_path(account)

    def list_accounts(self) -> list[str]:
        if not self.sessions_dir.exists():
            return []
        return sorted(p.stem for p in self.sessions_dir.iterdir() if p.suffix == ".session")

    def remove_account(self, name: str):
        path = self.session_path(name)
        if path.exists():
            path.unlink()
        journal = path.with_suffix(".session-journal")
        if journal.exists():
            journal.unlink()

    def set_default_account(self, name: str):
        """Set default account alias."""
        default_path = self.config_dir / "default_account"
        default_path.write_text(name, encoding="utf-8")

    def get_default_account(self) -> str | None:
        """Get default account alias, or None."""
        default_path = self.config_dir / "default_account"
        if default_path.exists():
            return default_path.read_text(encoding="utf-8").strip()
        return None

    def resolve_account(self, account: str | None) -> str:
        """Resolve account: explicit arg > default > error."""
        if account:
            return account
        default = self.get_default_account()
        if default:
            return default
        raise click.UsageError(
            "No --account specified and no default set. Use 'tg-export account default <name>' to set one."
        )

    def save_credentials(self, api_id: int, api_hash: str):
        cred_path = self.config_dir / "api_credentials.yaml"
        data = {"api_id": api_id, "api_hash": api_hash}
        cred_path.write_text(yaml.dump(data, default_flow_style=False), encoding="utf-8")
        os.chmod(cred_path, 0o600)

    def load_credentials(self) -> tuple[int, str]:
        cred_path = self.config_dir / "api_credentials.yaml"
        if not cred_path.exists():
            raise CredentialsError(
                f"api_credentials.yaml not found at {cred_path}. "
                f"Run 'tg-export auth credentials' to create it."
            )
        # Warn if permissions are too loose; does not block to keep CI fixtures simple.
        tighten_if_loose(cred_path)
        try:
            data = yaml.safe_load(cred_path.read_text(encoding="utf-8"))
        except yaml.YAMLError as e:
            raise CredentialsError(f"Cannot parse {cred_path}: {e}") from e
        if not isinstance(data, dict):
            raise CredentialsError(f"{cred_path} must contain a YAML mapping, got {type(data).__name__}")
        api_id = data.get("api_id")
        api_hash = data.get("api_hash")
        if not isinstance(api_id, int):
            raise CredentialsError(f"{cred_path}: api_id must be an integer, got {type(api_id).__name__}")
        if not isinstance(api_hash, str) or not api_hash:
            raise CredentialsError(f"{cred_path}: api_hash must be a non-empty string")
        return api_id, api_hash

    def load_global_config(self) -> dict:
        """Load global config from config.yaml. Returns raw dict.

        The file carries the proxy login and password, so it gets the same
        treatment as api_credentials.yaml -- until now its mode was never
        looked at.
        """
        from tg_export.config import ConfigError

        config_path = self.config_dir / "config.yaml"
        if not config_path.exists():
            return {}
        tighten_if_loose(config_path)
        try:
            data = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as e:
            raise ConfigError(f"Cannot parse {config_path}: {e}") from e
        if not isinstance(data, dict):
            raise ConfigError(f"{config_path} must contain a YAML mapping, got {type(data).__name__}")
        # A typo in a section name used to be ignored in silence, and for the
        # proxy section that means connecting directly instead -- exactly the
        # outcome TgApi refuses to allow when python-socks is missing.
        unknown = set(data) - _KNOWN_GLOBAL_KEYS
        if unknown:
            known = ", ".join(sorted(_KNOWN_GLOBAL_KEYS))
            raise ConfigError(
                f"Unknown key(s) in {config_path}: {', '.join(sorted(unknown))}. Known keys: {known}"
            )
        return data

    def load_proxy(self) -> tuple | None:
        """Load global proxy settings from config.yaml."""
        from tg_export.config import ConfigError

        data = self.load_global_config()
        if "proxy" not in data:
            return None
        proxy_raw = data["proxy"]
        if not isinstance(proxy_raw, dict):
            raise ConfigError(
                f"proxy must be a mapping with type/host/port, got {type(proxy_raw).__name__} ({proxy_raw!r})"
            )
        if not proxy_raw:
            return None
        proxy_type = proxy_raw.get("type", "socks5")
        valid_types = ("socks5", "socks4", "http")
        if proxy_type not in valid_types:
            raise ConfigError(f"Unknown proxy type: {proxy_type!r}, expected one of {valid_types}")
        port = proxy_raw.get("port", 1080)
        if isinstance(port, bool) or not isinstance(port, int):
            raise ConfigError(f"proxy.port must be an integer, got {port!r}")
        host = proxy_raw.get("host", "127.0.0.1")
        if not isinstance(host, str) or not host:
            raise ConfigError(f"proxy.host must be a non-empty string, got {host!r}")
        return (
            proxy_type,
            host,
            port,
            proxy_raw.get("rdns", True),
            proxy_raw.get("username"),
            proxy_raw.get("password"),
        )

    def load_min_free_space(self) -> int:
        """Free space an export must keep, in bytes.

        Returns the default when the key is absent. A caller-side ``or`` used
        to swallow ``min_free_space: 0`` -- a deliberate way to switch the check
        off -- and silently substitute the default instead.
        """
        from tg_export.config import parse_size

        data = self.load_global_config()
        return parse_size(data.get("min_free_space", DEFAULT_MIN_FREE_SPACE))

    async def add_account(self, name: str):
        """Interactive Telethon login. Requires terminal interaction."""
        from telethon import TelegramClient
        from telethon.errors import SessionPasswordNeededError

        from tg_export.session import FixedSQLiteSession

        api_id, api_hash = self.load_credentials()
        target = self.session_path(name)
        # The login writes to a file of its own and takes the place of the
        # working session only once it succeeded. Deleting the old file first --
        # as this did -- meant a wrong code, a dropped connection or Ctrl+C at
        # the prompt left the account with no session at all, recoverable only
        # by another full login.
        staging = target.with_name(f".{target.name}.new.session")
        self._remove_session_files(staging)

        # FixedSQLiteSession, not the path: a bare path makes Telethon build a
        # plain SQLiteSession, which addresses the session columns by name while
        # they are written positionally. Login is the only writer outside the
        # subclass, so leaving it out keeps the file a source of corruption.
        client = TelegramClient(FixedSQLiteSession(str(staging)), api_id, api_hash)
        try:
            await client.connect()

            if not await client.is_user_authorized():
                phone = click.prompt("Phone number (with +)", type=str)
                sent = await client.send_code_request(phone)
                _notify(f"Code type: {type(sent.type).__name__}")
                _notify(f"Next type: {sent.next_type.__class__.__name__ if sent.next_type else 'none'}")
                _notify(f"Timeout: {sent.timeout}s" if sent.timeout else "No timeout")

                code = click.prompt("Enter code", type=str)
                try:
                    await client.sign_in(phone, code)
                except SessionPasswordNeededError:
                    await self._sign_in_with_password(client)

            me = await client.get_me()
        except BaseException:
            # Ctrl+C at a prompt arrives as KeyboardInterrupt, hence BaseException:
            # the staging file must not survive it either.
            await self._disconnect(client)
            self._remove_session_files(staging)
            raise
        await self._disconnect(client)

        self._remove_session_files(target)
        staging.replace(target)
        restrict_file(target)
        _notify(f"Logged in as: {getattr(me, 'first_name', '?')} (id={getattr(me, 'id', '?')})")

    @staticmethod
    async def _disconnect(client) -> None:
        """Close the client; disconnect() is sync on an unconnected client."""
        disc = client.disconnect()
        if disc is not None:
            await disc

    @staticmethod
    def _remove_session_files(path: Path) -> None:
        """Delete a session file together with the SQLite sidecars it may have."""
        for suffix in ("", "-journal", "-wal", "-shm"):
            candidate = Path(f"{path}{suffix}")
            if candidate.exists():
                candidate.unlink()

    @staticmethod
    async def _sign_in_with_password(client) -> None:
        """Ask for the two-factor password until it is accepted or attempts run out.

        The branch is chosen by exception type rather than by a substring of the
        class name: the project already dropped name matching in
        ``TgApi.start_takeout``, since a rename in Telethon breaks it silently --
        the user would get a traceback instead of a password prompt. Errors other
        than a wrong password now propagate on their own, without an `else: raise`.
        """
        from telethon.errors import PasswordHashInvalidError

        for attempt in range(_PASSWORD_ATTEMPTS):
            password = click.prompt("2FA password", hide_input=True, type=str)
            try:
                await client.sign_in(password=password)
                return
            except PasswordHashInvalidError:
                remaining = _PASSWORD_ATTEMPTS - 1 - attempt
                if remaining > 0:
                    _notify(f"Wrong password. {remaining} attempts left.")
                    continue
                _notify("Too many wrong attempts.")
                raise
