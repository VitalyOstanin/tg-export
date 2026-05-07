"""Account management with session storage."""

from __future__ import annotations

import contextlib
import logging
import os
from pathlib import Path

import click
import yaml

logger = logging.getLogger(__name__)


class CredentialsError(ValueError):
    """Raised when api_credentials.yaml is missing required fields or has bad types."""


class AccountManager:
    def __init__(self, config_dir: Path | None = None):
        self.config_dir = config_dir or Path.home() / ".config" / "tg-export"

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
        return self.sessions_dir / f"{name}.session"

    def config_path(self, name: str) -> Path:
        return self.config_dir / f"{name}.yaml"

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
        default_path.write_text(name)

    def get_default_account(self) -> str | None:
        """Get default account alias, or None."""
        default_path = self.config_dir / "default_account"
        if default_path.exists():
            return default_path.read_text().strip()
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
        cred_path.write_text(yaml.dump(data, default_flow_style=False))
        os.chmod(cred_path, 0o600)

    def load_credentials(self) -> tuple[int, str]:
        cred_path = self.config_dir / "api_credentials.yaml"
        if not cred_path.exists():
            raise CredentialsError(
                f"api_credentials.yaml not found at {cred_path}. "
                f"Run 'tg-export auth credentials' to create it."
            )
        # Warn if permissions are too loose; does not block to keep CI fixtures simple.
        try:
            mode = cred_path.stat().st_mode & 0o077
            if mode != 0:
                logger.warning(
                    "%s has too-permissive mode %o; tightening to 0o600",
                    cred_path,
                    mode,
                )
                with contextlib.suppress(OSError):
                    os.chmod(cred_path, 0o600)
        except OSError:
            pass
        try:
            data = yaml.safe_load(cred_path.read_text())
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
        """Load global config from config.yaml. Returns raw dict."""
        config_path = self.config_dir / "config.yaml"
        if not config_path.exists():
            return {}
        return yaml.safe_load(config_path.read_text()) or {}

    def load_proxy(self) -> tuple | None:
        """Load global proxy settings from config.yaml."""
        data = self.load_global_config()
        proxy_raw = data.get("proxy")
        if not proxy_raw or not isinstance(proxy_raw, dict):
            return None
        proxy_type = proxy_raw.get("type", "socks5")
        valid_types = ("socks5", "socks4", "http")
        if proxy_type not in valid_types:
            raise ValueError(f"Unknown proxy type: {proxy_type!r}, expected one of {valid_types}")
        return (
            proxy_type,
            proxy_raw.get("host", "127.0.0.1"),
            proxy_raw.get("port", 1080),
            proxy_raw.get("rdns", True),
            proxy_raw.get("username"),
            proxy_raw.get("password"),
        )

    def load_min_free_space(self) -> int | None:
        """Load min_free_space from global config. Returns bytes or None."""
        from tg_export.config import parse_size

        data = self.load_global_config()
        val = data.get("min_free_space")
        if val is None:
            return None
        return parse_size(val)

    async def add_account(self, name: str):
        """Interactive Telethon login. Requires terminal interaction."""
        from telethon import TelegramClient

        api_id, api_hash = self.load_credentials()
        session_path = str(self.session_path(name))

        # Удаляем старую сессию если есть (может быть битая)
        old = self.session_path(name)
        if old.exists():
            old.unlink()
        journal = old.with_suffix(".session-journal")
        if journal.exists():
            journal.unlink()

        client = TelegramClient(session_path, api_id, api_hash)
        await client.connect()

        if not await client.is_user_authorized():
            phone = click.prompt("Phone number (with +)", type=str)
            sent = await client.send_code_request(phone)
            click.echo(f"Code type: {type(sent.type).__name__}")
            click.echo(f"Next type: {sent.next_type.__class__.__name__ if sent.next_type else 'none'}")
            click.echo(f"Timeout: {sent.timeout}s" if sent.timeout else "No timeout")

            code = click.prompt("Enter code", type=str)
            try:
                await client.sign_in(phone, code)
            except Exception as e:
                if "SessionPasswordNeeded" in type(e).__name__:
                    for attempt in range(3):
                        password = click.prompt("2FA password", hide_input=True, type=str)
                        try:
                            await client.sign_in(password=password)
                            break
                        except Exception as e2:
                            if "PasswordHashInvalid" in type(e2).__name__:
                                remaining = 2 - attempt
                                if remaining > 0:
                                    click.echo(f"Wrong password. {remaining} attempts left.")
                                else:
                                    click.echo("Too many wrong attempts.")
                                    disc = client.disconnect()
                                    if disc is not None:
                                        await disc
                                    raise
                            else:
                                raise
                else:
                    raise

        me = await client.get_me()
        click.echo(f"Logged in as: {getattr(me, 'first_name', '?')} (id={getattr(me, 'id', '?')})")
        disc = client.disconnect()
        if disc is not None:
            await disc
