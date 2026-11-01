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
    search: Optional[dict] = Field(
        default=None,
        description="This search's changes to Settings -> Job search (core.job_search: mode, job types, "
                    "minimum pay, exclusions, notes, ...); anything malformed falls back to the profile's value",
    )


class ApplyRequest(BaseModel):
    mode: Literal["easy", "external", "all"] = "easy"
    limit: Optional[int] = Field(default=None, ge=1, le=500)
    # None: the number set in Settings (Agent Settings → Jobs at the same time).
    workers: Optional[int] = Field(default=None, ge=1, le=4)  # 4 = core.config.MAX_PARALLEL_JOBS
    job_url: Optional[str] = None
    job_urls: Optional[list[str]] = Field(default=None, description="Specific job URLs to apply to (batch apply)")
    acknowledge_unknown: bool = Field(default=False, strict=True,
        description="The user checked the earlier uncertain submission and explicitly chose to retry")
    review: bool = Field(default=False, description="Accepted from older callers and ignored: Trial is the default "
                         "(choose with run_mode)")
    run_mode: Optional[Literal["auto", "trial", "training"]] = Field(
        default=None, description="auto (submits), or trial, the default (goes through every page like auto but never "
                    "activates the agent's final Submit; no approval prompts; 'training' is trial's old name)")


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
