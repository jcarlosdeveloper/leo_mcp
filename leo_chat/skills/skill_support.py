"""Helper to query per-skill capabilities without duplicating factory logic."""

from leo_chat.skills.skill_factory import SkillFactory


def skill_supports_patch(skill_name: str) -> bool:
    """Return True if the named skill opts into disk-writing patch output.

    Reads the capability flag directly off the skill class (no instantiation
    or private-registry access). Defaults to False for unknown or non-patch
    skills, keeping disk writes strictly opt-in per skill regardless of the
    global write mode.
    """
    try:
        skill_class = SkillFactory.get_skill_class(skill_name)
        return getattr(skill_class, "supports_patch_output", False)
    except ValueError:
        return False