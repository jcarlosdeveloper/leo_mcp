#!/usr/bin/env python3
"""
Probe v4: target chrome://leo-ai specifically.
"""
import asyncio
import json
import datetime
from playwright.async_api import async_playwright

async def main():
    async with async_playwright() as p:
        browser = await p.chromium.connect_over_cdp("http://localhost:9222")
        ctx = browser.contexts[0]

        # Find Leo pages
        leo_pages = [p for p in ctx.pages if "chrome://leo-ai" in p.url]
        if not leo_pages:
            print("❌ No chrome://leo-ai page found.")
            return

        print(f"✅ Found {len(leo_pages)} Leo page(s):")
        for p in leo_pages:
            print(f"   {p.url}")

        leo_page = leo_pages[0]
        print(f"\nWatching: {leo_page.url}")
        print("Send a Leo prompt now. Watching for 120s...\n")

        last_state = None
        for _ in range(240):  # 240 × 0.5s = 120s
            try:
                state = None
                for frame in leo_page.frames:
                    try:
                        candidate = await frame.evaluate("""() => {
                            const stop   = document.querySelector('[data-testid="stop-generation-button"]');
                            const submit = document.querySelector('[data-testid="leo-submit-button"]');
                            const input  = document.querySelector('[contenteditable]');
                            return {
                                frame_url:       location.href.slice(0, 70),
                                stop_present:    !!stop,
                                submit_present:  !!submit,
                                submit_disabled: submit
                                    ? submit.className.includes('NIKQqyVXbPYDpHJBdK9Agg')
                                    : null,
                                input_editable:  input
                                    ? input.getAttribute('contenteditable')
                                    : null,
                            };
                        }""")
                        if candidate['submit_present'] or candidate['stop_present']:
                            state = candidate
                            break
                    except Exception:
                        continue

                if state is None:
                    state = {"error": "no frame with Leo UI found yet"}

            except Exception as e:
                state = {"error": str(e)}

            if state != last_state:
                ts = datetime.datetime.now().strftime('%H:%M:%S.%f')[:-3]
                print(f"[{ts}] STATE CHANGE:")
                print(json.dumps(state, indent=2))
                print()
                last_state = state

            await asyncio.sleep(0.5)

asyncio.run(main())