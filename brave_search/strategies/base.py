"""
Base Strategy - Abstract Base Class para todas las estrategias de búsqueda
Garantiza que cualquier nueva estrategia cumpla con el mismo contrato.
"""
from abc import ABC, abstractmethod
from playwright.async_api import Page
from typing import Dict, Any


class SearchStrategy(ABC):
    """
    Interfaz abstracta para todas las estrategias de búsqueda en Brave.
    
    Contrato:
    - Debe aceptar una instancia de Page y un prompt
    - Debe retornar un dict estructurado con success, result, metadata, error
    - Debe manejar sus propios timeouts internamente
    - Debe hacer logging estructurado
    """
    
    @abstractmethod
    async def execute(self, page: Page, prompt: str) -> Dict[str, Any]:
        """
        Ejecuta una búsqueda y retorna resultados estructurados.
        
        Args:
            page: Instancia de Playwright Page
            prompt: Prompt para enviar a Brave AI
        
        Returns:
            Dict con:
                success (bool): Si la búsqueda tuvo éxito
                result (str|None): Texto de la respuesta o None
                metadata (dict|None): Metadata adicional o None
                mode (str): Modo de búsqueda ('fast', 'deep', etc.)
                elapsed_seconds (float): Tiempo total de ejecución
                error (str|None): Mensaje de error o None
        """
        pass