from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class WebsiteScrapeStatusResponse(BaseModel):
    status: str = "not_started"
    website: str | None = None
    download_url: str | None = None
    message: str | None = None
    error: str | None = None

    model_config = ConfigDict(from_attributes=True)