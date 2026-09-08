"""
DeepResearch Strategy - Investigación profunda (6 min timeout)
Patrón: asyncio.Task + Heartbeat Loop
- Evita cuelgues silenciosos
- Emite logs cada 15s para mostrar progreso
- Permite que el cliente MCP sepa que el servidor sigue vivo
"""
import asyncio
import logging
import time
from playwright.async_api import Page
from typing import Dict, Any

from .base import SearchStrategy
from ..pages.brave_search_page import BraveSearchPage

logger = logging.getLogger(__name__)


class DeepResearch(SearchStrategy):
    """
    Estrategia de investigación profunda con heartbeat.
    
    Timeout: 6 minutos (360 segundos)
    Use case: Research exhaustivo, análisis competitivo, documentación compleja
    
    Patrón: asyncio.Task + Heartbeat Loop
    - Evita cuelgues silenciosos
    - Emite logs cada 15s para mostrar progreso
    - Permite que el cliente MCP sepa que el servidor sigue vivo
    """
    
    # URL con parámetros para forzar la investigación exhaustiva
    URL = 'https://search.brave.com/ask?source=llmSuggest&enable_research=true'
    TIMEOUT = 360000  # 6 minutos
    HEARTBEAT_INTERVAL = 15.0  # 15 segundos entre logs de progreso
    
    async def execute(self, page: Page, prompt: str) -> Dict[str, Any]:
        """
        Ejecuta una investigación profunda con heartbeat.
        
        Args:
            page: Instancia de Playwright Page
            prompt: Prompt para enviar a Brave AI
        
        Returns:
            Dict con resultados estructurados
        """
        start_time = time.time()
        logger.info(f"Iniciando DeepResearch (Timeout max: {self.TIMEOUT/1000}s)")
        
        try:
            brave_page = BraveSearchPage(page)
            await brave_page.navigate_to(self.URL)
            
            # 1. Envolver la búsqueda real en una Task de asyncio
            search_task = asyncio.create_task(
                brave_page.execute_search(prompt, self.TIMEOUT)
            )
            
            # 2. Heartbeat Loop - evita cuelgues silenciosos
            elapsed = 0.0
            heartbeat_count = 0
            
            while not search_task.done():
                logger.info(
                    f"DeepResearch en progreso... "
                    f"[Tiempo transcurrido: ~{int(elapsed)}s]. "
                    f"Esperando a Brave AI."
                )
                heartbeat_count += 1
                
                # Esperar a que la tarea termine o pasen 15s
                done, _ = await asyncio.wait(
                    [search_task],
                    timeout=self.HEARTBEAT_INTERVAL
                )
                
                if done:
                    break  # La tarea terminó (éxito o error)
                
                elapsed = time.time() - start_time
            
            # 3. Recolectar resultado final (o lanzar excepción si falló)
            search_result = search_task.result()
            total_elapsed = time.time() - start_time
            
            logger.info(f"DeepResearch completado con éxito en {total_elapsed:.2f}s")
            
            # 4. Extraer metadata adicional
            metadata = await brave_page.get_metadata()
            
            return {
                'success': True,
                'result': search_result['text'],  # Extraer solo el texto
                'metadata': {
                    **metadata,
                    'execution_time_seconds': total_elapsed,
                    'scrape_duration_seconds': search_result.get('duration_seconds', 0),
                    'start_timestamp': search_result.get('start_time', start_time),
                    'end_timestamp': search_result.get('end_time', time.time()),
                    'heartbeat_logs_sent': heartbeat_count
                },
                'mode': 'deep',
                'elapsed_seconds': total_elapsed,
                'error': None
            }
            
        except asyncio.TimeoutError:
            total_elapsed = time.time() - start_time
            logger.error(f"DeepResearch superó el tiempo límite ({total_elapsed:.2f}s)")
            
            return {
                'success': False,
                'result': None,
                'metadata': {
                    'execution_time_seconds': total_elapsed,
                    'start_timestamp': start_time,
                    'end_timestamp': time.time(),
                    'timeout_reached': True
                },
                'mode': 'deep',
                'elapsed_seconds': total_elapsed,
                'error': f'Timeout after {total_elapsed:.2f}s (max: 360s)'
            }
            
        except Exception as e:
            total_elapsed = time.time() - start_time
            logger.error(f"DeepResearch falló: {str(e)}", exc_info=True)
            
            return {
                'success': False,
                'result': None,
                'metadata': {
                    'execution_time_seconds': total_elapsed,
                    'start_timestamp': start_time,
                    'end_timestamp': time.time(),
                    'error_type': type(e).__name__
                },
                'mode': 'deep',
                'elapsed_seconds': total_elapsed,
                'error': str(e)
            }