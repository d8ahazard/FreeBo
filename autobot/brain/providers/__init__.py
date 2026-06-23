"""LLM providers for the Autobot brain. Provider-agnostic: any OpenAI-compatible Chat Completions endpoint."""

from .catalog import PROVIDERS, catalog_for_ui, get_provider
from .openai_compatible import ChatResult, OpenAICompatibleClient, ProviderError

__all__ = ["OpenAICompatibleClient", "ChatResult", "ProviderError",
           "PROVIDERS", "catalog_for_ui", "get_provider"]
