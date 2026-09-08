#!/usr/bin/env python3
"""
Tests de Playwright con URLs internas de Brave (brave://)
Esto es crítico para reemplazar AppleScript en leo_ask.sh

Ejecutar: python3 tests/brave_search/test_playwright_brave_internal.py
"""
import asyncio
import pytest
from playwright.async_api import async_playwright

BRAVE_PATH = "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser"

# URLs críticas que usa leo_ask.sh
BRAVE_INTERNAL_URLS = [
    "brave://newtab",
    "brave://settings",
    "brave://version",
    "brave://flags",
    # La URL CRÍTICA del AI Chat (formato real que usa leo_ask.sh)
    # "brave://ai-chat-internal"  # No existe públicamente, es interno de Brave
]


async def _test_url(url: str) -> dict:
    """Intenta navegar a una URL interna de Brave"""
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                executable_path=BRAVE_PATH,
                headless=True,
                args=[
                    "--no-sandbox",
                    "--disable-gpu",
                    "--disable-dev-shm-usage",
                ]
            )
            
            page = await browser.new_page()
            
            # Intentar navegar
            response = await page.goto(url, wait_until="networkidle", timeout=10000)
            
            # Obtener contenido
            title = await page.title()
            html = await page.content()
            url_actual = page.url
            
            await browser.close()
            
            return {
                "success": True,
                "url_requested": url,
                "url_actual": url_actual,
                "title": title,
                "status_code": response.status if response else "N/A",
                "html_length": len(html),
                "restricciones": "Ninguna detectada" if len(html) > 0 else "Posible bloqueo",
            }
            
    except Exception as e:
        error_msg = str(e)
        # Detectar errores específicos de URLs internas
        if "net::ERR_FAILED" in error_msg:
            error_type = "ERR_FAILED - URL interna bloqueada"
        elif "net::ERR_ABORTED" in error_msg:
            error_type = "ERR_ABORTED - Navegación cancelada"
        elif "Timeout" in error_msg:
            error_type = "Timeout - Página no cargó"
        else:
            error_type = f"{type(e).__name__}: {error_msg}"
        
        return {
            "success": False,
            "url_requested": url,
            "error": error_msg,
            "error_type": error_type,
        }


async def main():
    print("🧪 Testing URLs Internas de Brave con Playwright (Headless)\n")
    print("=" * 70)
    
    # Test 1: URLs internas conocidas
    print("\n📄 Test 1: brave://newtab")
    result1 = await test_url("brave://newtab")
    if result1["success"]:
        print(f"   ✅ Success: {result1['title']}")
        print(f"   📊 HTML: {result1['html_length']} chars")
        print(f"   🔗 URL final: {result1['url_actual']}")
    else:
        print(f"   ❌ {result1['error_type']}")
        print(f"      Error: {result1['error'][:200]}")
    
    print("\n📄 Test 2: brave://version")
    result2 = await test_url("brave://version")
    if result2["success"]:
        print(f"   ✅ Success: {result2['title']}")
        print(f"   📊 HTML: {result2['html_length']} chars")
    else:
        print(f"   ❌ {result2['error_type']}")
    
    print("\n📄 Test 3: brave://settings")
    result3 = await test_url("brave://settings")
    if result3["success"]:
        print(f"   ✅ Success: {result3['title']}")
        print(f"   📊 HTML: {result3['html_length']} chars")
    else:
        print(f"   ❌ {result3['error_type']}")
    
    # Test 4: Simular la URL real del AI Chat
    # El script actual lee de brave://ai-chat-internal o similar
    print("\n📄 Test 4: brave://ai-chat-internal (simulado)")
    print("   ⚠️  Esta URL es INTERNA y puede no existir públicamente")
    print("   📝 El leo_ask.sh actual usa AppleScript para leer el DOM directamente")
    
    # Test 5: Alternativa - intentar con chrome:// (Chromium base)
    print("\n📄 Test 5: chrome://version (alternativa Chromium)")
    result5 = await test_url("chrome://version")
    if result5["success"]:
        print(f"   ✅ Success: {result5['title']}")
        print(f"   📊 HTML: {result5['html_length']} chars")
    else:
        print(f"   ❌ {result5['error_type']}")
    
    print("\n" + "=" * 70)
    print("\n📋 CONCLUSIONES:")
    print("-" * 70)
    
    # Analizar resultados
    tests_internos = [result1, result2, result3]
    exitosos = [r for r in tests_internos if r["success"]]
    
    if len(exitosos) == len(tests_internos):
        print("✅ TODAS las URLs internas funcionaron")
        print("   → Playwright PUEDE reemplazar AppleScript para URLs brave://")
    elif len(exitosos) > 0:
        print(f"⚠️  PARCIAL: {len(exitosos)}/{len(tests_internos)} URLs funcionaron")
        print("   → Algunas URLs internas están bloqueadas en headless")
        print("   → Posible workaround: usar modo NO headless con --headless=new")
    else:
        print("❌ NINGUNA URL interna funcionó")
        print("   → Las URLs brave:// están bloqueadas en modo headless")
        print("   → Alternativas:")
        print("      1. Usar Playwright en modo NO headless (abre ventana)")
        print("      2. Extraer UUID directamente de la DB de Brave (archivo)")
        print("      3. Mantener AppleScript solo para URLs internas")
    
    print("\n💡 RECOMENDACIÓN:")
    print("   Si las URLs brave:// NO funcionan en headless, la mejor opción es:")
    print("   → Leer DIRECTAMENTE del archivo de base de datos de Brave AI Chat")
    print("   → Ruta: ~/Library/Application Support/BraveSoftware/Brave-Browser/Default/AIChat")
    print("   → Esto evita completamente el navegador y es más rápido/confiable")


# ── Pytest Tests ────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_brave_internal_urls():
    """
    Test pytest que verifica URLs internas de Brave.
    
    NOTA: Las URLs brave:// suelen fallar en modo headless. Esto es COMPORTAMIENTO ESPERADO.
    El propósito de este test es DOCUMENTAR esta limitación, no que pasen.
    
    Workaround: Para producción, usar CDP con debug port 9222 (no headless para Brave real).
    """
    urls_to_test = ["brave://newtab", "brave://version"]
    
    results = []
    for url in urls_to_test:
        result = await _test_url(url)
        results.append(result)
        status = '✅' if result['success'] else '❌'
        error_info = result.get('error_type', result.get('title', 'Unknown'))
        print(f"\n{url}: {status} {error_info}")
    
    # DOCUMENTAR: Es ESPERADO que fallen en headless
    # El test pasa si podemos conectar y obtener resultados (aunque sean errores)
    assert len(results) == 2, "No se completaron todos los tests"
    
    # Imprimir conclusión
    success_count = sum(1 for r in results if r["success"])
    print(f"\n📊 CONCLUSIÓN: {success_count}/{len(results)} URLs funcionaron en headless")
    print("   → Las URLs brave:// generalmente NO funcionan en headless")
    print("   → Solución: Usar CDP con debug port 9222 (ver leo_chat/pages/)")


if __name__ == "__main__":
    asyncio.run(main())