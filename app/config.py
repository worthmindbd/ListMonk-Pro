import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

# Repo root (app/config.py -> app/ -> repo root). Used as the default location
# for mutable JSON state files when DATA_DIR is not set (e.g. local dev).
_REPO_ROOT = Path(__file__).resolve().parent.parent


class Settings:
    listmonk_url: str = os.getenv("LISTMONK_URL", "http://localhost:9000")
    listmonk_user: str = os.getenv("LISTMONK_USER", "listmonk")
    listmonk_api_key: str = os.getenv("LISTMONK_API_KEY", "")

    # Directory for runtime-mutable JSON files (schedule, unsubscribe log/settings).
    # In Docker this points at a writable named volume; locally it falls back to
    # the repo root so existing files keep working.
    data_dir: str = os.getenv("DATA_DIR", str(_REPO_ROOT))

    # IMAP settings for unsubscribe monitoring
    imap_host: str = os.getenv("IMAP_HOST", "")
    imap_port: int = int(os.getenv("IMAP_PORT", "993"))
    imap_user: str = os.getenv("IMAP_USER", "")
    imap_pass: str = os.getenv("IMAP_PASS", "")
    imap_use_ssl: bool = os.getenv("IMAP_USE_SSL", "true").lower() == "true"

    # IMAP settings for bounce monitoring
    bounce_imap_host: str = os.getenv("BOUNCE_IMAP_HOST", "")
    bounce_imap_port: int = int(os.getenv("BOUNCE_IMAP_PORT", "993"))
    bounce_imap_user: str = os.getenv("BOUNCE_IMAP_USER", "")
    bounce_imap_pass: str = os.getenv("BOUNCE_IMAP_PASS", "")
    bounce_imap_use_ssl: bool = os.getenv("BOUNCE_IMAP_USE_SSL", "true").lower() == "true"

    @property
    def imap_configured(self) -> bool:
        return bool(self.imap_host and self.imap_user and self.imap_pass)

    @property
    def bounce_imap_configured(self) -> bool:
        return bool(self.bounce_imap_host and self.bounce_imap_user and self.bounce_imap_pass)

    @property
    def base_url(self) -> str:
        return self.listmonk_url.rstrip("/")

    def data_path(self, filename: str) -> Path:
        """Resolve a mutable JSON state file inside the writable data dir.

        Ensures the directory exists. Migrates a pre-existing file from the
        repo root (legacy location) into the data dir on first access so
        existing schedules/logs are preserved after the volume switch.
        """
        d = Path(self.data_dir)
        d.mkdir(parents=True, exist_ok=True)
        target = d / filename
        if not target.exists():
            legacy = _REPO_ROOT / filename
            if legacy.exists() and legacy.resolve() != target.resolve():
                try:
                    target.write_bytes(legacy.read_bytes())
                except OSError:
                    pass
        return target


settings = Settings()
