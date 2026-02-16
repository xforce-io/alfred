"""
Dolphin patches for web environment compatibility

Fixes blocking issues when running Dolphin agents in non-TTY environments.
"""


def patch_globalskills_for_web():
    """
    Patch GlobalSkills to avoid blocking in non-TTY environments.

    Main issue: rich.Console.status blocks in uvicorn/web environments.
    Solution: Disable entry points loading and use quiet console.
    """
    try:
        import dolphin.sdk.skill.global_skills as gs_module

        # Patch 1: Skip entry points loading entirely
        # This avoids the console.status blocking issue
        def skip_entry_points(self):
            return False

        gs_module.GlobalSkills._loadSkillkitsFromEntryPoints = skip_entry_points

        print("[Patch] GlobalSkills: Disabled entry points loading for web compatibility")

    except ImportError as e:
        print(f"[Patch] Warning: Could not patch GlobalSkills: {e}")
    except Exception as e:
        print(f"[Patch] Error patching GlobalSkills: {e}")


def apply_all_patches():
    """Apply all Dolphin patches for web environment"""
    print("[Patch] Applying Dolphin patches for web environment...")
    patch_globalskills_for_web()
    print("[Patch] All patches applied")
