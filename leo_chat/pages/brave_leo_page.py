"""
Brave Leo Page Object Model (POM)

Background automation of your own Brave Leo instance via Playwright/CDP.
Connects to a Brave instance you launched with --remote-debugging-port=9222.

Exact flow:
1. Connect to Brave via debug port 9222
2. Open NEW tab with brave://leo-ai
3. Select model (leo-buttonmenu → leo-menu-item[data-key='{model_key}'])
4. Inject prompt into [data-test-id="leo-input"]
5. Send
6. Extract UUID from URL → poll SQLite
7. Close only the opened tab, preserve the Brave window
"""

import asyncio
import json
import logging
import os
import re
import socket
import time
from typing import Optional, Dict, List

from playwright.async_api import async_playwright, Browser, BrowserContext, Page

from leo_chat.selectors import load_selectors
from leo_chat.model_registry import load_model_registry, MODEL_REGISTRY_PATH
from leo_chat.helpers import _log_json

logger = logging.getLogger(__name__)


def _page_phase(event: str, t0: Optional[float], **kwargs) -> None:
    """Emit a structured page-level phase event with elapsed-ms since t0."""
    elapsed = int((time.monotonic() - t0) * 1000) if t0 is not None else 0
    _log_json("INFO", event, elapsed_ms=elapsed, **kwargs)


