---
name: skill-installer
description: Install and manage skills dynamically through conversation. Search, install, update, list, and remove skills from the registry.
---

# Skill Installer

Install and manage skills dynamically through conversation.

## Usage

This skill allows you to:
- Search for available skills in the registry
- Install new skills from various sources
- Update existing skills
- List installed and available skills

## Commands

### Search for skills
```
User: "Search for PDF editing skills"
You: Use skill-installer to search the registry
```

### Install a skill
```
User: "Install the calendar skill"
You: Use skill-installer to install from registry or URL
```

### List skills
```
User: "What skills are available?"
You: Use skill-installer to list all skills with their status
```

### Update skills
```
User: "Update the calendar skill"
You: Use skill-installer to update the skill to the latest version
```

### Remove skills
```
User: "Remove the old-skill"
You: Use skill-installer to remove the installed skill
```

## Functions

### search_skills
Search for skills in the registry by keyword.

**Parameters:**
- `query` (string): Search keyword or phrase

**Returns:** List of matching skills with descriptions and install info

### install_skill
Install a skill from the registry or a URL.

**Parameters:**
- `source` (string): Skill name from registry, git URL, or local path
- `method` (string, optional): Installation method - "registry", "git", "url", "local"

**Returns:** Installation status and any required next steps

### list_skills
List all installed and available skills.

**Parameters:**
- `filter` (string, optional): Filter by status - "all", "installed", "available", "needs-update"

**Returns:** Skills list with status, version, and description

### update_skill
Update an installed skill to the latest version.

**Parameters:**
- `skill_name` (string): Name of the skill to update

**Returns:** Update status

### remove_skill
Remove an installed skill.

**Parameters:**
- `skill_name` (string): Name of the skill to remove

**Returns:** Removal status

## Installation Methods

The skill-installer supports multiple installation methods:

1. **Registry** (default): Install from skill registry (JSON index)
2. **Git**: Clone from a git repository
3. **URL**: Download from a direct URL (zip/tar.gz)
4. **Local**: Copy from a local path

## Skill Registry

The registry is a JSON file that can be:
- Local: `~/.alfred/skills-registry.json`
- Remote: URL specified in config `skill_installer.registry_url`

Registry format:
```json
{
  "skills": {
    "skill-name": {
      "name": "Skill Display Name",
      "description": "What this skill does",
      "version": "1.0.0",
      "source": {
        "type": "git|url|npm|pip",
        "location": "https://github.com/user/skill-name"
      },
      "install": {
        "kind": "pip|npm|brew|download",
        "package": "package-name",
        "bins": ["binary-name"]
      },
      "requires": {
        "bins": ["required-binary"],
        "env": ["API_KEY"]
      }
    }
  }
}
```

## Configuration

Add to your `config/dolphin.yaml`:

```yaml
skill_installer:
  registry_url: "https://raw.githubusercontent.com/your-org/skill-registry/main/registry.json"
  default_method: "registry"
  auto_install_deps: true
  skills_dir: "~/.alfred/skills"  # Will be auto-detected from resource_skills.directories
```

## Examples

### Example 1: Search and Install
```
User: "I need to work with PDFs"
Assistant: Let me search for PDF-related skills...
[Calls search_skills with query="PDF"]
Found: nano-pdf, pdf-viewer, pdf-merge
User: "Install nano-pdf"
Assistant: [Calls install_skill with source="nano-pdf"]
Installed nano-pdf successfully. The skill requires API_KEY environment variable.
```

### Example 2: Install from URL
```
User: "Install skill from https://github.com/user/my-skill"
Assistant: [Calls install_skill with source="https://github.com/user/my-skill", method="git"]
Cloned and installed my-skill successfully.
```

## Implementation Details

The skill uses the following scripts:
- `search.py`: Search the registry
- `install.py`: Handle installation logic
- `list.py`: List skills and their status
- `update.py`: Update existing skills
- `remove.py`: Remove skills
- `registry.py`: Manage registry operations

All scripts are located in the `scripts/` subdirectory.
