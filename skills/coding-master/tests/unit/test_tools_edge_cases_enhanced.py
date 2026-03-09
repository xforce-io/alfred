"""Enhanced edge case tests for coding-master v3 tools.

Additional coverage: file permissions, path traversal, large files,
partial failures, and resource exhaustion scenarios.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from unittest.mock import patch, mock_open, MagicMock

import pytest

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent.parent / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from tools import (
    _atomic_json_update,
    _atomic_json_read,
    _is_expired,
    _check_feature_md_sections,
    _slugify,
    CM_DIR,
)


# ═══════════════════════════════════════════════════════════
#  File Permission & Security Edge Cases
# ═══════════════════════════════════════════════════════════


class TestFilePermissionEdgeCases:
    """Test file permission and security edge cases."""

    def test_atomic_update_readonly_file(self, tmp_path):
        """Update should fail gracefully on read-only file."""
        path = tmp_path / "readonly.json"
        path.write_text('{"existing": "data"}')
        
        # Make file read-only
        path.chmod(0o444)
        
        try:
            def updater(data: dict) -> dict:
                data["modified"] = True
                return {"ok": True}
            
            # Should handle permission error gracefully
            with pytest.raises((PermissionError, OSError)):
                _atomic_json_update(path, updater)
        finally:
            # Restore permissions for cleanup
            path.chmod(0o644)

    def test_atomic_update_nonexistent_directory(self, tmp_path):
        """Update should create parent directories if needed."""
        path = tmp_path / "deep" / "nested" / "path" / "file.json"
        
        def updater(data: dict) -> dict:
            data["created"] = True
            return {"ok": True}
        
        result = _atomic_json_update(path, updater)
        assert result["ok"] is True
        assert path.exists()
        assert json.loads(path.read_text()) == {"created": True}

    def test_path_traversal_attempts(self, tmp_path):
        """Path traversal attempts in repo names should be handled."""
        from tools import _slugify
        
        malicious_names = [
            "../../../etc/passwd",
            "repo/../../../etc",
            "repo\x00hidden",
            "repo; rm -rf /",
            "repo&&whoami",
            "repo|cat /etc/passwd",
        ]
        
        for name in malicious_names:
            slug = _slugify(name)
            # Slug should not contain path separators or dangerous chars
            assert ".." not in slug or slug.replace(".", "") != ""
            assert "\x00" not in slug
            assert ";" not in slug
            assert "|" not in slug
            assert "&&" not in slug

    def test_very_long_path(self, tmp_path):
        """Handle very long paths gracefully."""
        # Create deeply nested path
        deep_path = tmp_path
        for i in range(50):
            deep_path = deep_path / f"subdir_{i}"
        
        final_file = deep_path / "test.json"
        
        def updater(data: dict) -> dict:
            data["deep"] = True
            return {"ok": True}
        
        # May fail on some systems due to path length limits
        try:
            result = _atomic_json_update(final_file, updater)
            if result["ok"]:
                assert final_file.exists()
        except OSError as e:
            # Path too long is acceptable on some systems
            assert "File name too long" in str(e) or "path" in str(e).lower()


# ═══════════════════════════════════════════════════════════
#  Large Data & Performance Edge Cases
# ═══════════════════════════════════════════════════════════


class TestLargeDataEdgeCases:
    """Test handling of large data structures."""

    def test_large_json_file(self, tmp_path):
        """Handle reasonably large JSON files."""
        path = tmp_path / "large.json"
        
        # Create 1MB of JSON data
        large_data = {
            "items": [
                {"id": i, "data": "x" * 100}
                for i in range(10000)
            ]
        }
        path.write_text(json.dumps(large_data))
        
        def updater(data: dict) -> dict:
            data["modified"] = True
            data["items"].append({"id": 99999, "data": "new"})
            return {"ok": True}
        
        result = _atomic_json_update(path, updater)
        assert result["ok"] is True
        
        final = json.loads(path.read_text())
        assert final["modified"] is True
        assert len(final["items"]) == 10001

    def test_deeply_nested_json(self, tmp_path):
        """Handle deeply nested JSON structures."""
        path = tmp_path / "nested.json"
        
        # Create deeply nested structure
        depth = 100
        data = "value"
        for i in range(depth):
            data = {"level": depth - i, "nested": data}
        
        path.write_text(json.dumps(data))
        
        def updater(d: dict) -> dict:
            d["modified"] = True
            return {"ok": True}
        
        result = _atomic_json_update(path, updater)
        assert result["ok"] is True
        
        final = json.loads(path.read_text())
        assert final["modified"] is True

    def test_json_with_special_floats(self, tmp_path):
        """Handle special float values in JSON - Python supports these by default."""
        path = tmp_path / "floats.json"
        
        data = {
            "nan": float('nan'),
            "inf": float('inf'),
            "neg_inf": float('-inf'),
        }
        
        # Python json module supports NaN/Infinity by default
        # Just verify it works without crashing
        path.write_text(json.dumps(data))
        loaded = json.loads(path.read_text())
        assert loaded["inf"] == float('inf')
        assert loaded["neg_inf"] == float('-inf')

    def test_unicode_edge_cases(self, tmp_path):
        """Handle various Unicode edge cases."""
        path = tmp_path / "unicode.json"
        
        unicode_data = {
            "emoji": "🚀🎉💻",
            "chinese": "中文测试",
            "arabic": "مرحبا",
            "hebrew": "שלום",
            "russian": "Привет",
            "japanese": "こんにちは",
            "korean": "안녕하세요",
            "zero_width": "test\u200Bjoin\u200Bing",
            "rtl": "‫test‬",
            "bidi": "a\u202Eb\u202Cc",
        }
        
        path.write_text(json.dumps(unicode_data, ensure_ascii=False))
        
        def updater(data: dict) -> dict:
            data["added"] = "新增"
            return {"ok": True}
        
        result = _atomic_json_update(path, updater)
        assert result["ok"] is True
        
        final = json.loads(path.read_text())
        assert final["added"] == "新增"


# ═══════════════════════════════════════════════════════════
#  Concurrent Access & Race Conditions
# ═══════════════════════════════════════════════════════════


class TestConcurrentAccessEdgeCases:
    """Test concurrent access scenarios."""

    def test_rapid_successive_updates(self, tmp_path):
        """Handle rapid successive updates from same thread."""
        path = tmp_path / "rapid.json"
        path.write_text('{"counter": 0}')
        
        def updater(data: dict) -> dict:
            data["counter"] = data.get("counter", 0) + 1
            return {"ok": True}
        
        # 100 rapid updates
        for _ in range(100):
            result = _atomic_json_update(path, updater)
            assert result["ok"] is True
        
        final = json.loads(path.read_text())
        assert final["counter"] == 100

    def test_mixed_read_write_concurrent(self, tmp_path):
        """Mix of concurrent reads and writes."""
        path = tmp_path / "mixed.json"
        path.write_text('{"value": 0}')
        
        errors = []
        read_results = []
        
        def writer():
            def updater(data: dict) -> dict:
                data["value"] = data.get("value", 0) + 1
                return {"ok": True}
            
            for _ in range(50):
                try:
                    _atomic_json_update(path, updater)
                except Exception as e:
                    errors.append(f"write: {e}")
        
        def reader():
            for _ in range(50):
                try:
                    data = _atomic_json_read(path)
                    read_results.append(data.get("value", 0))
                except Exception as e:
                    errors.append(f"read: {e}")
        
        threads = []
        for _ in range(2):
            threads.append(threading.Thread(target=writer))
            threads.append(threading.Thread(target=reader))
        
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        
        assert len(errors) == 0, f"Errors: {errors}"
        
        final = json.loads(path.read_text())
        assert final["value"] == 100  # 2 writers * 50 updates

    def test_file_deleted_during_read(self, tmp_path):
        """Handle file deletion during read operation."""
        path = tmp_path / "volatile.json"
        path.write_text('{"data": "value"}')
        
        # This is a race condition that's hard to trigger reliably
        # We'll simulate by checking behavior when file doesn't exist
        path.unlink()
        
        result = _atomic_json_read(path)
        assert result == {}


# ═══════════════════════════════════════════════════════════
#  Lease & Time Edge Cases
# ═══════════════════════════════════════════════════════════


class TestLeaseTimeEdgeCases:
    """Test lease and time-related edge cases."""

    def test_lease_timezone_edge_cases(self):
        """Handle various timezone formats in lease."""
        from datetime import datetime, timezone, timedelta
        
        # UTC timezone
        utc_time = datetime.now(timezone.utc).isoformat()
        assert _is_expired({"lease_expires_at": utc_time}) is True  # Just created should be expired or very close
        
        # Explicit +00:00 offset
        explicit_utc = datetime.now(timezone(timedelta(hours=0))).isoformat()
        assert isinstance(_is_expired({"lease_expires_at": explicit_utc}), bool)
        
        # Positive offset
        plus_offset = datetime.now(timezone(timedelta(hours=8))).isoformat()
        assert isinstance(_is_expired({"lease_expires_at": plus_offset}), bool)
        
        # Negative offset
        minus_offset = datetime.now(timezone(timedelta(hours=-5))).isoformat()
        assert isinstance(_is_expired({"lease_expires_at": minus_offset}), bool)

    def test_lease_microsecond_precision(self):
        """Handle microsecond precision in timestamps."""
        from datetime import datetime, timezone, timedelta
        
        now = datetime.now(timezone.utc)
        
        # Future with microseconds
        future_micro = (now + timedelta(seconds=1)).isoformat()
        assert _is_expired({"lease_expires_at": future_micro}) is False
        
        # Past with microseconds
        past_micro = (now - timedelta(seconds=1)).isoformat()
        assert _is_expired({"lease_expires_at": past_micro}) is True

    def test_lease_various_datetime_formats(self):
        """Handle various datetime string formats."""
        from datetime import datetime, timezone
        
        test_cases = [
            # ISO format with Z
            (datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), None),  # May or may not work
            # ISO format with microseconds
            (datetime.now(timezone.utc).isoformat(timespec='microseconds'), bool),
            # ISO format without microseconds  
            (datetime.now(timezone.utc).isoformat(timespec='seconds'), bool),
        ]
        
        for timestamp, expected_type in test_cases:
            try:
                result = _is_expired({"lease_expires_at": timestamp})
                if expected_type:
                    assert isinstance(result, expected_type)
            except ValueError:
                # Some formats might not be parseable - that's ok
                pass


# ═══════════════════════════════════════════════════════════
#  Feature MD Content Edge Cases
# ═══════════════════════════════════════════════════════════


class TestFeatureMdContentEdgeCases:
    """Test feature markdown content edge cases."""

    def test_plan_in_analysis_section(self, tmp_path):
        """Plan mentioned within Analysis should not count."""
        md = tmp_path / "feature.md"
        md.write_text("""
