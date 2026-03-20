"""Account management with session storage."""

from __future__ import annotations

import os
from pathlib import Path

import yaml


class AccountManager:
    def __init__(self, config_dir: Path | None = None):
        self.config_dir = config_dir or Path.home() / ".config" / "tg-export"

    def ensure_dirs(self):
        self.sessions_dir.mkdir(parents=True, exist_ok=True)

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
        from telethon import TelegramClient

        api_id, api_hash = self.load_credentials()
        session_path = str(self.session_path(name))
        client = TelegramClient(session_path, api_id, api_hash)
        await client.start()
        await client.disconnect()
