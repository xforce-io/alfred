#!/usr/bin/env python3
"""
Skill installer - Install skills from various sources
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path
from typing import Dict, Optional


class SkillInstaller:
    def __init__(self, skills_dir: Optional[str] = None):
        self.skills_dir = Path(skills_dir) if skills_dir else self._get_default_skills_dir()
        self.registry_cache: Optional[Dict] = None

    def _get_default_skills_dir(self) -> Path:
        """Get the default skills directory"""
        # Try to find from config or use default
        home = Path.home()
        candidates = [
            home / ".alfred" / "skills",
            Path.cwd() / "skills",
        ]

        for path in candidates:
            if path.exists():
                return path

        # Default to ~/.alfred/skills
        default_path = home / ".alfred" / "skills"
        default_path.mkdir(parents=True, exist_ok=True)
        return default_path

    def _get_registry_url(self) -> str:
        """Get registry URL from config or use default"""
        # TODO: Read from config file
        return os.environ.get(
            "ALFRED_SKILL_REGISTRY",
            "https://raw.githubusercontent.com/your-org/alfred-skills/main/registry.json"
        )

    def _load_registry(self) -> Dict:
        """Load skill registry"""
        if self.registry_cache:
            return self.registry_cache

        # Try local registry first
        local_registry = Path.home() / ".alfred" / "skills-registry.json"
        if local_registry.exists():
            with open(local_registry) as f:
                self.registry_cache = json.load(f)
                return self.registry_cache

        # Try remote registry
        try:
            registry_url = self._get_registry_url()
            with urllib.request.urlopen(registry_url, timeout=10) as response:
                self.registry_cache = json.loads(response.read())
                return self.registry_cache
        except Exception as e:
            print(f"Warning: Could not load registry: {e}", file=sys.stderr)
            return {"skills": {}}

    def install(self, source: str, method: Optional[str] = None) -> bool:
        """
        Install a skill from various sources

        Args:
            source: Skill name (from registry), git URL, or file path
            method: Installation method - "registry", "git", "url", "local"

        Returns:
            True if successful
        """
        # Auto-detect method if not specified
        if method is None:
            method = self._detect_method(source)

        print(f"Installing skill from {source} using method: {method}")

        if method == "registry":
            return self._install_from_registry(source)
        elif method == "git":
            return self._install_from_git(source)
        elif method == "url":
            return self._install_from_url(source)
        elif method == "local":
            return self._install_from_local(source)
        else:
            print(f"Error: Unknown installation method: {method}", file=sys.stderr)
            return False

    def _detect_method(self, source: str) -> str:
        """Auto-detect installation method"""
        if source.startswith(("http://", "https://")):
            if "github.com" in source or source.endswith(".git"):
                return "git"
            return "url"
        elif os.path.exists(source):
            return "local"
        else:
            return "registry"

    def _install_from_registry(self, skill_name: str) -> bool:
        """Install skill from registry"""
        registry = self._load_registry()

        if skill_name not in registry.get("skills", {}):
            print(f"Error: Skill '{skill_name}' not found in registry", file=sys.stderr)
            print("\nAvailable skills:")
            for name, info in registry.get("skills", {}).items():
                print(f"  - {name}: {info.get('description', 'No description')}")
            return False

        skill_info = registry["skills"][skill_name]
        source_info = skill_info.get("source", {})
        source_type = source_info.get("type", "git")
        location = source_info.get("location")

        if not location:
            print(f"Error: No source location in registry for '{skill_name}'", file=sys.stderr)
            return False

        # Install the skill content
        if source_type == "git":
            success = self._install_from_git(location, skill_name)
        elif source_type == "url":
            success = self._install_from_url(location, skill_name)
        else:
            print(f"Error: Unsupported source type: {source_type}", file=sys.stderr)
            return False

        if not success:
            return False

        # Install dependencies if specified
        install_info = skill_info.get("install", {})
        if install_info:
            self._install_dependencies(skill_name, install_info)

        print(f"\n✓ Skill '{skill_name}' installed successfully!")

        # Show requirements if any
        requires = skill_info.get("requires", {})
        if requires:
            print("\nNote: This skill requires:")
            if "bins" in requires:
                print(f"  - Binaries: {', '.join(requires['bins'])}")
            if "env" in requires:
                print(f"  - Environment variables: {', '.join(requires['env'])}")

        return True

    def _install_from_git(self, url: str, skill_name: Optional[str] = None) -> bool:
        """Install skill from git repository"""
        if not skill_name:
            # Extract skill name from URL
            skill_name = url.rstrip("/").split("/")[-1].replace(".git", "")

        target_dir = self.skills_dir / skill_name

        if target_dir.exists():
            print(f"Warning: Skill directory already exists: {target_dir}")
            response = input("Overwrite? [y/N] ")
            if response.lower() != "y":
                return False
            shutil.rmtree(target_dir)

        print(f"Cloning {url}...")
        try:
            subprocess.run(
                ["git", "clone", url, str(target_dir)],
                check=True,
                capture_output=True,
                text=True
            )

            # Remove .git directory to avoid confusion
            git_dir = target_dir / ".git"
            if git_dir.exists():
                shutil.rmtree(git_dir)

            return True
        except subprocess.CalledProcessError as e:
            print(f"Error cloning repository: {e.stderr}", file=sys.stderr)
            return False
        except FileNotFoundError:
            print("Error: git command not found. Please install git.", file=sys.stderr)
            return False

    def _install_from_url(self, url: str, skill_name: Optional[str] = None) -> bool:
        """Install skill from URL (zip or tar.gz)"""
        if not skill_name:
            # Extract skill name from URL
            skill_name = url.rstrip("/").split("/")[-1]
            for ext in [".zip", ".tar.gz", ".tgz", ".tar.bz2"]:
                skill_name = skill_name.replace(ext, "")

        target_dir = self.skills_dir / skill_name

        if target_dir.exists():
            print(f"Warning: Skill directory already exists: {target_dir}")
            response = input("Overwrite? [y/N] ")
            if response.lower() != "y":
                return False
            shutil.rmtree(target_dir)

        print(f"Downloading from {url}...")

        with tempfile.TemporaryDirectory() as tmpdir:
            # Download file
            tmpfile = Path(tmpdir) / "download"
            try:
                urllib.request.urlretrieve(url, tmpfile)
            except Exception as e:
                print(f"Error downloading: {e}", file=sys.stderr)
                return False

            # Extract archive
            print("Extracting...")
            try:
                if url.endswith(".zip"):
                    import zipfile
                    with zipfile.ZipFile(tmpfile) as zf:
                        zf.extractall(tmpdir)
                else:
                    import tarfile
                    with tarfile.open(tmpfile) as tf:
                        tf.extractall(tmpdir)

                # Find the skill directory (might be in a subdirectory)
                extracted_items = list(Path(tmpdir).iterdir())
                extracted_items = [p for p in extracted_items if p.is_dir() and p.name != "__MACOSX"]

                if len(extracted_items) == 1:
                    # Single directory, move it
                    shutil.move(str(extracted_items[0]), str(target_dir))
                else:
                    # Multiple items, create directory and move all
                    target_dir.mkdir(parents=True, exist_ok=True)
                    for item in extracted_items:
                        if item.name != "download":
                            shutil.move(str(item), str(target_dir / item.name))

                return True
            except Exception as e:
                print(f"Error extracting archive: {e}", file=sys.stderr)
                return False

    def _install_from_local(self, path: str, skill_name: Optional[str] = None) -> bool:
        """Install skill from local path"""
        source_path = Path(path).expanduser().resolve()

        if not source_path.exists():
            print(f"Error: Path does not exist: {path}", file=sys.stderr)
            return False

        if not skill_name:
            skill_name = source_path.name

        target_dir = self.skills_dir / skill_name

        if target_dir.exists():
            print(f"Warning: Skill directory already exists: {target_dir}")
            response = input("Overwrite? [y/N] ")
            if response.lower() != "y":
                return False
            shutil.rmtree(target_dir)

        print(f"Copying from {source_path}...")
        shutil.copytree(source_path, target_dir)
        return True

    def _install_dependencies(self, skill_name: str, install_info: Dict) -> bool:
        """Install dependencies for a skill"""
        kind = install_info.get("kind")

        if not kind:
            return True

        print(f"\nInstalling dependencies ({kind})...")

        try:
            if kind == "pip":
                package = install_info.get("package")
                if package:
                    subprocess.run(["pip", "install", package], check=True)

            elif kind == "npm":
                package = install_info.get("package")
                if package:
                    subprocess.run(["npm", "install", "-g", package], check=True)

            elif kind == "brew":
                formula = install_info.get("formula", install_info.get("package"))
                if formula:
                    subprocess.run(["brew", "install", formula], check=True)

            elif kind == "uv":
                package = install_info.get("package")
                if package:
                    subprocess.run(["uv", "tool", "install", package], check=True)

            else:
                print(f"Warning: Unknown dependency kind: {kind}", file=sys.stderr)
                return False

            print("✓ Dependencies installed")
            return True

        except subprocess.CalledProcessError as e:
            print(f"Warning: Failed to install dependencies: {e}", file=sys.stderr)
            return False
        except FileNotFoundError:
            print(f"Warning: Package manager '{kind}' not found", file=sys.stderr)
            return False


def main():
    parser = argparse.ArgumentParser(description="Install skills")
    parser.add_argument("source", help="Skill name, git URL, or local path")
    parser.add_argument("--method", choices=["registry", "git", "url", "local"],
                       help="Installation method (auto-detected if not specified)")
    parser.add_argument("--skills-dir", help="Skills directory (default: auto-detect)")

    args = parser.parse_args()

    installer = SkillInstaller(args.skills_dir)
    success = installer.install(args.source, args.method)

    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