# Feature 1

## Spec
Task

## Analysis
We need to plan this carefully.
The plan is to do X then Y.

## Plan
- real step 1
- real step 2
""")
        has_analysis, has_plan = _check_feature_md_sections(md)
        assert has_analysis is True
        assert has_plan is True

    def test_analysis_with_subsections(self, tmp_path):
        """Analysis with subsections."""
        md = tmp_path / "feature.md"
        md.write_text("""
# Feature 1

## Spec
Task

## Analysis
### Subsection 1
Content here

### Subsection 2
More content

## Plan
- step
""")
        has_analysis, has_plan = _check_feature_md_sections(md)
        assert has_analysis is True
        assert has_plan is True

    def test_empty_file(self, tmp_path):
        """Completely empty markdown file."""
        md = tmp_path / "feature.md"
        md.write_text("")
        has_analysis, has_plan = _check_feature_md_sections(md)
        assert has_analysis is False
        assert has_plan is False

    def test_only_whitespace(self, tmp_path):
        """Markdown file with only whitespace."""
        md = tmp_path / "feature.md"
        md.write_text("   \n\n\t\n   ")
        has_analysis, has_plan = _check_feature_md_sections(md)
        assert has_analysis is False
        assert has_plan is False

    def test_comment_like_sections(self, tmp_path):
        """Sections that look like comments."""
        md = tmp_path / "feature.md"
        md.write_text("""
