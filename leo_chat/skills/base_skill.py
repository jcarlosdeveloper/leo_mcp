"""Base Skill - Abstract base class for all Leo Chat skills.

Mandatory contract:
- model_key: model identifier used by the skill.
- system_prompt: prompt that defines default (prose) behavior.
- get_full_prompt(): builds and returns the compiled prompt for the flow.

Structured output (single unified mechanism):
- A skill opts in by setting STRUCTURED_DIRECTIVE (the strict output contract)
  and, optionally, system_prompt_structured (a terse prompt that avoids prose
  conflicting with the contract). get_full_prompt(structured=True) then emits
  the contract so downstream tooling can parse machine-readable output.
- supports_patch_output marks skills whose structured output is a file patch
  the MCP may write to disk. Non-writing structured skills (e.g. planners)
  leave it False.
"""

from abc import ABC
from typing import Optional


class BaseSkill(ABC):
    """Base class for all Leo Chat skills."""

    model_key: str = "default"
    system_prompt: str = "You are a helpful assistant."
    skill_id: str = "base"
    description: str = "Base skill with no implementation"

    # Strict output contract emitted when structured=True. None means the skill
    # has no structured mode and always produces prose.
    STRUCTURED_DIRECTIVE: Optional[str] = None

    # Terse system prompt paired with STRUCTURED_DIRECTIVE. When set, it
    # replaces system_prompt in structured mode so prose formatting cannot
    # conflict with the machine-readable contract.
    system_prompt_structured: Optional[str] = None

    # True only for skills whose structured output is a file patch the MCP may
    # write to disk. Keeps disk writes strictly opt-in per skill.
    supports_patch_output: bool = False

    def _active_system_prompt(self, structured: bool) -> str:
        """Select the terse structured prompt when available, else the default."""
        if structured and self.system_prompt_structured:
            return self.system_prompt_structured
        return self.system_prompt

    def get_full_prompt(
        self,
        user_prompt: str,
        context: str = "",
        structured: bool = False,
    ) -> str:
        """Assemble system, optional structured contract, context, and request.

        In structured mode the skill's STRUCTURED_DIRECTIVE is injected (if the
        skill defines one) so downstream tooling receives machine-parseable
        output. Skills without a directive fall back to prose regardless of the
        structured flag.
        """
        parts = []

        active_system = self._active_system_prompt(structured)
        if active_system:
            parts.append(f"<system>\n{active_system}\n</system>\n")

        if structured and self.STRUCTURED_DIRECTIVE:
            parts.append(
                f"<output_contract>\n{self.STRUCTURED_DIRECTIVE}\n</output_contract>\n"
            )

        if context and context.strip():
            parts.append(f"<context>\n{context}\n</context>\n")

        parts.append(f"<user>\n{user_prompt}\n</user>\n")

        return "\n".join(parts)

    def __repr__(self) -> str:
        return f"<{self.__class__.__name__}(model={self.model_key}, id={self.skill_id})>"