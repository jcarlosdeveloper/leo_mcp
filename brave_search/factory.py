"""
Search Factory - Fábrica de estrategias

Centraliza la creación de estrategias según el modo solicitado.
"""

from typing import Literal
from .strategies.fast_search import FastSearch
from .strategies.deep_research import DeepResearch
from .strategies.base import SearchStrategy

SearchMode = Literal['fast', 'deep']


class SearchFactory:
    """
    Fábrica para crear estrategias de búsqueda.
    
    Patrón Factory: centraliza la lógica de instanciación.
    """
    
    @staticmethod
    def get_strategy(mode: SearchMode) -> SearchStrategy:
        """
        Crea la estrategia apropiada según el modo.
        
        Args:
            mode: 'fast' para búsqueda rápida, 'deep' para investigación profunda
        
        Returns:
            Instancia de la estrategia seleccionada
        
        Raises:
            ValueError: Si el modo es desconocido
        """
        strategies = {
            'fast': FastSearch(),
            'deep': DeepResearch()
        }
        
        if mode not in strategies:
            raise ValueError(
                f"Modo desconocido: {mode}. "
                f"Usar 'fast' o 'deep'. "
                f"Modos disponibles: {list(strategies.keys())}"
            )
        
        logger = __import__('logging').getLogger(__name__)
        logger.info(f"Estrategia creada: {mode}")
        
        return strategies[mode]