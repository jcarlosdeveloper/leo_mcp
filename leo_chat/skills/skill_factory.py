"""
Skill Factory - Skill factory with dynamic registration

Allows:
- Registering skills at runtime
- Getting a skill by ID
- Listing available skills
- Overriding models at runtime
"""

import logging
from typing import Dict, Type, Optional
from .base_skill import BaseSkill

logger = logging.getLogger(__name__)


class SkillFactory:
    """
    Skill factory with dynamic registration.

    Usage:
        # Register a skill
        SkillFactory.register("code_refiner", CodeRefinerSkill)

        # Get an instance
        skill = SkillFactory.get_skill("code_refiner")

        # Build the compiled prompt
        prompt = skill.get_full_prompt("Fix this bug...")
    """

    _registry: Dict[str, Type[BaseSkill]] = {}
    _model_overrides: Dict[str, str] = {}

    @classmethod
    def register(cls, skill_id: str, skill_class: Type[BaseSkill], model_override: Optional[str] = None):
        """
        Registers a skill in the factory.

        Args:
            skill_id: Unique skill ID
            skill_class: Skill class (must inherit from BaseSkill)
            model_override: Override for the default model (optional)

        Raises:
            TypeError: If the class does not inherit from BaseSkill, or if it
                fails to override BaseSkill's placeholder defaults (description,
                model_key, skill_id), or if its own skill_id disagrees with the
                registry key. Inheriting the abstract defaults silently is a
                configuration bug, so it fails loudly at registration time.
        """
        if not issubclass(skill_class, BaseSkill):
            raise TypeError(f"Skill class must inherit from BaseSkill, got {skill_class}")

        # A concrete skill must override the abstract placeholders rather than
        # inherit them; otherwise metadata (used by list_skills / model routing)
        # would be meaningless.
        if skill_class.description == BaseSkill.description:
            raise TypeError(f"Skill '{skill_id}' must define its own 'description'")
        if skill_class.model_key == BaseSkill.model_key:
            raise TypeError(f"Skill '{skill_id}' must define its own 'model_key'")
        if skill_class.skill_id == BaseSkill.skill_id:
            raise TypeError(f"Skill '{skill_id}' must define its own 'skill_id'")
        if skill_class.skill_id != skill_id:
            raise TypeError(
                f"Skill registry key '{skill_id}' does not match class "
                f"skill_id '{skill_class.skill_id}'"
            )

        cls._registry[skill_id] = skill_class

        if model_override:
            cls._model_overrides[skill_id] = model_override
            logger.debug(f"Registered skill '{skill_id}' with model override: {model_override}")
        else:
            logger.debug(f"Registered skill '{skill_id}' (model: {skill_class.model_key})")

    @classmethod
    def get_skill(cls, skill_id: str) -> BaseSkill:
        """
        Get an instance of a registered skill.

        Args:
            skill_id: Skill ID to instantiate

        Returns:
            Skill instance

        Raises:
            ValueError: If skill is not registered
        """
        skill_class = cls.get_skill_class(skill_id)
        instance = skill_class()

        # Apply model override if one exists
        if skill_id in cls._model_overrides:
            instance.model_key = cls._model_overrides[skill_id]

        return instance

    @classmethod
    def get_skill_class(cls, skill_id: str) -> Type[BaseSkill]:
        """Return a registered skill class without instantiating it.

        Lets callers inspect class-level capabilities (e.g. supports_patch_output)
        without the cost or side effects of constructing an instance.

        Raises:
            ValueError: If skill is not registered.
        """
        if skill_id not in cls._registry:
            available = ", ".join(cls._registry.keys())
            raise ValueError(f"Skill '{skill_id}' not found. Available: {available}")
        return cls._registry[skill_id]

    @classmethod
    def list_skills(cls) -> Dict[str, Dict[str, str]]:
        """
        Lists all available skills with their metadata.

        Returns:
            Dict of {skill_id: {description, model_key}}
        """
        return {
            skill_id: {
                'description': skill_class.description,
                'model_key': skill_class.model_key
            }
            for skill_id, skill_class in cls._registry.items()
        }

    @classmethod
    def set_model_override(cls, skill_id: str, model_key: str):
        """
        Changes a skill's model at runtime.

        Args:
            skill_id: Skill ID
            model_key: New model key
        """
        if skill_id not in cls._registry:
            raise ValueError(f"Skill '{skill_id}' not found")

        cls._model_overrides[skill_id] = model_key
        logger.info(f"Model override set: {skill_id} → {model_key}")


# Auto-register built-in skills on import
def _register_builtin_skills():
    """Registers the built-in skills automatically.

    Any failure to import or register a skill (ImportError, SyntaxError, a
    failed override validation, etc.) is logged with a full traceback instead
    of being swallowed, so a broken skill is diagnosable rather than silently
    missing.
    """
    # The remaining built-in skills form the production surface: senior_planner
    # (planning), code_refiner (whole-file patch), and code_editor (str_replace
    # edit). A failure to import or register one is logged with a full traceback
    # rather than swallowed, so a broken skill is diagnosable.
    optional_skills = [
        (".senior_planner", "SeniorPlannerSkill", "senior_planner"),
        (".code_refiner", "CodeRefinerSkill", "code_refiner"),
        (".code_editor", "CodeEditorSkill", "code_editor"),
    ]

    for module_name, class_name, skill_id in optional_skills:
        try:
            module = __import__(f"{__package__}{module_name}", fromlist=[class_name])
            SkillFactory.register(skill_id, getattr(module, class_name))
        except Exception:
            logger.exception(f"Failed to register built-in skill '{skill_id}'")


# Auto-register on module import
_register_builtin_skills()