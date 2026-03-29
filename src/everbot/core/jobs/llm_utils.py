"""Shared utilities for reflection skills."""

import json
import re


def parse_json_response(response: str) -> dict:
    """Extract JSON from LLM response (handles markdown code blocks)."""
    match = re.search(r"```(?:json)?\s*\n(.*?)\n\s*```", response, re.DOTALL)
    if match:
        return json.loads(match.group(1))
    return json.loads(response.strip())


def parse_system_dph(dph_path: str, variables: dict) -> dict:
    """Parse a system DPH file into configuration and prompt string.
    
    This provides a lightweight way completely decouple backend prompts and configurations
    from Python code without needing a full-blown DolphinReActAgent allocation,
    enabling system jobs to also configure their system prompt, models, etc., cleanly
    inside .dph files like other agents.
    """
    from pathlib import Path
    content = Path(dph_path).read_text(encoding="utf-8")
    
    # 1. Extract config from `/explore/(...)` or similar block
    # Note: we extract key="value" or key='value'
    config = {}
    header_match = re.search(r'/(?:explore|prompt)/\((.*?)\)', content)
    if header_match:
        params_str = header_match.group(1)
        # Regex to find: key="value" or key='value' or key=value (numbers/bools)
        for m in re.finditer(r'([a-zA-Z_]\w*)\s*=\s*(?:"([^"]*)"|\'([^\']*)\'|([A-Za-z0-9_.-]+))', params_str):
            key = m.group(1)
            val = m.group(2) if m.group(2) is not None else (m.group(3) if m.group(3) is not None else m.group(4))
            
            # Simple type conversions for numbers and booleans
            if val is not None:
                if val.lower() == "true":
                    val = True
                elif val.lower() == "false":
                    val = False
                else:
                    try:
                        if "." in val:
                            val = float(val)
                        else:
                            val = int(val)
                    except ValueError:
                        pass # keep as string
            
            config[key] = val
            
    # 2. Slice body text
    body = content.replace(header_match.group(0), "") if header_match else content
    body = re.sub(r'->\s*[a-zA-Z_]\w*', '', body).strip()
    
    # 3. Variable substitutions
    for k, v in variables.items():
        # Substitute $var
        body = body.replace(f"${k}", str(v))
        # Substitute {{var}}
        body = body.replace(f"{{{{{k}}}}}", str(v))
        
    return {
        "config": config,
        "prompt": body
    }

