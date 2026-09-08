"""
FastSearch Strategy - Búsqueda rápida (30s timeout)
Optimización: Usa summary=1 en la URL para forzar respuesta directa de la IA.
"""
import logging
import time
import urllib.parse
from playwright.async_api import Page
from typing import Dict, Any

from .base import SearchStrategy
from ..pages.brave_search_page import BraveSearchPage

logger = logging.getLogger(__name__)


class FastSearch(SearchStrategy):
    """
    Estrategia de búsqueda rápida.
    
    Timeout: 60 segundos (suficiente para respuestas simples)
    Use case: Respuestas directas, health checks
    
    Nota: Usa /ask con source=llmSuggest para mejor resultado
    """
    
    # URL con parámetros para mejor compatibilidad
    BASE_URL = 'https://search.brave.com/ask'
    TIMEOUT = 60000  # 60 segundos - más conservador
    
    async def execute(self, page: Page, prompt: str) -> Dict[str, Any]:
        """
        Ejecuta una búsqueda rápida.
        
        Args:
            page: Instancia de Playwright Page
            prompt: Prompt para enviar a Brave AI
        
        Returns:
            Dict con resultados estructurados
        """
        start_time = time.time()
        logger.info(f"Iniciando FastSearch (Timeout: {self.TIMEOUT/1000}s)")
        
        try:
            # Construir URL con el query
            # Usar source=llmSuggest mejora compatibilidad
            encoded_prompt = urllib.parse.quote(prompt)
            url = f"{self.BASE_URL}?q={encoded_prompt}&source=llmSuggest"
            
            logger.debug(f"Navegando a: {url[:80]}...")
            
            brave_page = BraveSearchPage(page)
            
            # La navegación ya dispara la búsqueda automáticamente por el parámetro ?q=
            await brave_page.navigate_to(url)
            
            # Solo esperamos a que la IA termine y calculamos el tiempo manualmente
            search_start = time.time()
            response_text = await brave_page.wait_for_ai_completion(self.TIMEOUT)
            search_end = time.time()
            
            result = {
                'text': response_text,
                'start_time': search_start,
                'end_time': search_end,
                'duration_seconds': search_end - search_start
            }
            
            metadata = await brave_page.get_metadata()
            
            elapsed = time.time() - start_time
            
            logger.info(f"FastSearch completado con éxito en {elapsed:.2f}s")
            
            return {
                'success': True,
                'result': result['text'],  # Extraer solo el texto del dict
                'metadata': {
                    **metadata,
                    'execution_time_seconds': elapsed,
                    'scrape_duration_seconds': result.get('duration_seconds', 0),
                    'start_timestamp': result.get('start_time', start_time),
                    'end_timestamp': result.get('end_time', time.time())
                },
                'mode': 'fast',
                'elapsed_seconds': elapsed,
                'error': None
            }
            
        except Exception as e:
            elapsed = time.time() - start_time
            logger.error(f"FastSearch falló: {str(e)}", exc_info=True)
            
            return {
                'success': False,
                'result': None,
                'metadata': {
                    'execution_time_seconds': elapsed,
                    'start_timestamp': start_time,
                    'end_timestamp': time.time()
                },
                'mode': 'fast',
                'elapsed_seconds': elapsed,
                'error': str(e)
            }