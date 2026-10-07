"""
Application Settings
====================
Centralised configuration loaded from environment variables / .env file.
Uses pydantic-settings for type-safe, validated configuration.
"""

from pathlib import Path
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application-wide configuration.

    Values are read from environment variables first, then from a `.env` file
    located at the project root.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
    )

    # ── Application ──────────────────────────────────────────────────────
    app_name: str = "Geospatial File Measurement API"
    app_version: str = "2.0.0"
    debug: bool = False

    # ── Server ───────────────────────────────────────────────────────────
    host: str = "0.0.0.0"
    port: int = 8000

    # ── Database ─────────────────────────────────────────────────────────
    database_url: str = "sqlite+aiosqlite:///./data/geospatial.db"

    # ── File Storage ─────────────────────────────────────────────────────
    upload_dir: str = "./data/uploads"

    # ── File Constraints ─────────────────────────────────────────────────
    max_upload_size_mb: int = 50
    allowed_extensions: str = ".zip,.kml"

    # ── CRS / Projection ─────────────────────────────────────────────────
    # Fallback only. The primary strategy is per-dataset UTM selection; see
    # app.crs for why UTM is preferred over a global projection.
    default_projected_crs: str = "EPSG:3857"

    # ── CORS ─────────────────────────────────────────────────────────────
    # Comma-separated origins, or "*" to allow any. Tighten this in production.
    cors_origins: str = "*"

    # ── Derived helpers ──────────────────────────────────────────────────
    @property
    def max_upload_size_bytes(self) -> int:
        """Maximum upload size in bytes."""
        return self.max_upload_size_mb * 1024 * 1024

    @property
    def allowed_extensions_list(self) -> list[str]:
        """Return allowed extensions as a list."""
        return [ext.strip() for ext in self.allowed_extensions.split(",")]

    @property
    def cors_origins_list(self) -> list[str]:
        """Return allowed CORS origins as a list."""
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]

    @property
    def upload_path(self) -> Path:
        """Resolved upload directory path."""
        path = Path(self.upload_dir)
        path.mkdir(parents=True, exist_ok=True)
        return path


@lru_cache
def get_settings() -> Settings:
    """Return a cached Settings singleton."""
    return Settings()
