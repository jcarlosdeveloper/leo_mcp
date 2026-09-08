# brave_search/strategies/factory.py
"""
Fábrica para instanciar la estrategia de búsqueda correcta
según la petición del cliente MCP.
"""
import logging
from typing import Optional

from .base import SearchStrategy
from .fast_search import FastSearch
from .deep_research import DeepResearch

logger = logging.getLogger(__name__)


class SearchStrategyFactory:
    """
    Fábrica para instanciar la estrategia de búsqueda correcta 
    según la petición del cliente MCP.
    
    Patrón Factory Method:
    - Oculta la complejidad de instanciación
    - Proporciona fallback seguro
    - Logging de decisión
    """
    
    @staticmethod
    def get_strategy(mode: str = "fast") -> SearchStrategy:
        """
        Retorna la instancia de la estrategia solicitada.
        
        Args:
            mode (str): "fast" para FastSearch, "deep" para DeepResearch.
                        Por defecto es "fast".
                        
        Returns:
            SearchStrategy: Una instancia que cumple con la interfaz base.
        
        Examples:
            >>> strategy = SearchStrategyFactory.get_strategy("fast")
            >>> result = await strategy.execute(page, "query")
            
            >>> strategy = SearchStrategyFactory.get_strategy("deep")
            >>> result = await strategy.execute(page, "complex research")
        """
        mode_lower = mode.strip().lower()
        
        if mode_lower == "deep":
            logger.debug("Factory seleccionó: DeepResearch (6 min timeout, enable_research=true)")
            return DeepResearch()
        
        elif mode_lower == "fast":
            logger.debug("Factory seleccionó: FastSearch (60s timeout, ?q= URL param)")
            return FastSearch()
        
        else:
            # Fallback seguro por si el LLM inventa un modo
            logger.warning(f"Modo desconocido '{mode}'. Usando FastSearch como fallback.")
            return FastSearch()