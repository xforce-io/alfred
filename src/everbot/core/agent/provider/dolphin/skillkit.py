"""Re-export dolphin Skillkit/SkillFunction base classes."""
from dolphin.core.skill.skillkit import Skillkit as SkillkitBase
from dolphin.core.skill.skill_function import SkillFunction

__all__ = ["SkillkitBase", "SkillFunction"]
