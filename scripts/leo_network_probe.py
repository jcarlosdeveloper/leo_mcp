#!/usr/bin/env python3
"""
Leo stream-transport probe: SSE vs chunked fetch.

Attaches to a live Brave (port 9222), finds the Leo conversation page, enables
the CDP Network domain, and classifies how Leo streams a generation while a
prompt is in flight. This is the SOLE research gate for Layer 3 (CDP Network
completion signal) and shapes the Layer 4 gate phrasing.

Classification:
  - SSE             -> Network.eventSourceMessageReceived fires per token/chunk;
                       Network.loadingFinished fires late or never for the stream.
  - chunked fetch   -> a fetch/XHR request's Network.loadingFinished fires cleanly
                       at end-of-generation.

Usage:
  1. Launch Brave with --remote-debugging-port=9222.
  2. python3 scripts/leo_network_probe.py
  3. The script opens a fresh Leo tab, sends a probe prompt, and records events.
"""

import asyncio
import json
import sys
import time

from playwright.async_api import async_playwright


PROBE_PROMPT = (
    "Reply with exactly: 'probe-ok' followed by a numbered list 1..5, "
    "each on its own line."
)
WATCH_SECONDS = 45


async def main() -> None:
    async with async_playwright() as p:
        browser = await p.chromium.connect_over_cdp("http://localhost:9222")
        ctx = browser.contexts[0] if browser.contexts else await browser.new_context()

        page = await ctx.new_page()
        cdp = await ctx.new_cdp_session(page)

        # Enable Network domain and register event listeners.
        await cdp.send("Network.enable")
        await cdp.send("Page.enable")

        events = {
            "requestWillBeSent": 0,
            "responseReceived": 0,
            "loadingFinished": 0,
            "loadingFailed": 0,
            "eventSourceMessageReceived": 0,
        }
        request_urls = []
        event_sources = []
        loading_finished_at = []

        async def on_request(params):
            events["requestWillBeSent"] += 1
            u = params.get("request", {}).get("url", "")
            if any(k in u for k in ("leo", "brave", "conversation", "api", "chat")):
                request_urls.append(u[:160])
                print(f"[request] {u[:160]}", flush=True)

        async def on_response(params):
            events["responseReceived"] += 1

        async def on_loading_finished(params):
            events["loadingFinished"] += 1
            loading_finished_at.append(time.monotonic())
            print(f"[loadingFinished] requestId={params.get('requestId', '')[:12]}", flush=True)

        async def on_loading_failed(params):
            events["loadingFailed"] += 1
            print(f"[loadingFailed] {params.get('errorText', '')}", flush=True)

        async def on_event_source(params):
            events["eventSourceMessageReceived"] += 1
            data = params.get("data", "")[:120]
            event_sources.append(data)
            print(f"[SSE event] {data}", flush=True)

        cdp.on("Network.requestWillBeSent", on_request)
        cdp.on("Network.responseReceived", on_response)
        cdp.on("Network.loadingFinished", on_loading_finished)
        cdp.on("Network.loadingFailed", on_loading_failed)
        cdp.on("Network.eventSourceMessageReceived", on_event_source)

        # Navigate to a fresh Leo conversation.
        await cdp.send("Page.navigate", {"url": "brave://leo-ai/chat"})
        await asyncio.sleep(4)

        # Focus input and send the probe prompt.
        try:
            frame = page.main_frame
            await frame.evaluate(
                """() => {
                    const el = document.querySelector('[contenteditable="true"]')
                        || document.querySelector('[data-testid="leo-input"]')
                        || document.querySelector('textarea');
                    if (el && el.focus) el.focus();
                }"""
            )
            await page.keyboard.insert_text(PROBE_PROMPT)
            await asyncio.sleep(0.3)
            await page.keyboard.press("Enter")
            print(f"\n✅ Probe prompt sent. Watching {WATCH_SECONDS}s...\n", flush=True)
        except Exception as e:
            print(f"⚠️ Could not auto-inject prompt ({e}). Send manually.", flush=True)

        await asyncio.sleep(WATCH_SECONDS)

        print("\n" + "=" * 60)
        print("EVENT COUNTS")
        print("=" * 60)
        print(json.dumps(events, indent=2))

        sse_count = events["eventSourceMessageReceived"]
        fetch_count = events["loadingFinished"]

        print("\n" + "=" * 60)
        print("CLASSIFICATION")
        print("=" * 60)
        if sse_count > 0:
            print(f"→ SSE detected ({sse_count} eventSourceMessageReceived events).")
            print("  Terminal token stays PRIMARY; loadingFinished is corroborating.")
        elif fetch_count > 0:
            print(f"→ Chunked FETCH detected ({fetch_count} loadingFinished events).")
            print("  loadingFinished can be the PRIMARY completion signal.")
        else:
            print("→ INCONCLUSIVE: no SSE events and no loadingFinished observed.")
            print("  Keep terminal token primary; treat loadingFinished as best-effort.")

        await browser.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(0)
