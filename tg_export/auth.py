"""Account management with session storage."""

from __future__ import annotations

import os
from pathlib import Path

import click
import yaml


class AccountManager:
    def __init__(self, config_dir: Path | None = None):
        self.config_dir = config_dir or Path.home() / ".config" / "tg-export"

    def ensure_dirs(self):
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
        return sorted(
            p.stem for p in self.sessions_dir.iterdir()
            if p.suffix == ".session"
        )

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
            "No --account specified and no default set. "
            "Use 'tg-export auth default <name>' to set one."
        )

    def save_credentials(self, api_id: int, api_hash: str):
        cred_path = self.config_dir / "api_credentials.yaml"
        data = {"api_id": api_id, "api_hash": api_hash}
        cred_path.write_text(yaml.dump(data, default_flow_style=False))
        os.chmod(cred_path, 0o600)

    def load_credentials(self) -> tuple[int, str]:
        cred_path = self.config_dir / "api_credentials.yaml"
        data = yaml.safe_load(cred_path.read_text())
        return data["api_id"], data["api_hash"]

    async def add_account(self, name: str):
        """Interactive Telethon login. Requires terminal interaction."""
        import logging
        from telethon import TelegramClient

        logging.basicConfig(level=logging.DEBUG)
        logging.getLogger("telethon").setLevel(logging.DEBUG)

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
            phone = input("Phone number (with +): ")
            sent = await client.send_code_request(phone)
            print(f"Code type: {sent.type.__class__.__name__}")
            print(f"Next type: {sent.next_type.__class__.__name__ if sent.next_type else 'none'}")
            print(f"Timeout: {sent.timeout}s" if sent.timeout else "No timeout")

            code = input("Enter code: ")
            try:
                await client.sign_in(phone, code)
            except Exception as e:
                if "SessionPasswordNeeded" in type(e).__name__:
                    import getpass
                    for attempt in range(3):
                        password = getpass.getpass("2FA password: ")
                        try:
                            await client.sign_in(password=password)
                            break
                        except Exception as e2:
                            if "PasswordHashInvalid" in type(e2).__name__:
                                remaining = 2 - attempt
                                if remaining > 0:
                                    print(f"Wrong password. {remaining} attempts left.")
                                else:
                                    print("Too many wrong attempts.")
                                    await client.disconnect()
                                    raise
                            else:
                                raise
                else:
                    raise

        me = await client.get_me()
        print(f"Logged in as: {me.first_name} (id={me.id})")
        await client.disconnect()
