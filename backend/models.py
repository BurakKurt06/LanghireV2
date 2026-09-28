"""Pydantic request/response models for API input validation."""
from pydantic import BaseModel, Field
from typing import Literal, Optional


class CollectRequest(BaseModel):
    title: Optional[str] = None
    max_jobs: int = Field(default=0, ge=0, le=500)
    source: str = Field(default="linkedin", description="Plugin name to collect from")
    sources: Optional[list[str]] = Field(
        default=None,
        max_length=20,
        description="Plugin names to collect from, in order. Falls back to `source` when omitted.",
    )
    filters: dict = Field(default_factory=dict, description="Plugin-specific filter key-value pairs")


class ApplyRequest(BaseModel):
    mode: Literal["easy", "external", "all"] = "easy"
    limit: Optional[int] = Field(default=None, ge=1, le=500)
    workers: int = Field(default=1, ge=1, le=4)
    job_url: Optional[str] = None
    job_urls: Optional[list[str]] = Field(default=None, description="Specific job URLs to apply to (batch apply)")
    review: bool = Field(default=False, description="Trial run: wait for the user's approval before each submit")


class AddJobsBulkRequest(BaseModel):
    """Bulk-add job URLs from a pasted block or an uploaded file.

    Either field may be supplied; `text` is split on newlines. Both are capped
    so a stray large file cannot queue unbounded work.
    """
    urls: Optional[list[str]] = Field(default=None, max_length=1000)
    text: Optional[str] = Field(default=None, max_length=200_000)
    source: str = Field(default="pasted", max_length=40)


class CollectFilters(BaseModel):
    filters: dict = Field(default_factory=dict, description="Plugin-specific filter key-value pairs")


class DecayRequest(BaseModel):
    days: int = Field(default=30, ge=1, le=365)
    factor: float = Field(default=0.95, gt=0, le=1.0)


class CleanupRequest(BaseModel):
    threshold: float = Field(default=0.3, ge=0, le=1.0)


class PluginImportRequest(BaseModel):
    file_path: str = Field(description="Path to the .yaml plugin file to import")


class PluginToggleRequest(BaseModel):
    enabled: bool = Field(description="Whether the plugin should be enabled")
