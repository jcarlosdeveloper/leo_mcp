"""
Strategies module - Strategy Pattern para búsquedas
"""
from .base import SearchStrategy
from .fast_search import FastSearch
from .deep_research import DeepResearch
from .factory import SearchStrategyFactory

__all__ = ['SearchStrategy', 'FastSearch', 'DeepResearch', 'SearchStrategyFactory']