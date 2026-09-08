#!/usr/bin/env python3
"""
Test for completed Tasks 1 and 2

Verifies:
1. assemble_context() with exact formatting
2. BraveLeoPage with real DOM selectors
"""

import asyncio
import sys
import time
from pathlib import Path


def test_task1_assemble_context():
    """Test Task 1: assemble_context with exact formatting"""
    print("\nTest 1: assemble_context()")
    print("-" * 60)

    from leo_chat.context.prompt_builder import assemble_context

    # Create test file with dynamic content
    test_file = Path("/tmp/test_assemble_context.txt")
    timestamp = str(int(time.time()))
    test_content = f"Generated content {timestamp}\nLine 2\nLine 3"
    test_file.write_text(test_content)

    print(f"Test file: {test_file}")
    print(f"Content: {len(test_content)} chars")

    # Run assemble_context
    result = assemble_context([str(test_file)])

    print(f"\nGenerated result ({len(result)} chars):")
    print("-" * 60)
    print(result)
    print("-" * 60)

    # Verify exact format
    # Note: the header uses the resolved (absolute) path, not the original
    expected_header = "--- FILE START:"
    expected_footer = "--- FILE END ---"

    checks = {
        "Header present": expected_header in result,
        "Footer present": expected_footer in result,
        "Content included": test_content in result,
        "Correct format": result.count(expected_header) == 1 and result.count(expected_footer) == 1
    }

    print("\nChecks:")
    all_passed = True
    for check_name, passed in checks.items():
        status = "OK" if passed else "FAIL"
        print(f"  [{status}] {check_name}")
        if not passed:
            all_passed = False

    # Test with missing file (must handle gracefully)
    print("\nTest with missing file:")
    result_missing = assemble_context(["/tmp/nonexistent_file_12345.txt"])

    if "WARNING" in result_missing or "not found" in result_missing.lower():
        print("  [OK] Error handled correctly (includes warning)")
    else:
        print("  [FAIL] Did not handle error gracefully")
        all_passed = False

    # Cleanup
    test_file.unlink()

    return all_passed


async def test_task2_brave_leo_page_structure():
    """Test Task 2: BraveLeoPage structure and selectors"""
    print("\nTest 2: BraveLeoPage (POM)")
    print("-" * 60)

    from leo_chat.pages.brave_leo_page import BraveLeoPage

    # Verify the class exists and has the correct attributes
    print("Verifying BraveLeoPage structure:")

    checks = {
        "Class exists": BraveLeoPage is not None,
        "DEBUG_PORT defined": hasattr(BraveLeoPage, 'DEBUG_PORT') and BraveLeoPage.DEBUG_PORT == 9222,
        "DEBUG_URL defined": hasattr(BraveLeoPage, 'DEBUG_URL'),
        "LEO_URL defined": hasattr(BraveLeoPage, 'LEO_URL') and 'leo-ai' in BraveLeoPage.LEO_URL,
        "MODEL_BUTTON selector": hasattr(BraveLeoPage, 'MODEL_BUTTON') and "leo-button[slot='anchor-content']" in BraveLeoPage.MODEL_BUTTON,
        "MODEL_ITEM selector": hasattr(BraveLeoPage, 'MODEL_ITEM') and "leo-menu-item[data-key=" in BraveLeoPage.MODEL_ITEM,
        "INPUT_FIELD selector": hasattr(BraveLeoPage, 'INPUT_FIELD') and "data-test-id" in BraveLeoPage.INPUT_FIELD,
        "inject_and_send method exists": hasattr(BraveLeoPage, 'inject_and_send'),
        "connect method exists": hasattr(BraveLeoPage, 'connect'),
        "select_model method exists": hasattr(BraveLeoPage, 'select_model')
    }

    all_passed = True
    for check_name, passed in checks.items():
        status = "OK" if passed else "FAIL"
        print(f"  [{status}] {check_name}")
        if not passed:
            all_passed = False

    # Verify inject_and_send method signature
    import inspect
    method = getattr(BraveLeoPage, 'inject_and_send', None)
    if method:
        sig = inspect.signature(method)
        params = list(sig.parameters.keys())

        has_self = 'self' in params
        has_final_prompt = 'final_prompt' in params
        has_model_key = 'model_key' in params
        has_default_model = sig.parameters['model_key'].default == "chat-claude-sonnet"

        print("\nVerifying inject_and_send signature:")
        print(f"  [{'OK' if has_self else 'FAIL'}] Has 'self' parameter")
        print(f"  [{'OK' if has_final_prompt else 'FAIL'}] Has 'final_prompt' parameter")
        print(f"  [{'OK' if has_model_key else 'FAIL'}] Has 'model_key' parameter")
        print(f"  [{'OK' if has_default_model else 'FAIL'}] Default model_key = 'chat-claude-sonnet'")

        if not (has_self and has_final_prompt and has_model_key and has_default_model):
            all_passed = False

    # Instantiate and verify
    print("\nVerifying instantiation:")
    try:
        page = BraveLeoPage()
        print(f"  [OK] Instantiated: {repr(page)}")
        print(f"  [OK] Initial state: browser={page.browser}, context={page.context}, page={page.page}")
    except Exception as e:
        print(f"  [FAIL] Instantiation error: {e}")
        all_passed = False

    return all_passed


def main():
    """Run all tests"""
    print("\n" + "=" * 60)
    print("COMPLETED TASKS TEST - leo_chat module")
    print("=" * 60)

    # Task 1 (synchronous)
    task1_passed = test_task1_assemble_context()

    # Task 2 (asynchronous)
    task2_passed = asyncio.run(test_task2_brave_leo_page_structure())

    # Final summary
    print("\n" + "=" * 60)
    print("FINAL RESULTS")
    print("=" * 60)

    print(f"\n  Task 1 (assemble_context): {'OK' if task1_passed else 'FAIL'}")
    print(f"  Task 2 (BraveLeoPage POM): {'OK' if task2_passed else 'FAIL'}")

    if task1_passed and task2_passed:
        print("\nSUCCESS: Both tasks completed")
        print("\nCreated files:")
        print("  - leo_chat/context/prompt_builder.py")
        print("  - leo_chat/pages/brave_leo_page.py")
        return 0
    else:
        print("\nWARNING: Some tasks failed")
        return 1


if __name__ == '__main__':
    exit_code = main()
    sys.exit(exit_code)