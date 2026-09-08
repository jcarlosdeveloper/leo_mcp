"""
Leo MCP Test Configuration and Fixtures

Este archivo configura pytest y provee fixtures reutilizables para todos los tests.
"""

import asyncio
import json
import sys
from collections.abc import Generator
from pathlib import Path

import pytest

# ============================================
# Path Setup
# ============================================

# Agregar el directorio raíz al sys.path para imports
ROOT_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT_DIR))

# ============================================
# Pytest Configuration
# ============================================


def pytest_configure(config):
    """Configurar markers personalizados."""
    config.addinivalue_line(
        "markers",
        "slow: marks tests as slow (deselect with '-m \"not slow\"')",
    )
    config.addinivalue_line(
        "markers",
        "integration: marks tests requiring Brave browser and MCP context",
    )
    config.addinivalue_line(
        "markers",
        "streaming: marks tests for streaming functionality",
    )
    config.addinivalue_line(
        "markers",
        "playwright: marks tests requiring Playwright browsers",
    )


# ============================================
# Fixtures Globales
# ============================================


@pytest.fixture(scope="session")
def root_dir() -> Path:
    """Devuelve el directorio raíz del proyecto."""
    return ROOT_DIR


@pytest.fixture(scope="session")
def tests_dir() -> Path:
    """Devuelve el directorio de tests."""
    return Path(__file__).parent


@pytest.fixture
def sample_url() -> str:
    """URL de ejemplo para tests de web scraping."""
    return "https://example.com"


@pytest.fixture
def invalid_urls() -> list:
    """Lista de URLs inválidas para tests de validación."""
    return [
        "brave://leo-ai/",
        "brave://leo-aibrave://leo-ai",
        "example.com",  # sin scheme
        "ftp://example.com",  # scheme no soportado
        "",  # vacío
        "not-a-url",
    ]


@pytest.fixture
def valid_http_urls() -> list:
    """Lista de URLs HTTP válidas para tests."""
    return [
        "https://example.com",
        "https://httpbin.org/html",
        "http://example.com",
    ]


@pytest.fixture(scope="session")
def event_loop() -> Generator:
    """Crear event loop para tests async."""
    loop = asyncio.get_event_loop_policy().new_event_loop()
    yield loop
    loop.close()


# ============================================
# Fixtures para Leo MCP Server
# ============================================


@pytest.fixture
def leo_server_config() -> dict:
    """Configuración básica para el servidor Leo MCP."""
    return {
        "hermes_dir": Path.home() / ".hermes",
        "default_timeout": 30,
        "streaming_poll_interval": 1.5,
        "streaming_max_attempts": 20,
        "cache_ttl_minutes": 30,
    }


@pytest.fixture
def sample_leo_response() -> dict:
    """Respuesta leo de ejemplo para tests de parsing."""
    return {
        "status": "ok",
        "skill_used": "/code_refiner",
        "has_copy_block": False,
        "has_plan_block": False,
        "files_required": [],
        "content": "Sample response content",
        "conversation_uuid": "d76a98d4-1234-5678-90ab-cdef12345678",
    }


@pytest.fixture
def sample_fetch_result() -> dict:
    """Resultado de fetch_url de ejemplo."""
    return {
        "success": True,
        "url": "https://example.com",
        "title": "Example Domain",
        "text": "Example Domain\n\nThis domain is for use in documentation...",
        "text_length": 129,
        "html_length": 559,
        "elapsed_seconds": 2.99,
        "error": None,
    }


# ============================================
# Helper Functions para Tests
# ============================================


@pytest.fixture
def json_assertions():
    """Provee funciones de aserción para JSON."""

    class JSONAssertions:
        @staticmethod
        def assert_valid_json_string(json_string: str) -> dict:
            """Valida que un string es JSON válido y lo retorna parseado."""
            try:
                return json.loads(json_string)
            except json.JSONDecodeError as e:
                pytest.fail(f"Invalid JSON: {e}")

        @staticmethod
        def assert_has_keys(data: dict, *keys: str) -> None:
            """Verifica que un dict tiene las keys especificadas."""
            missing = [k for k in keys if k not in data]
            if missing:
                pytest.fail(f"Missing keys in data: {missing}")

        @staticmethod
        def assert_status_ok(data: dict) -> None:
            """Verifica que el status es 'ok'."""
            assert data.get("status") == "ok", (
                f"Expected status 'ok', got: {data.get('status')}"
            )

    return JSONAssertions()


# ============================================
# Markers Utilitarios
# ============================================


@pytest.fixture
def mark_slow() -> pytest.Mark:
    """Decorator para marcar tests como lentos."""
    return pytest.mark.slow


@pytest.fixture
def mark_integration() -> pytest.Mark:
    """Decorator para marcar tests como de integración."""
    return pytest.mark.integration


@pytest.fixture
def mark_streaming() -> pytest.Mark:
    """Decorator para marcar tests de streaming."""
    return pytest.mark.streaming


# ============================================
# Cleanup Fixtures
# ============================================


@pytest.fixture
def temp_file_cleanup(tmp_path: Path) -> Generator[Path, None, None]:
    """Crea un archivo temporal y lo limpia después del test."""
    temp_file = tmp_path / "test_file.txt"
    yield temp_file
    # Cleanup automático (pytest lo hace, pero podemos agregar lógica extra)
    if temp_file.exists():
        temp_file.unlink()


# ============================================
# NOTA: Fixtures específicos para cada módulo
# ============================================
#
# Para tests de fetch_url, usar:
#   - sample_url
#   - invalid_urls
#   - valid_http_urls
#   - sample_fetch_result
#
# Para tests de streaming, usar:
#   - leo_server_config
#   - sample_leo_response
#
# Para tests generales, usar:
#   - root_dir
#   - tests_dir
#   - json_assertions
#
# ============================================
