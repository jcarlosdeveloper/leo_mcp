#!/usr/bin/env python3
"""
Prueba FINAL: Selección de modelo + Inyección silenciosa
Basada en el enfoque que SÍ funcionó (test_simple_selector.py)
"""

import asyncio
from playwright.async_api import async_playwright

async def main():
    print("=" * 70)
    print("🧪 PRUEBA FINAL: Selección de Modelo + Inyección Silenciosa")
    print("=" * 70)
    
    async with async_playwright() as p:
        try:
            # 1. CONEXIÓN
            print("\n1. 🔌 Conectando a Brave (CDP port 9222)...")
            browser = await p.chromium.connect_over_cdp("http://localhost:9222")
            context = browser.contexts[0]
            print("   ✅ Conectado")
            
            # 2. PÁGINA DE LEO
            print("\n2. 📄 Buscando pestaña de Leo...")
            leo_page = None
            for page in context.pages:
                if "leo-ai" in page.url:
                    leo_page = page
                    print(f"   ✅ Página encontrada")
                    break
            
            if not leo_page:
                leo_page = await context.new_page()
                await leo_page.goto("brave://leo-ai/")
                await asyncio.sleep(2)
            
            await leo_page.wait_for_load_state("domcontentloaded")
            await asyncio.sleep(1)
            
            # 3. SELECCIÓN DE MODELO (XPath probado)
            print("\n3. 🎯 Seleccionando modelo: chat-claude-sonnet")
            
            MODEL_BUTTON_XPATH = 'xpath=//leo-buttonmenu[contains(.,"Automatic") or contains(.,"Claude") or contains(.,"Qwen")]'
            MODEL_ITEM = "leo-menu-item[data-key='chat-claude-sonnet']"
            
            # Click en botón de modelo
            model_button = await leo_page.wait_for_selector(MODEL_BUTTON_XPATH, state='visible', timeout=5000)
            await model_button.click()
            print("   ✅ Menú abierto")
            await asyncio.sleep(0.5)
            
            # Click en item (usar 'attached' porque puede estar oculto si ya está seleccionado)
            try:
                model_item = await leo_page.wait_for_selector(MODEL_ITEM, state='attached', timeout=3000)
                is_visible = await model_item.is_visible()
                
                if is_visible:
                    await model_item.click()
                    print(f"   ✅ Modelo seleccionado")
                    await asyncio.sleep(0.3)
                else:
                    print(f"   ℹ️  Modelo ya estaba seleccionado")
            except Exception as e:
                print(f"   ⚠️  Error: {e}")
            
            # Cerrar menú
            await leo_page.keyboard.press('Escape')
            await asyncio.sleep(0.3)
            
            # 4. INYECCIÓN DE PROMPT
            print("\n4. 📝 Inyectando prompt...")
            
            INPUT_SELECTOR = '[data-test-id="leo-input"]'
            prompt_text = "Hola, me gustaria saber mas de como implementar IA en mi dia a dia"
            
            input_field = await leo_page.wait_for_selector(INPUT_SELECTOR, state='visible', timeout=10000)
            await input_field.click()
            await asyncio.sleep(0.2)
            
            await leo_page.keyboard.insert_text(prompt_text)
            print("   ✅ Texto insertado")
            await asyncio.sleep(0.2)
            
            await leo_page.keyboard.press("Enter")
            print("   ✅ Enter presionado")
            await asyncio.sleep(0.5)
            
            print("\n" + "=" * 70)
            print("✅ ¡MISIÓN CUMPLIDA! Revisa tu pestaña de Brave.")
            print("=" * 70)
            
            # NO cerrar - dejar la página para que el usuario vea el resultado
            # await browser.close()  # En CDP, close() NO cierra la ventana real
            
        except Exception as e:
            print(f"\n❌ ERROR: {e}")
            import traceback
            traceback.print_exc()

if __name__ == "__main__":
    asyncio.run(main())