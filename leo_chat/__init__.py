"""
Leo Chat Module

Interacciones complejas conLeo vía CDP (sin robar foco).
Skills dinámicos con selección de modelos en runtime.
"""

# Core Execution
from .execution import execute_leo_flow

# Context
from .context.prompt_builder import assemble_context, PromptBuilder

# Skills
from .skills.base_skill import BaseSkill
from .skills.skill_factory import SkillFactory

# Pages
try:
    from .pages.brave_leo_page import BraveLeoPage
except ImportError:
    BraveLeoPage = None

# Selectors & model registry (extracted from the POM)
from .selectors import load_selectors, get_selector
from .model_registry import load_model_registry

# DB (leo_read) - moved from root to leo_chat/db/
from .db.leo_read import (
    decrypt, get_key, get_latest_response, stream_response,
    is_response_complete, wait_for_completion,
    latest_assistant_state, AssistantState,
)

__all__ = [
    # Core Execution
    'execute_leo_flow',
    
    # Context
    'assemble_context',
    'PromptBuilder',
    
    # Pages
    'BraveLeoPage',
    
    # Selectors & model registry
    'load_selectors',
    'get_selector',
    'load_model_registry',
    
    # Skills
    'BaseSkill',
    'SkillFactory',
    
    # DB
    'decrypt',
    'get_key',
    'get_latest_response',
    'stream_response',
    'is_response_complete',
    'wait_for_completion',
]