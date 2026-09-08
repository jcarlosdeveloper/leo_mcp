#!/usr/bin/env python3
"""
Prueba de Playwright con Brave en modo headless.
Extrae contenido de una URL sin abrir ventana visible.
"""
import asyncio
from playwright.async_api import async_playwright

BRAVE_PATH = "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser"

async def test_brave_headless(url: str = "https://example.com") -> dict:
    """
    Navega a una URL con Brave en modo headless y extrae contenido.
    
    Args:
        url: URL a navegar
        
    Returns:
        Dict con title, text, html_length, success, error
    """
    start_time = asyncio.get_event_loop().time()
    
    try:
        async with async_playwright() as p:
            # Lanzar Brave en modo headless
            browser = await p.chromium.launch(
                executable_path=BRAVE_PATH,
                headless=True,  # Sin ventana visible
                args=[
                    "--no-sandbox",
                    "--disable-gpu",
                    "--disable-dev-shm-usage",
                    "--disable-software-rasterizer",
                ]
            )
            
            # Crear página
            page = await browser.new_page()
            
            # Navegar y esperar carga completa
            await page.goto(url, wait_until="networkidle", timeout=30000)
            
            # Extraer contenido
            title = await page.title()
            text = await page.inner_text("body")
            html = await page.content()
            
            # Esperar un poco para asegurar que JS cargó
            await page.wait_for_timeout(1000)
            
            end_time = asyncio.get_event_loop().time()
            elapsed = end_time - start_time
            
            await browser.close()
            
            return {
                "success": True,
                "url": url,
                "title": title,
                "text_length": len(text),
                "html_length": len(html),
                "elapsed_seconds": round(elapsed, 2),
                "text_preview": text[:200] + "..." if len(text) > 200 else text,
            }
            
    except Exception as e:
        return {
            "success": False,
            "url": url,
            "error": str(e),
            "error_type": type(e).__name__,
        }


async def main():
    print("🧪 Testing Brave Headless con Playwright\n")
    print("=" * 60)
    
    # Test 1: URL simple
    print("\n📄 Test 1: example.com")
    result1 = await test_brave_headless("https://example.com")
    if result1["success"]:
        print(f"   ✅ Título: {result1['title']}")
        print(f"   📊 Texto: {result1['text_length']} chars")
        print(f"   📊 HTML: {result1['html_length']} chars")
        print(f"   ⏱️  Tiempo: {result1['elapsed_seconds']}s")
        print(f"   📝 Preview: {result1['text_preview']}")
    else:
        print(f"   ❌ Error: {result1['error']}")
    
    # Test 2: URL con JS
    print("\n📄 Test 2: brave.com (sitio real con JS)")
    result2 = await test_brave_headless("https://brave.com")
    if result2["success"]:
        print(f"   ✅ Título: {result2['title']}")
        print(f"   📊 Texto: {result2['text_length']} chars")
        print(f"   📊 HTML: {result2['html_length']} chars")
        print(f"   ⏱️  Tiempo: {result2['elapsed_seconds']}s")
    else:
        print(f"   ❌ Error: {result2['error']}")
    
    # Test 3: Múltiples URLs en paralelo
    print("\n📄 Test 3: Concurrencia (3 URLs en paralelo)")
    urls = [
        "https://example.com",
        "https://httpbin.org/html",
        "https://news.ycombinator.com",
    ]
    
    start_concurrent = asyncio.get_event_loop().time()
    tasks = [test_brave_headless(url) for url in urls]
    results = await asyncio.gather(*tasks)
    end_concurrent = asyncio.get_event_loop().time()
    
    total_time = end_concurrent - start_concurrent
    individual_time = sum(r.get("elapsed_seconds", 0) for r in results if r["success"])
    
    print(f"   URLs procesadas: {len([r for r in results if r['success']])}/{len(urls)}")
    print(f"   ⏱️  Tiempo total: {round(total_time, 2)}s")
    print(f"   ⏱️  Tiempo individual acumulado: {round(individual_time, 2)}s")
    speedup = round(individual_time / total_time, 2) if total_time > 0 else "N/A"
    print(f"   🚀 Speedup: {speedup}x")
    
    for i, (url, result) in enumerate(zip(urls, results), 1):
        status = "✅" if result["success"] else "❌"
        print(f"   {status} [{i}] {url}: {result.get('title', result.get('error', 'Unknown'))}")
    
    print("\n" + "=" * 60)
    print("✅ Tests completados")


if __name__ == "__main__":
    asyncio.run(main())