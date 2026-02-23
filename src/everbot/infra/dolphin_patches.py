"""
Dolphin patches for web environment compatibility

Fixes blocking issues when running Dolphin agents in non-TTY environments.
"""

import logging

logger = logging.getLogger(__name__)


def patch_globalskills_for_web():
    """
    Patch GlobalSkills to avoid blocking in non-TTY environments.

    Main issue: rich.Console.status blocks in uvicorn/web environments.
    Solution: Disable entry points loading and use quiet console.

    .. deprecated:: No call sites in the current codebase; consider removing.
    """
    try:
        import dolphin.sdk.skill.global_skills as gs_module

        # Patch 1: Skip entry points loading entirely
        # This avoids the console.status blocking issue
        def skip_entry_points(self):
            return False

        gs_module.GlobalSkills._loadSkillkitsFromEntryPoints = skip_entry_points

        logger.info("GlobalSkills: Disabled entry points loading for web compatibility")

    except ImportError as e:
        logger.warning("Could not patch GlobalSkills: %s", e)
    except Exception as e:
        logger.warning("Error patching GlobalSkills: %s", e)


def apply_all_patches():
    """Apply all Dolphin patches for web environment.

    .. deprecated:: No call sites in the current codebase; consider removing.
    """
    logger.info("Applying Dolphin patches for web environment...")
    patch_globalskills_for_web()
    logger.info("All patches applied")
