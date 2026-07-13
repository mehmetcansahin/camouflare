from __future__ import annotations

from camouflare.app import create_app
from camouflare.config import Settings
from camouflare.observability import configure_logging

settings = Settings()
configure_logging(level=settings.log_level_value, log_format=settings.log_format)
app = create_app(settings=settings)
