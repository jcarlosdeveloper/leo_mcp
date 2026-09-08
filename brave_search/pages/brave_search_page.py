"""
Brave Search Page Object Model

Encapsula la interacción con la UI de Brave Search.
Selectores ordenados por prioridad:
1. IDs estáticos (#chatllm-*) - más estables
2. Clases semánticas (.prose, .answer-done)
3. Fallbacks genéricos
"""

import asyncio
import time
from playwright.async_api import Page
from typing import Optional, Dict, Any
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

import logging
logger = logging.getLogger(__name__)


class BraveSearchPage:
    """
    Page Object Model para Brave Search.
    
    Selectores:
    - Prioridad 1: Aria labels y selectores semánticos (apps Svelte)
    - Prioridad 2: IDs estáticos 
    - Prioridad 3: Clases semánticas
    - Prioridad 4: Fallbacks genéricos
    """
    
    # Selectores ordenados por prioridad
    # Prioridad 1: Aria labels y selectores semánticos (apps Svelte)
    # Prioridad 2: IDs estáticos 
    # Prioridad 3: Clases semánticas
    # Prioridad 4: Fallbacks genéricos
    
    # El textarea en Brave Ask usa aria-label='Search', NO tiene name='q'
    SEARCH_INPUT = 'textarea[aria-label="Search"], input[aria-label="Search"]'
    SUBMIT_BUTTON = 'button[type="submit"], button[aria-label="Search"], button[aria-label="Submit"]'
    
    # Detectar cuando la respuesta está completa
    RESPONSE_DONE = '.prose, [class*="answer"], [class*="response"]'
    
    # CONTENT_SELECTOR actualizado (Opción C: buscar el contenedor correcto)
    # Usamos .last en wait_for_ai_completion para agarrar la respuesta más reciente
    # Selector principal: ask-center-content (confirmado en debug)
    CONTENT_SELECTOR = '.ask-center-content, .prose, #chatllm-content, [class*="response-container"]'
    
    # Placeholders que indican que la IA aún está generando
    PLACEHOLDERS = ['searching', 'analyzing', 'answering', 'thinking', 'researching']
    
    def __init__(self, page: Page):
        """Inicializa el Page Object."""
        self.page = page
    
    async def navigate_to(self, url: str):
        """Navega a una URL específica."""
        logger.debug(f"Navegando a: {url}")
        await self.page.goto(url, wait_until='domcontentloaded')
    
    async def wait_for_ai_completion(self, timeout_ms: int) -> str:
        """
        Espera inteligentemente a que la IA de Brave termine de generar.
        Implementa: Espera táctica inicial, selectores estrictos y validación semántica.
        """
        tiempo_limite = timeout_ms / 1000.0
        start_time = asyncio.get_event_loop().time()
        
        # OPCIÓN A: Espera mínima táctica
        # Le damos a Brave 8 segundos de "respiro" para que pase la fase de 
        # "Searching...", "Analyzing..." y comience a escupir texto real.
        logger.debug("Aplicando espera táctica de 8s para saltar placeholders iniciales...")
        await asyncio.sleep(8.0)
        
        # OPCIÓN C: Buscar el elemento por contexto (usando .last)
        # Esto asegura que agarramos la respuesta actual y no la de una búsqueda anterior
        container_locator = self.page.locator(self.CONTENT_SELECTOR).last
        
        try:
            logger.debug("Esperando que el contenedor de la respuesta sea visible...")
            await container_locator.wait_for(state="visible", timeout=15000)
        except PlaywrightTimeoutError:
            logger.warning("Timeout esperando contenedor. Puede que la UI haya cambiado o la red esté lenta.")
            await asyncio.sleep(2)
        
        stable_count = 0
        # Aumentamos el umbral de estabilización a 6 iteraciones (3 segundos sin cambios)
        stable_threshold = 6 
        last_text = ""
        
        logger.debug("Iniciando bucle de estabilización semántica...")
        
        while True:
            elapsed = asyncio.get_event_loop().time() - start_time
            if elapsed > tiempo_limite:
                logger.warning(f"Timeout global alcanzado ({elapsed:.2f}s > {tiempo_limite:.2f}s)")
                break
            
            try:
                current_text = await container_locator.inner_text()
            except Exception:
                current_text = ""
            
            current_text_lower = current_text.strip().lower()
            
            # Detectar si seguimos en la fase de placeholders de Brave
            is_placeholder = any(current_text_lower.startswith(p) for p in self.PLACEHOLDERS)
            
            try:
                # Verificar si Brave inyectó explícitamente una clase de carga/streaming
                is_streaming = await container_locator.evaluate(
                    "el => el.classList.contains('streaming') || el.classList.contains('loading')"
                )
            except Exception:
                is_streaming = False
            
            if is_placeholder or is_streaming:
                logger.debug(f"Aún generando... placeholder={is_placeholder}, streaming={is_streaming}")
                stable_count = 0
                await asyncio.sleep(0.5)
                continue
            
            # OPCIÓN B: Estabilización semántica (Palabras y Párrafos)
            palabras = len(current_text.split())
            parrafos = current_text.count('\n\n')
            
            if current_text == last_text and palabras > 10:
                stable_count += 1
                logger.debug(f"Estable ({stable_count}/{stable_threshold}) | Palabras: {palabras} | Párrafos: {parrafos}")
                
                # Criterio de salida robusto:
                # Si el texto es sustancial (más de 50 palabras y múltiples párrafos), somos un poco más flexibles.
                # Si es muy corto, exigimos que pase el stable_threshold completo (3 segundos sin cambios).
                if (palabras > 50 and parrafos >= 2 and stable_count >= 4) or (stable_count >= stable_threshold):
                    logger.debug("✅ Respuesta de IA completada y estabilizada.")
                    break
            else:
                stable_count = 0
                if current_text and current_text != last_text:
                    logger.debug(f"Texto creciendo: {len(last_text)} -> {len(current_text)} chars")
            
            last_text = current_text
            await asyncio.sleep(0.5)
        
        logger.info(f"Completado: {len(last_text)} chars en {elapsed:.2f}s")
        return last_text
    
    async def execute_search(self, prompt: str, timeout_ms: int) -> Dict[str, Any]:
        """
        Ejecuta búsqueda y retorna resultados con métricas.
        
        Args:
            prompt: Prompt para Brave AI
            timeout_ms: Timeout en milisegundos
        
        Returns:
            Dict con {text, start_time, end_time, duration_seconds}
        """
        start_time = time.time()
        logger.info(f"Iniciando búsqueda (timeout: {timeout_ms}ms)")
        
        # Ingresar prompt
        await self.page.fill(self.SEARCH_INPUT, prompt)
        
        # Enviar con Enter
        await self.page.press(self.SEARCH_INPUT, 'Enter')
        
        # Esperar generación de IA
        response_text = await self.wait_for_ai_completion(timeout_ms)
        
        end_time = time.time()
        
        return {
            'text': response_text,
            'start_time': start_time,
            'end_time': end_time,
            'duration_seconds': end_time - start_time
        }
    
    async def get_metadata(self) -> Dict[str, Any]:
        """Extrae metadata (URLs, fuentes, métricas)."""
        metadata = {
            'urls_analyzed': 0,
            'sources': [],
            'time_elapsed': None,
            'iterations': None
        }
        
        try:
            # Extraer métricas
            metric_selectors = ['.deep-research-metric', '[class*="metric"]']
            
            for selector in metric_selectors:
                try:
                    metrics = await self.page.query_selector_all(selector)
                    for metric in metrics:
                        text = await metric.inner_text()
                        if ':' in text:
                            key, value = text.split(':', 1)
                            key = key.strip().lower().replace(' ', '_')
                            value = value.strip()
                            try:
                                metadata[key] = float(value) if '.' in value else int(value)
                            except ValueError:
                                metadata[key] = value
                    break
                except Exception:
                    continue
            
            # Extraer fuentes
            sources = await self.page.query_selector_all('.source-item a, [class*="source"] a')
            urls = []
            for src in sources[:15]:
                try:
                    url = await src.get_attribute('href')
                    if url and url.startswith('http'):
                        urls.append(url)
                except Exception:
                    continue
            
            if urls:
                metadata['sources'] = urls
            
        except Exception as e:
            logger.warning(f"Error extrayendo metadata: {e}")
        
        return metadata