class BraveLeoPage:
    """
    Page Object Model for Brave Leo background automation.

    Selectors are resolved from ``selectors.toml`` (project root) via
    :func:`leo_chat.selectors.load_selectors`, but are mirrored as class
    attributes for backward compatibility. Model display names come from
    ``models.toml`` via :func:`leo_chat.model_registry.load_model_registry`.

    Usage:
        async with BraveLeoPage() as leo:
            await leo.connect()
            ok = await leo.inject_and_send("What is Python?", model_key="chat-claude-sonnet")
            response = await leo.wait_for_response()
    """

    DEBUG_PORT = 9222
    DEBUG_URL = f"http://localhost:{DEBUG_PORT}"
    LEO_URL = "brave://leo-ai"

    BRAVE_PROFILE = os.environ.get("BRAVE_PROFILE", "Default")

    # --- Selectors (prefer data-testid; avoid language-dependent attributes) ---
    # Backward-compatible class attributes, resolved from selectors.toml.
    # `MODEL_BUTTON` is a legacy alias kept because an older test referenced it.
    MODEL_BUTTON_SELECTOR = load_selectors()["model_button"]
    MODEL_BUTTON = MODEL_BUTTON_SELECTOR
    MODEL_ITEM = load_selectors()["model_item"]
    INPUT_FIELD = load_selectors()["input_field"]
    # Language-independent: target by icon, not by localized title
    SEND_BUTTON = load_selectors()["send_button"]
    SHOW_ALL_MODELS = load_selectors()["show_all_models"]
    RESPONSE_SELECTOR = load_selectors()["response_selector"]
    CONVERSATION_IFRAME = load_selectors()["conversation_iframe"]

    _UUID_PATTERN = r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}'

    # Leo input character limit (conservative; observed cap ~20k)
    LEO_MAX_PROMPT_CHARS = 18_000

    def __init__(self):
        self.browser: Optional[Browser] = None
        self.context: Optional[BrowserContext] = None
        self.page: Optional[Page] = None
        self.conversation_uuid: Optional[str] = None
        self._last_model_key: str = "chat-automatic"
        self._baseline_rowid: int = -1
        self._pw = None

    # ─────────────────────────────────────────────
    # Context manager
    # ─────────────────────────────────────────────

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.close()
        return False  # do not suppress exceptions

    # ─────────────────────────────────────────────
    # Connection
    # ─────────────────────────────────────────────

    async def connect(self, max_attempts: int = 3, base_delay: float = 2.0) -> bool:
        """
        Resilient CDP connection to Brave with retries and exponential backoff.
        Verifies the CDP port is open BEFORE attempting connect_over_cdp.
        """
        for attempt in range(1, max_attempts + 1):
            try:
                logger.info(
                    f"🔌 Connecting to Brave at {self.DEBUG_URL} "
                    f"(attempt {attempt}/{max_attempts})..."
                )

                if not self._is_cdp_port_open(self.DEBUG_PORT):
                    logger.warning(f"⚠️ CDP port {self.DEBUG_PORT} is closed.")
                    if attempt < max_attempts:
                        await asyncio.sleep(base_delay * (2 ** (attempt - 1)))
                        continue
                    logger.error(
                        "❌ CDP port never opened. Is Brave running with "
                        "--remote-debugging-port=9222?"
                    )
                    return False

                self._pw = await async_playwright().start()
                self.browser = await self._pw.chromium.connect_over_cdp(
                    self.DEBUG_URL, timeout=30000
                )
                logger.info("✅ Connected via connect_over_cdp")

                contexts = self.browser.contexts
                if not contexts:
                    logger.error("❌ No browser contexts found.")
                    await self._partial_cleanup()
                    if attempt < max_attempts:
                        await asyncio.sleep(base_delay * (2 ** (attempt - 1)))
                        continue
                    return False

                self.context = contexts[0]

                await self.context.add_init_script(
                    "Object.defineProperty(navigator, 'webdriver', "
                    "{ get: () => undefined });"
                )

                # Active wait for targets to sync
                pages = self.context.pages
                wait_attempts = 0
                while not pages and wait_attempts < 10:
                    await asyncio.sleep(0.5)
                    pages = self.context.pages
                    wait_attempts += 1

                logger.info(
                    f"📑 {len(contexts)} contexts, {len(pages)} pages after sync."
                )
                self.page = None
                logger.info("✅ CDP connection established and validated")
                return True

            except Exception as e:
                logger.error(f"❌ Connection error (attempt {attempt}): {e}")
                await self._partial_cleanup()
                if attempt < max_attempts:
                    await asyncio.sleep(base_delay * (2 ** (attempt - 1)))
                else:
                    logger.error("❌ All connection retries exhausted.")
                    return False

        return False

    @staticmethod
    def _is_cdp_port_open(port: int, timeout: float = 1.0) -> bool:
        """Quickly check whether the CDP port accepts connections."""
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(timeout)
            return s.connect_ex(('127.0.0.1', port)) == 0

    async def _partial_cleanup(self):
        """Clean up the Playwright driver between failed retries (leaves Brave intact)."""
        try:
            if self.browser:
                await self.browser.close()
        except Exception:
            pass
        try:
            if self._pw:
                await self._pw.stop()
        except Exception:
            pass
        self.browser = None
        self.context = None
        self._pw = None

    # ─────────────────────────────────────────────
    # Page management
    # ─────────────────────────────────────────────

    async def get_or_create_leo_page(self) -> Page:
        """
        Always opens a NEW tab with brave://leo-ai using a direct CDP session
        (bypasses Playwright's block on the brave:// scheme).

        Returns:
            Playwright Page pointing to brave://leo-ai/<UUID>
        """
        if not self.context:
            raise RuntimeError("Not connected. Call connect() first.")

        logger.debug("🆕 Opening new tab for Leo...")
        self.page = await self.context.new_page()

        cdp_session = await self.context.new_cdp_session(self.page)
        logger.debug("🔌 Sending CDP navigation command (Page.navigate)...")
        await cdp_session.send("Page.navigate", {"url": self.LEO_URL})

        logger.debug("⏳ Waiting for Brave to assign UUID and render...")
        _url_wait_t0 = time.monotonic()
        try:
            await self.page.wait_for_url(
                "**/leo-ai/*", timeout=15000, wait_until="domcontentloaded"
            )
        except Exception as e:
            logger.warning(
                f"⚠️ Timeout waiting for URL with UUID: {e}. Continuing anyway..."
            )
            await asyncio.sleep(3)
        _page_phase("leo_page_phase", _url_wait_t0, phase="wait_for_url",
                    url=self.page.url)

        logger.debug(f"✅ New Leo tab ready: {self.page.url}")
        return self.page

    async def _ensure_page_alive(self) -> Page:
        """
        Guarantee a live Leo page exists. Recreates the tab if it was closed
        (e.g. after a failed retry that nulled self.page). This fixes the
        chunking bug where is_first=False retries crashed on a None page.

        Returns:
            A live Playwright Page on brave://leo-ai/<UUID>.
        """
        if self.page is None or self.page.is_closed():
            logger.debug("♻️ No live page found — recreating Leo tab.")
            await self.get_or_create_leo_page()
        return self.page

    # ─────────────────────────────────────────────
    # Input sanitization
    # ─────────────────────────────────────────────

    def _sanitize_prompt(self, prompt: str) -> str:
        """
        Light cleanup only. No HTML-escaping: text is injected via
        keyboard.insert_text() (plain text into a contenteditable), not via
        innerHTML. Escaping previously corrupted quotes and symbols.
        """
        if not prompt:
            return prompt
        return prompt.strip()

    async def _extract_conversation_uuid(self) -> Optional[str]:
        """
        Extract the UUID from the page URL (brave://leo-ai/<UUID>).
        The URL is the most reliable source for a clean UUID string.
        """
        try:
            url = self.page.url
            m = re.search(self._UUID_PATTERN, url)
            if m:
                uuid = m.group(0)
                logger.debug(f"✅ UUID from URL: {uuid[:8]}...")
                return uuid
            logger.warning(f"⚠️ No UUID found in URL: {url}")
            return None
        except Exception as e:
            logger.warning(f"Error extracting UUID from URL: {e}")
            return None


    # ─────────────────────────────────────────────
    # Model selection
    # ─────────────────────────────────────────────

    async def _get_current_model_text(self) -> Optional[str]:
        """Read the text of the currently selected model button."""
        try:
            model_button = await self.page.wait_for_selector(
                self.MODEL_BUTTON_SELECTOR, state="visible", timeout=3000
            )
            if model_button:
                return (await model_button.inner_text()).strip()
            return None
        except Exception as e:
            logger.debug(f"Could not read current model: {e}")
            return None

    async def _change_model(self, model_key: str, target_name: str) -> bool:
        """
        Open the model menu and select the desired model.

        Args:
            model_key: Model key (e.g. "chat-claude-sonnet").
            target_name: Human-readable name (e.g. "claude sonnet").

        Returns:
            True always — model-selection errors are non-fatal; we fall back
            to whatever model is currently active.
        """
        try:
            logger.debug(f"   🔄 Changing model to: {model_key}")

            model_button = await self.page.wait_for_selector(
                self.MODEL_BUTTON_SELECTOR, state="visible", timeout=5000
            )
            if not model_button:
                logger.warning("⚠️ Model button not found")
                return True

            await model_button.click()
            await asyncio.sleep(0.5)
            logger.debug("   ✅ Menu opened")

            model_item_selector = self.MODEL_ITEM.format(model_key=model_key)

            # First attempt: model is directly visible in the menu
            if await self._try_click_model_item(model_item_selector, model_key):
                return True

            # Second attempt: expand via "Show all models" then retry
            logger.debug("   📋 Trying: 'Show all models'...")
            try:
                show_all_button = await self.page.wait_for_selector(
                    self.SHOW_ALL_MODELS, state="visible", timeout=2000
                )
            except Exception:
                show_all_button = None

            if show_all_button:
                await show_all_button.click()
                await asyncio.sleep(0.5)
                logger.debug("   ✅ 'Show all models' clicked")
                if await self._try_click_model_item(
                    model_item_selector, model_key, via_show_all=True
                ):
                    return True
            else:
                logger.warning("   ⚠️ 'Show all models' button not found")

            # Close menu if still open
            await self.page.keyboard.press("Escape")
            await asyncio.sleep(0.2)
            return True

        except Exception as e:
            logger.warning(f"⚠️ Error in _change_model: {e}")
            return True

    async def _try_click_model_item(
        self, selector: str, model_key: str, via_show_all: bool = False
    ) -> bool:
        """
        Attempt to locate and click a model menu item.

        Args:
            selector: The leo-menu-item selector for the target model.
            model_key: Model key (for logging).
            via_show_all: Whether this attempt is after expanding all models.

        Returns:
            True if the item was found and clicked, False otherwise.
        """
        suffix = " (show all)" if via_show_all else ""
        try:
            model_item = await self.page.wait_for_selector(
                selector, state="visible", timeout=3000
            )
            if model_item:
                await model_item.click()
                await asyncio.sleep(0.3)
                logger.debug(f"   ✅ Model {model_key} selected{suffix}")
                return True
            logger.warning(f"   ⚠️ Model {model_key} not found in menu{suffix}")
        except Exception as e:
            logger.warning(f"   ⚠️ Error selecting model{suffix}: {e}")
        return False

    async def select_model(self, model_key: str) -> bool:
        """
        Select the model using the real Brave Leo DOM.

        Strategy:
        1. If aria-selected already marks this model → skip.
        2. Read current model button text; if it matches target → skip.
        3. Otherwise open the menu and select.

        Returns:
            True if successful or already selected; True also on soft failure
            (we continue with the default model rather than aborting).
        """
        await self._ensure_page_alive()

        # Fast path: already selected (aria-selected)
        try:
            already = self.page.locator(
                f"leo-menu-item[data-key='{model_key}'][aria-selected='true']"
            )
            if await already.count() > 0:
                logger.debug(f"✅ Model '{model_key}' already active (aria-selected).")
                return True
        except Exception as e:
            logger.debug(f"Could not read aria-selected: {e}")

        try:
            logger.debug(f"🎯 Verifying model: {model_key}")

            current_model_text = await self._get_current_model_text()
            logger.debug(f"   Current model: {current_model_text}")

            model_name_map = _load_model_registry()
            target_name = model_name_map.get(model_key, model_key)
            logger.debug(f"   Target name: {target_name}")

            if current_model_text and target_name.lower() in current_model_text.lower():
                logger.debug(f"   ✅ Model '{model_key}' is already selected")
                return True

            logger.debug(
                f"   📋 Current model ({current_model_text}) != target ({target_name})"
            )
            return await self._change_model(model_key, target_name)

        except Exception as e:
            logger.warning(f"⚠️ Error in select_model: {e}. Continuing with default.")
            return True

    # ─────────────────────────────────────────────
    # Busy-state detection (unified)
    # ─────────────────────────────────────────────

    async def _is_leo_busy(self) -> bool:
        """Return True if Leo is currently generating, False if ready to accept input.

        The ONLY reliable, language-independent "generating" signal is the icon
        swap on the submit button: idle/send shows ``leo-icon name="arrow-up"``,
        while Leo swaps it to a stop glyph  while generating. ``disabled`` is
        NOT a generating signal — it is true both while generating AND while the
        input is empty (idle), so checking it false-positives "busy" forever and
        deadlocks every between-chunk / follow-up wait.

        We therefore read the inner ``<leo-icon name=...>``: ``arrow-up`` (or no
        button at all) means idle/ready; any other name (e.g. ``stop``) means
        Leo is mid-generation.
        """
        if not self.page or self.page.is_closed():
            return True
        try:
            return await self.page.evaluate("""() => {
                const btn = document.querySelector('[data-testid="leo-submit-button"]');
                if (!btn) return false;
                const icon = btn.querySelector('leo-icon');
                if (!icon) return false;
                return icon.getAttribute('name') !== 'arrow-up';
            }""")
        except Exception:
            return True

    # ─────────────────────────────────────────────
    # Core injection
    # ─────────────────────────────────────────────

    async def inject_and_send(
        self,
        final_prompt: str,
        model_key: str = "chat-claude-sonnet",
        max_attempts: int = 2,
        is_first: bool = True, ) -> bool:
        """
        Open a Leo tab, optionally select a model, inject a prompt, and send.

        If the prompt exceeds LEO_MAX_PROMPT_CHARS it is split into chunks and
        sent sequentially over the same conversation (same UUID), preserving
        context while bypassing Leo's input limit.

        Args:
            final_prompt: The prompt text to inject.
            model_key: Model key for Leo model selection.
            max_attempts: Retry attempts per message on failure.
            is_first: If False, skips model selection (continuing an existing
                conversation). The page is always ensured alive regardless.
        """
        if not self.browser:
            logger.error("❌ Not connected. Call connect() first.")
            return False

        sanitized_prompt = self._sanitize_prompt(final_prompt)
        if sanitized_prompt != final_prompt:
            logger.info("Prompt sanitized for safety.")
        final_prompt = sanitized_prompt

        if len(final_prompt) > self.LEO_MAX_PROMPT_CHARS:
            logger.info(
                f"📦 Prompt exceeds {self.LEO_MAX_PROMPT_CHARS} chars "
                f"({len(final_prompt)} total) — chunking over same conversation."
            )
            return await self._inject_chunked(
                final_prompt=final_prompt,
                model_key=model_key,
                max_attempts=max_attempts,
            )

        return await self._send_one_message(
            final_prompt=final_prompt,
            model_key=model_key,
            max_attempts=max_attempts,
            is_first=is_first,
        )


    async def _send_one_message(
        self,
        final_prompt: str,
        model_key: str,
        max_attempts: int,
        is_first: bool, ) -> bool:
        """
        Inject one message into the current Leo page and send it.

        FIX 1: page liveness is guaranteed via _ensure_page_alive() on EVERY
        attempt — including is_first=False — so a retry that nulled self.page
        no longer crashes the chunking flow.

        FIX 2: For follow-up chunks (is_first=False), we wait until Leo is truly
        idle before injecting — sending while Leo is busy puts the send button
        in "stop" state, causing click failures and corrupted chunk sequences.
        """
        for attempt in range(1, max_attempts + 1):
            try:
                logger.debug(
                    f"💉 Injecting prompt ({len(final_prompt)} chars) "
                    f"[attempt {attempt}/{max_attempts}]..."
                )

                # Always ensure a live page exists (fixes chunking-retry crash)
                await self._ensure_page_alive()

                _t0 = time.monotonic()

                # Guard: wait for Leo to be truly idle before injecting a
                # FOLLOW-UP message. When Leo is still generating, the send
                # button shows as "stop" (disabled for send), so injecting now
                # silently fails or stops generation.
                #
                # SKIPPED on the first turn: a freshly-opened tab has nothing
                # generating yet, and _is_leo_busy() false-positives while the
                # submit button is still mounting / in its default state,
                # causing a full _busy_ceiling (30s) dead wait on EVERY first
                # turn. Resume turns keep a bounded wait.
                if not is_first and await self._is_leo_busy():
                    logger.debug("⏳ Leo busy before inject — waiting to settle...")
                    _busy_wait_t0 = time.monotonic()
                    _busy_ceiling = 60
                    deadline = asyncio.get_event_loop().time() + _busy_ceiling
                    while await self._is_leo_busy():
                        if asyncio.get_event_loop().time() > deadline:
                            logger.warning(
                                    f"⚠️ Leo still busy after {_busy_ceiling}s — proceeding anyway"
                                )
                            break
                        await asyncio.sleep(1.0)
                    _page_phase("leo_page_phase", _busy_wait_t0, phase="busy_wait_settle")
                    logger.debug("✅ Leo settled — proceeding with inject")
                else:
                    _page_phase("leo_page_phase", _t0, phase="busy_wait_skipped",
                                is_first=is_first)

                # Snapshot the max rowid BEFORE sending so UUID resolution is
                # anchored to this request's entries and never collides with a
                # parallel instance. Uses get_max_rowid_any (covers both
                # character_type=0 and 1) as the single authoritative baseline.
                # Captured on EVERY turn (first or resume): on the resume
                # (multiturn) path get_uuid_by_user_entry(self._baseline_rowid)
                # must anchor above THIS turn's user entry, not the global
                # latest (which a parallel instance may have stolen). Leaving
                # it at the default -1 made resume turns resolve a foreign UUID.
                try:
                    from leo_chat.db.leo_read import get_max_rowid_any
                    loop = asyncio.get_event_loop()
                    self._baseline_rowid = await loop.run_in_executor(
                        None, get_max_rowid_any
                    )
                    logger.debug(f"📌 Baseline rowid = {self._baseline_rowid}")
                except Exception as e:
                    logger.warning(f"⚠️ Could not capture baseline rowid: {e}")
                    self._baseline_rowid = -1

                # Model selection only on the first message of a conversation
                if is_first:
                    _model_t0 = time.monotonic()
                    try:
                        await asyncio.wait_for(
                            self.select_model(model_key), timeout=5.0
                        )
                    except asyncio.TimeoutError:
                        logger.warning("⏱️ Model selection timed out, using default.")
                    except Exception as e:
                        logger.warning(
                            f"⚠️ Model selection failed ({e}), using default."
                        )
                    self._last_model_key = model_key
                    _page_phase("leo_page_phase", _model_t0, phase="model_selection")

                # Locate input
                input_locator = self.page.locator(self.INPUT_FIELD)
                try:
                    await input_locator.wait_for(state="attached", timeout=5000)
                except Exception as e:
                    logger.error(f"❌ Input field not found in DOM: {e}")
                    await self._capture_dom_snapshot(f"input_not_found_attempt{attempt}")
                    raise

                # Clear overlays/popups covering the input
                await self._dismiss_overlays()
                await self.page.keyboard.press("Escape")
                await asyncio.sleep(0.1)

                # Robust focus
                if not await self._focus_input(input_locator):
                    await self._capture_dom_snapshot(f"focus_failed_attempt{attempt}")
                    raise RuntimeError("Could not focus input")

                await asyncio.sleep(0.2)

                # Inject text
                await self.page.keyboard.insert_text(final_prompt)
                await asyncio.sleep(0.3)

                # Send: wait for the button to be enabled (not disabled), then
                # click without force so Playwright's actionability check catches
                # a still-disabled button rather than silently clicking
                # stop-generation. Uses `disabled` (stable) instead of the
                # build-specific CSS hash.
                try:
                    send_btn = self.page.locator(self.SEND_BUTTON).last
                    await self.page.wait_for_function(
                        """() => {
                            const btn = document.querySelector(
                                '[data-testid="leo-submit-button"]'
                            );
                            return btn && !btn.disabled;
                        }""",
                        timeout=10000,
                    )
                    await send_btn.click(timeout=3000)
                except Exception as e:
                    logger.warning(f"Send click failed ({e}), using Enter fallback")
                    await self.page.keyboard.press("Enter")

                # Allow Brave to commit the user entry to the DB.
                # The user entry (character_type=0) is written immediately on
                # send — resolve the UUID from it rather than from the URL,
                # which never carries a UUID in this Brave build.
                await asyncio.sleep(1.0)

                from leo_chat.db.leo_read import get_uuid_by_user_entry
                loop = asyncio.get_event_loop()
                # On the resume (multiturn) path the UUID is already known and
                # pre-seeded by _navigate_to_conversation; re-resolving it from
                # the DB here could overwrite it with a foreign conversation's
                # entry (a parallel instance written after our baseline). Keep
                # the known UUID and only resolve fresh for the first turn.
                if not self.conversation_uuid:
                    _uuid_t0 = time.monotonic()
                    self.conversation_uuid = None
                    # A single fixed window: the previous 8s/20s size-gate
                    # starved SHORT prompts (prompt-only requests carry no
                    # <context> block), which is exactly the case that needs the
                    # LONGEST window — there is less to anchor on, so the user
                    # entry can commit later relative to the send click. A short
                    # prompt timing out here degrades the whole request into
                    # "still_working" (no UUID -> no structured completion).
                    _uuid_timeout = 20
                    deadline = asyncio.get_event_loop().time() + _uuid_timeout
                    while asyncio.get_event_loop().time() < deadline:
                        uid = await loop.run_in_executor(
                            None,
                            lambda: get_uuid_by_user_entry(self._baseline_rowid),
                        )
                        if uid:
                            self.conversation_uuid = uid
                            break
                        await asyncio.sleep(0.5)
                    _page_phase("leo_page_phase", _uuid_t0, phase="uuid_resolution",
                                found=bool(self.conversation_uuid))

                if self.conversation_uuid:
                    logger.debug(f"✅ UUID via DB: {self.conversation_uuid[:8]}...")
                else:
                    logger.warning("⚠️ No UUID not resolved via DB → DOM fallback")
                    await self._capture_dom_snapshot(f"no_uuid_attempt{attempt}")

                logger.debug("✅ Prompt injected and sent")
                _page_phase("leo_page_phase", _t0, phase="send_one_message_done",
                            is_first=is_first)
                return True

            except Exception as e:
                logger.error(
                    f"❌ Error in _send_one_message (attempt {attempt}): {e}",
                    exc_info=True,
                )
                await self._capture_dom_snapshot(f"inject_error_attempt{attempt}")
                if attempt < max_attempts:
                    try:
                        if self.page and not self.page.is_closed():
                            await self.page.close()
                    except Exception:
                        pass
                    self.page = None
                    await asyncio.sleep(1.0)
                else:
                    return False

        return False


    # ─────────────────────────────────────────────
    # Chunking
    # ─────────────────────────────────────────────

    async def _inject_chunked(
        self,
        final_prompt: str,
        model_key: str,
        max_attempts: int,  ) -> bool:
        """
        Split a large prompt into chunks and send them sequentially over the
        same conversation UUID.

        Chunk 1 is sent with is_first=True (creates the conversation, selects
        the model). Remaining chunks use is_first=False (same tab/conversation,
        no model re-selection). Leo preserves context within one UUID.

        Returns:
            True if all chunks were sent successfully, False otherwise.
        """
        total_len = len(final_prompt)
        chunks = self._split_prompt_at_boundaries(
            final_prompt, self.LEO_MAX_PROMPT_CHARS
        )
        logger.info(
            f"📦 Chunked prompt into {len(chunks)} parts (total {total_len} chars)"
        )

        total = len(chunks)
        for i, chunk in enumerate(chunks):
            chunk_label = f"{i + 1}/{total}"
            logger.info(f"📨 Sending chunk {chunk_label} ({len(chunk)} chars)")

            success = await self._send_one_message(
                final_prompt=chunk,
                model_key=model_key,
                max_attempts=max_attempts,
                is_first=(i == 0),
            )

            if not success:
                logger.error(f"❌ Failed to send chunk {chunk_label}")
                return False

            # Wait for Leo to finish before sending the next chunk
            if i < total - 1:
                await self._wait_for_leo_idle(chunk=chunk)
                logger.debug(f"✅ Chunk {chunk_label} done, ready for next.")

        logger.info(f"✅ All {total} chunks sent successfully.")
        return True

    @staticmethod
    def _split_prompt_at_boundaries(text: str, max_chars: int) -> List[str]:
        """
        Split text into chunks of at most max_chars, preferring natural break
        points (paragraph > newline > sentence > space) within the last 20%
        of each candidate window.

        Includes a hard guarantee that `remaining` always shrinks, preventing
        any theoretical infinite loop on pathological input (e.g. a single
        >max_chars token with no separators).

        Args:
            text: The text to split.
            max_chars: Maximum characters per chunk.

        Returns:
            List of chunk strings.
        """
        if max_chars <= 0:
            raise ValueError("max_chars must be positive")

        chunks: List[str] = []
        remaining = text

        while len(remaining) > max_chars:
            search_start = int(max_chars * 0.8)
            candidate = remaining[:max_chars]

            split_at = candidate.rfind("\n\n", search_start)
            if split_at == -1:
                split_at = candidate.rfind("\n", search_start)
            if split_at == -1:
                split_at = candidate.rfind(". ", search_start)
            if split_at == -1:
                split_at = candidate.rfind(" ", search_start)
            if split_at == -1:
                # Hard split — no natural boundary found
                split_at = max_chars - 1

            chunk = remaining[: split_at + 1].rstrip()
            next_remaining = remaining[split_at + 1:].lstrip()

            # Safety guard: ensure forward progress. If stripping produced no
            # shrinkage (degenerate case), force a hard cut at max_chars.
            if len(next_remaining) >= len(remaining):
                chunk = remaining[:max_chars]
                next_remaining = remaining[max_chars:]

            if chunk:
                chunks.append(chunk)
            remaining = next_remaining

        if remaining.strip():
            chunks.append(remaining.strip())

        return chunks



    async def _wait_for_leo_idle(
        self,
        timeout_sec: int | None = None,
        chunk: str = "",
        *,
        base_timeout: int = 15,
        chars_per_second: float = 80.0,
        max_timeout: int = 120, ) -> None:
        """
        Wait for Leo to finish generating before sending the next chunk.

        Timeout is dynamic: estimated from chunk length if not provided explicitly.

        Args:
            timeout_sec:      Manual timeout override (None = calculate automatically).
            chunk:            The chunk text that was sent (used to estimate timeout).
            base_timeout:     Minimum guaranteed seconds regardless of chunk size.
            chars_per_second: Estimated Leo generation speed (tune to your case).
            max_timeout:      Hard ceiling to avoid unreasonably long waits.
        """
        # Calculate dynamic timeout if not explicitly provided
        if timeout_sec is None:
            estimated = len(chunk) / chars_per_second if chunk else 0
            timeout_sec = min(int(base_timeout + estimated), max_timeout)
            logger.debug(f"⏳ Dynamic timeout: {timeout_sec}s (chunk={len(chunk)} chars)")

        # Initial cooldown so Leo has time to START generating
        await asyncio.sleep(3.0)

        if not self.page or self.page.is_closed():
            await asyncio.sleep(5.0)
            return

        deadline = asyncio.get_event_loop().time() + timeout_sec
        while asyncio.get_event_loop().time() < deadline:
            if not await self._is_leo_busy():
                logger.debug("✅ Leo idle — ready for next chunk")
                return
            await asyncio.sleep(1.5)

        logger.warning(
            f"⏱️ Leo still busy after {timeout_sec}s — sending next chunk anyway"
        )


    # ─────────────────────────────────────────────
    # Input focus
    # ─────────────────────────────────────────────

    async def _focus_input(self, input_locator, max_focus_attempts: int = 2) -> bool:
        """
        Focus the input with a resilient strategy (JS focus + force click).

        FIX: the focus verification now also checks shadowRoot.activeElement.
        With Web Components / Shadow DOM, document.activeElement points to the
        host element, not the inner node — the old check produced false
        negatives. We walk into open shadow roots to confirm true focus.

        Returns:
            True if the input is focused, False after exhausting attempts.
        """
        focus_check_js = """
            node => {
                if (document.activeElement === node) return true;
                // Walk down through open shadow roots to find the focused node
                let active = document.activeElement;
                while (active && active.shadowRoot) {
                    if (active.shadowRoot.activeElement === node) return true;
                    active = active.shadowRoot.activeElement;
                }
                return active === node;
            }
        """

        for attempt in range(max_focus_attempts):
            try:
                await input_locator.evaluate("node => node.focus()")
                await input_locator.click(force=True)
                is_focused = await input_locator.evaluate(focus_check_js)
                if is_focused:
                    logger.debug(f"✅ Input focused on attempt {attempt + 1}")
                    return True
                logger.warning(f"⚠️ Input not focused on attempt {attempt + 1}")
            except Exception as e:
                logger.warning(f"⚠️ Warning during focus/click: {e}")
                try:
                    await input_locator.click(force=True)
                except Exception:
                    pass
            if attempt < max_focus_attempts - 1:
                await asyncio.sleep(0.3)
        return False

    # ─────────────────────────────────────────────
    # Response polling
    # ─────────────────────────────────────────────

    async def wait_for_response(self, timeout_ms: int = 120000) -> str:
        """
        Wait for Leo's response using the SQLite DB as the source of truth.

        The UI's submit-button state is NOT a reliable completion signal in this
        Brave build: the button remains `disabled=true` after generation (because
        the input is cleared, which re-disables send), so cross-referencing
        _is_leo_busy() here made `wait_for_response` hang forever on a completed
        response. The DB stabilization (wait_for_completion) already detects a
        complete, stable entry_text — trust it directly.

        Falls back to DOM stabilization (iframe-aware) if the DB path fails.
        """
        timeout_sec = timeout_ms / 1000.0

        if self.conversation_uuid:
            try:
                from leo_chat.db.leo_read import wait_for_completion

                uuid = self.conversation_uuid
                loop = asyncio.get_event_loop()
                logger.info(f"🕵️ Monitoring DB generation for UUID: {uuid[:8]}...")

                database_text = await loop.run_in_executor(
                    None,
                    lambda: wait_for_completion(
                        uuid,
                        expected_min_rowid=self._baseline_rowid,
                        timeout=int(timeout_sec),
                        poll_interval=1.0,
                    ),
                )

                if database_text and database_text.strip():
                    logger.debug(
                        f"✅ Response from DB ({len(database_text)} chars)."
                    )
                    return database_text

            except Exception as e:
                logger.error(
                    f"❌ DB tracking failed: {e} → falling back to DOM tracking"
                )

        return await self._wait_for_dom_stabilization(timeout_ms)

    async def _wait_for_dom_stabilization(self, timeout_ms: int) -> str:
        """
        Fallback DOM reader that pierces the conversation iframe to poll chat
        text while using the unified _is_leo_busy() flag to know when the UI
        has been re-enabled (generation finished).
        """
        if not self.page or self.page.is_closed():
            logger.error("❌ Active page instance missing during DOM read")
            return "⚠️ UI Error: Target page context is closed."

        timeout_sec = timeout_ms / 1000.0
        start_time = asyncio.get_event_loop().time()

        await asyncio.sleep(2.5)

        # Try iframe first; if the conversation iframe doesn't exist in this
        # Brave build (Leo renders directly in the main frame), fall back to
        # querying the main frame directly.
        iframe_count = await self.page.locator(self.CONVERSATION_IFRAME).count()
        if iframe_count > 0:
            chat_frame = self.page.frame_locator(self.CONVERSATION_IFRAME)
            response_container = chat_frame.locator(self.RESPONSE_SELECTOR).last
        else:
            logger.debug("No conversation iframe found — querying main frame directly.")
            response_container = self.page.locator(self.RESPONSE_SELECTOR).last

        try:
            await response_container.wait_for(state="visible", timeout=10000)
        except Exception:
            logger.warning(
                "⏱️ Response container visibility timed out."
            )
            await self._capture_dom_snapshot("iframe_container_visibility_timeout")

        logger.debug("🔍 Polling iframe text + unified UI busy state...")
        cached_text = ""
        execution_time = 0.0
        stable_count = 0

        while True:
            execution_time = asyncio.get_event_loop().time() - start_time
            if execution_time > timeout_sec:
                logger.warning(
                    f"⏱️ Maximum execution timeout reached ({execution_time:.1f}s)"
                )
                break

            ui_released = not await self._is_leo_busy()

            try:
                current_text = await response_container.inner_text()

                if current_text and current_text == cached_text:
                    stable_count += 1
                else:
                    stable_count = 0
                    cached_text = current_text

                # Done when UI is released AND text stopped growing (~1.6s)
                if ui_released and stable_count >= 2:
                    logger.debug(
                        "✅ UI re-enabled and iframe text stable — done."
                    )
                    break
            except Exception:
                pass

            await asyncio.sleep(0.8)

        # Final extraction after stabilization
        try:
            final_output = await response_container.inner_text()
        except Exception:
            final_output = cached_text

        if not final_output or len(final_output.strip()) == 0:
            logger.warning("⚠️ Iframe DOM text extraction returned empty.")
            await self._capture_dom_snapshot("empty_iframe_extraction")
            return "⚠️ Error: Unable to extract content from the chat container."

        logger.info(
            f"✅ Parsed via iframe DOM: {len(final_output)} chars "
            f"in {execution_time:.1f}s"
        )
        return final_output.strip()


    async def _navigate_to_conversation(self, uuid: str) -> bool:
        """
        Navigate to an existing Leo conversation by UUID instead of creating
        a new tab. Uses a direct CDP session to reach brave://leo-ai/<UUID>.

        Args:
            uuid: The conversation UUID to navigate to.

        Returns:
            True if navigation succeeded, False otherwise.
        """
        if not self.context:
            logger.error("Not connected. Call connect() first.")
            return False

        try:
            logger.debug(f"🔄 Navigating to existing conversation: {uuid[:8]}...")
            self.page = await self.context.new_page()

            cdp_session = await self.context.new_cdp_session(self.page)
            await cdp_session.send(
                "Page.navigate", {"url": f"brave://leo-ai/{uuid}"}
            )

            # Wait for the conversation to load
            try:
                await self.page.wait_for_url(
                    f"**/leo-ai/{uuid}*",
                    timeout=15000,
                    wait_until="domcontentloaded",
                )
            except Exception as e:
                logger.warning(
                    f"⚠️ Timeout waiting for conversation URL: {e}. Continuing..."
                )
                await asyncio.sleep(3)

            logger.debug(f"✅ Navigated to: {self.page.url}")
            return True

        except Exception as e:
            logger.warning(f"⚠️ Failed to navigate to conversation: {e}")
            return False


    # ─────────────────────────────────────────────
    # Cleanup
    # ─────────────────────────────────────────────

    async def close(self):
        """Public method to close the connection."""
        await self._cleanup()

    async def _cleanup(self):
        """
        Close ONLY the Leo tab opened for this request and disconnect CDP
        without closing the Brave window. Idempotent — safe to call repeatedly.
        """
        # 1. Close only the tab we opened
        try:
            if self.page and not self.page.is_closed():
                await self.page.close()
                logger.debug("🗑️  Leo tab closed")
        except Exception as e:
            logger.warning(f"⚠️ Error closing tab: {e}")
        finally:
            self.page = None

        # 2. Disconnect CDP — does NOT close the Brave window
        try:
            if self.browser:
                # Browser from connect_over_cdp uses close(), not disconnect()
                await self.browser.close()
                logger.debug("🔌 CDP disconnected (Brave window preserved)")
        except Exception as e:
            logger.warning(f"⚠️ Error disconnecting CDP: {e}")
        finally:
            self.browser = None
            self.context = None

        # 3. Stop the Playwright driver
        try:
            if self._pw:
                await self._pw.stop()
                logger.debug("🛑 Playwright driver stopped")
        except Exception as e:
            logger.warning(f"⚠️ Error stopping Playwright: {e}")
        finally:
            self._pw = None

    # ─────────────────────────────────────────────
    # Repr
    # ─────────────────────────────────────────────

    def __repr__(self) -> str:
        status = "connected" if self.browser else "disconnected"
        uuid_short = (
            self.conversation_uuid[:8] + "..."
            if self.conversation_uuid
            else "None"
        )
        return f"<BraveLeoPage({status}) uuid={uuid_short}>"


    # ─────────────────────────────────────────────
    # Diagnostics
    # ─────────────────────────────────────────────

    async def _capture_dom_snapshot(self, reason: str) -> Optional[str]:
        """
        Capture the page DOM + metadata for post-mortem analysis on failure.
        Saves to ~/.hermes/dom_snapshots/ with a timestamp.

        Uses module-level os/time/json imports (no more redundant local imports).
        """
        try:
            if not self.page or self.page.is_closed():
                logger.warning("No live page to capture DOM from")
                return None

            snapshot_dir = os.path.expanduser("~/.hermes/dom_snapshots")
            os.makedirs(snapshot_dir, exist_ok=True)
            ts = time.strftime("%Y%m%d_%H%M%S")
            base = os.path.join(snapshot_dir, f"snapshot_{ts}_{reason}")

            # 1. Full DOM HTML
            html_content = await self.page.content()
            with open(f"{base}.html", "w", encoding="utf-8") as f:
                f.write(html_content)

            # 2. Diagnostic metadata
            meta = {
                "timestamp": ts,
                "reason": reason,
                "url": self.page.url,
                "uuid": self.conversation_uuid,
                "input_field_present": await self.page.locator(
                    self.INPUT_FIELD
                ).count() > 0,
                "model_button_present": await self.page.locator(
                    self.MODEL_BUTTON_SELECTOR
                ).count() > 0,
            }
            with open(f"{base}_meta.json", "w", encoding="utf-8") as f:
                json.dump(meta, f, indent=2, ensure_ascii=False)

            # 3. Optional screenshot (great for spotting overlays/popups)
            try:
                await self.page.screenshot(path=f"{base}.png", full_page=True)
            except Exception as e:
                logger.debug(f"Screenshot failed (non-critical): {e}")

            logger.info(f"📸 DOM snapshot saved: {base}.*")
            return base

        except Exception as e:
            logger.warning(f"Could not capture DOM snapshot: {e}")
            return None

    async def _dismiss_overlays(self):
        """
        Close Leo menus/popups (leo-buttonmenu with isopen="true") that may
        cover the input field.
        """
        try:
            await self.page.evaluate(
                """
                () => {
                    document.querySelectorAll('leo-buttonmenu[isopen="true"]')
                        .forEach(menu => {
                            menu.setAttribute('isopen', 'false');
                            if (typeof menu.close === 'function') menu.close();
                        });
                }
                """
            )
            await asyncio.sleep(0.2)
        except Exception as e:
            logger.debug(f"Could not dismiss overlays: {e}")

# ── Model Registry Loader (backward-compatible shim) ────────────────────────
#
# The loader historically lived here as `_load_model_registry`. The real
# implementation moved to `leo_chat.model_registry`, but we keep these
# module-level names (and their mutable `_MODEL_REGISTRY_PATH` / cache) so
# existing imports and tests that override them keep working unchanged.

_MODEL_REGISTRY_PATH = MODEL_REGISTRY_PATH
_model_registry_cache: Optional[Dict[str, str]] = None


def _load_model_registry() -> Dict[str, str]:
    """
    Load model display names from models.toml (backward-compatible shim).

    Returns a dict mapping model_key → display_name. Falls back to a minimal
    built-in map if the config file is missing or unreadable. Cached in memory
    after the first load. The path and cache are module-level so tests can
    override them as before.
    """
    global _model_registry_cache

    registry = load_model_registry(
        path=_MODEL_REGISTRY_PATH,
        cache=_model_registry_cache,
    )
    # When the cache was empty (first call), load_model_registry returns a
    # fresh dict without a caller-owned cache to populate — persist it here.
    if _model_registry_cache is None:
        _model_registry_cache = registry
    return registry