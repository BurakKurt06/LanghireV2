"""Plugin-based job source system for LangHireV2."""
from .registry import PluginRegistry, PluginConfig

__all__ = ["PluginRegistry", "PluginConfig"]