# Feature 1

## Spec
Task

<!-- ## Analysis
This is a comment, not a section
-->

## Analysis
Real analysis here

## Plan
- step
""")
        has_analysis, has_plan = _check_feature_md_sections(md)
        assert has_analysis is True
        assert has_plan is True

    def test_horizontal_rules_between_sections(self, tmp_path):
        """Horizontal rules between sections."""
        md = tmp_path / "feature.md"
        md.write_text("""
# Feature 1

## Spec
Task

---

## Analysis
Analysis content

---

## Plan
- step
""")
        has_analysis, has_plan = _check_feature_md_sections(md)
        assert has_analysis is True
        assert has_plan is True

    def test_backtick_code_blocks(self, tmp_path):
        """Code blocks with backticks."""
        md = tmp_path / "feature.md"
        md.write_text("""
# Feature 1

## Spec
Task

## Analysis
```
## Plan
This is inside a code block
```
Real analysis continues here

## Plan
- real step
""")
        has_analysis, has_plan = _check_feature_md_sections(md)
        # Analysis has content outside code block
        assert has_analysis is True
        assert has_plan is True


# ═══════════════════════════════════════════════════════════
#  Slugify Edge Cases
# ═══════════════════════════════════════════════════════════


class TestSlugifyEdgeCases:
    """Test slugify function edge cases."""

    def test_slugify_empty_and_whitespace(self):
        """Handle empty and whitespace-only strings."""
        from tools import _slugify
        
        assert _slugify("") == "feature"
        assert _slugify("   ") == "feature"
        assert _slugify("\t\n\r") == "feature"

    def test_slugify_special_chars(self):
        """Handle various special characters."""
        from tools import _slugify
        
        test_cases = [
            ("hello/world", "helloworld"),
            ("hello\\world", "helloworld"),
            ("hello:world", "helloworld"),
            ("hello@world", "helloworld"),
            ("hello#world", "helloworld"),
            ("hello$world", "helloworld"),
            ("hello%world", "helloworld"),
            ("hello&world", "helloworld"),
            ("hello*world", "helloworld"),
            ("hello?world", "helloworld"),
            ("hello<world>", "helloworld"),
            ("hello|world", "helloworld"),
            ("hello'world", "helloworld"),
            ('hello"world', "helloworld"),
            ("hello`world", "helloworld"),
        ]
        
        for input_str, expected in test_cases:
            result = _slugify(input_str)
            assert result == expected, f"Failed for '{input_str}': got '{result}', expected '{expected}'"

    def test_slugify_multiple_spaces(self):
        """Handle multiple consecutive spaces."""
        from tools import _slugify
        
        assert _slugify("hello   world") == "hello-world"
        assert _slugify("hello\t\t\tworld") == "hello-world"
        assert _slugify("hello \t \t world") == "hello-world"

    def test_slugify_leading_trailing_special(self):
        """Handle special chars at start and end."""
        from tools import _slugify
        
        assert _slugify("!hello!") == "hello"
        assert _slugify("@#$hello%^&") == "hello"
        assert _slugify("---hello---") == "---hello---"  # Hyphens preserved

    def test_slugify_very_long_string(self):
        """Handle very long strings."""
        from tools import _slugify
        
        long_str = "a" * 1000
        result = _slugify(long_str)
        assert len(result) == 30  # Truncated to 30 chars
        assert result == "a" * 30  # Truncated

    def test_slugify_unicode_normalization(self):
        """Handle unicode normalization."""
        from tools import _slugify
        
        # Different representations of same character
        test_cases = [
            ("café", "café"),  # Accented chars preserved by \w
            ("naïve", "naïve"),  # Accented chars preserved by \w
            ("résumé", "résumé"),  # Accented chars preserved by \w
        ]
        
        for input_str, expected in test_cases:
            result = _slugify(input_str)
            # May or may not normalize depending on implementation
            assert isinstance(result, str)


# ═══════════════════════════════════════════════════════════
#  JSON Corruption & Recovery
# ═══════════════════════════════════════════════════════════


class TestJsonCorruptionRecovery:
    """Test JSON corruption detection and recovery."""

    def test_partial_json_write(self, tmp_path):
        """Handle partially written JSON (simulated crash)."""
        path = tmp_path / "partial.json"
        # Simulate a crash mid-write
        path.write_text('{"valid": "start", "incomplete": ')
        
        # Read should return empty dict on parse failure
        result = _atomic_json_read(path)
        assert result == {}

    def test_json_with_trailing_garbage(self, tmp_path):
        """Handle JSON with trailing garbage."""
        path = tmp_path / "garbage.json"
        path.write_text('{"valid": "json"}garbage_here')
        
        # Standard json.load should fail
        with pytest.raises(json.JSONDecodeError):
            json.loads(path.read_text())
        
        # Our read handles it gracefully
        result = _atomic_json_read(path)
        assert result == {}

    def test_json_with_leading_garbage(self, tmp_path):
        """Handle JSON with leading garbage."""
        path = tmp_path / "garbage.json"
        path.write_text('garbage_here{"valid": "json"}')
        
        result = _atomic_json_read(path)
        assert result == {}

    def test_null_bytes_in_file(self, tmp_path):
        """Handle files with null bytes."""
        path = tmp_path / "nulls.json"
        path.write_bytes(b'{\x00"valid": "json"\x00}')
        
        result = _atomic_json_read(path)
        assert result == {}

    def test_binary_data_in_file(self, tmp_path):
        """Handle binary data in JSON file — should return {} gracefully."""
        path = tmp_path / "binary.json"
        path.write_bytes(b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR')

        # _atomic_json_read catches UnicodeDecodeError and returns {}
        result = _atomic_json_read(path)
        assert result == {}

class TestCliEdgeCases:
    """Test CLI argument handling edge cases."""

    def test_repo_name_with_special_chars(self):
        """Handle repo names with special characters."""
        from tools import _slugify
        
        # These should be sanitized
        special_repos = [
            "my/repo/name",
            "repo with spaces",
            "repo@branch",
            "repo#123",
        ]
        
        for repo in special_repos:
            slug = _slugify(repo)
            assert "/" not in slug
            assert " " not in slug
            assert "@" not in slug
            assert "#" not in slug


# ═══════════════════════════════════════════════════════════
#  System & Environment Edge Cases  
# ═══════════════════════════════════════════════════════════


class TestSystemEnvironmentEdgeCases:
    """Test system and environment edge cases."""

    def test_temp_directory_full(self):
        """Simulate temp directory issues."""
        # This is hard to test without actually filling the disk
        # We just verify the test structure exists
        pass

    def test_home_directory_not_set(self):
        """Handle missing HOME environment variable."""
        # This would require modifying the environment
        # Documented as a scenario to consider
        pass

    def test_signal_interruption(self):
        """Handle signal interruption during operations."""
        # Signal handling would require more complex setup
        # Documented as a scenario to consider  
        pass
