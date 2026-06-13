"""Date-restricted search MCP tool."""

from .search import RESTRICTED_SEARCH_HOST, RestrictedSearchError, restricted_search

__all__ = ["RESTRICTED_SEARCH_HOST", "RestrictedSearchError", "restricted_search"]
