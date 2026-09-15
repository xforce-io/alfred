"""S1 unit test: async_skill_lock uses asyncio.sleep, not time.sleep.

Tests that async_skill_lock with timeout uses asyncio.sleep polling (not blocking)
and raises LockTimeoutError when lock is held, proving chat path won't block event loop.
"""
import asyncio
import tempfile
import threading
import time
from pathlib import Path

import pytest


@pytest.mark.asyncio
async def test_async_skill_lock_timeout_uses_asyncio_sleep():
    """S1: async_skill_lock with timeout raises LockTimeoutError without blocking loop."""
    from src.everbot.core.slm._atomic_io import async_skill_lock, skill_lock, LockTimeoutError
    
    with tempfile.TemporaryDirectory() as tmpdir:
        lock_path = Path(tmpdir) / "test.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        
        # Hold sync lock in a background thread
        lock_held = threading.Event()
        lock_released = threading.Event()
        
        def hold_lock():
            with skill_lock(lock_path, timeout=None):  # Block indefinitely
                lock_held.set()
                lock_released.wait()  # Hold until main thread signals
        
        thread = threading.Thread(target=hold_lock, daemon=True)
        thread.start()
        lock_held.wait()  # Wait for thread to acquire lock
        
        try:
            # Attempt async lock with 2s timeout (chat budget)
            start = time.monotonic()
            with pytest.raises(LockTimeoutError, match="Could not acquire lock .* within 2.0s"):
                async with async_skill_lock(lock_path, timeout=2.0):
                    pass
            elapsed = time.monotonic() - start
            
            # Should timeout in ~2s (not 5s sync default), proving asyncio.sleep not time.sleep
            assert 1.8 <= elapsed <= 2.5, f"Expected ~2s timeout, got {elapsed:.2f}s"
        finally:
            lock_released.set()
            thread.join(timeout=1.0)


@pytest.mark.asyncio
async def test_async_ensure_registered_raises_on_lock_timeout():
    """S1: async_ensure_registered raises on LockTimeoutError (not silent swallow)."""
    from src.everbot.core.slm._atomic_io import skill_lock, LockTimeoutError
    from src.everbot.core.slm.state_normalizer import async_ensure_registered
    from src.everbot.core.slm.version_manager import VersionManager
    
    with tempfile.TemporaryDirectory() as tmpdir:
        skills_dir = Path(tmpdir) / "skills"
        eval_dir = Path(tmpdir) / "eval"
        skills_dir.mkdir(parents=True)
        eval_dir.mkdir(parents=True)
        
        # Create a fake skill
        skill_id = "test_skill"
        skill_path = skills_dir / skill_id / "SKILL.md"
        skill_path.parent.mkdir(parents=True)
        skill_path.write_text("# Test Skill\n\ntest content\n")
        
        vm = VersionManager(skills_dir, eval_base_dir=eval_dir)
        
        # Hold lock in background thread
        lock_path = eval_dir / skill_id / ".lock"
        lock_held = threading.Event()
        lock_released = threading.Event()
        
        def hold_lock():
            with skill_lock(lock_path, timeout=None):
                lock_held.set()
                lock_released.wait()
        
        thread = threading.Thread(target=hold_lock, daemon=True)
        thread.start()
        lock_held.wait()
        
        try:
            # async_ensure_registered should raise LockTimeoutError
            start = time.monotonic()
            with pytest.raises(LockTimeoutError):
                await async_ensure_registered(vm, skill_id)
            elapsed = time.monotonic() - start
            
            # Should timeout in ~2s (chat budget)
            assert 1.8 <= elapsed <= 2.5, f"Expected ~2s timeout, got {elapsed:.2f}s"
        finally:
            lock_released.set()
            thread.join(timeout=1.0)


@pytest.mark.asyncio
async def test_skill_log_recorder_async_maybe_record_raises_on_busy():
    """S1: async_maybe_record converts LockTimeoutError to RuntimeError with Chinese message."""
    from src.everbot.core.slm._atomic_io import skill_lock
    from src.everbot.core.slm.skill_log_recorder import SkillLogRecorder
    
    with tempfile.TemporaryDirectory() as tmpdir:
        skills_dir = Path(tmpdir) / "skills"
        eval_dir = Path(tmpdir) / "eval"
        logs_dir = Path(tmpdir) / "logs"
        skills_dir.mkdir(parents=True)
        eval_dir.mkdir(parents=True)
        logs_dir.mkdir(parents=True)
        
        # Create a fake skill
        skill_id = "test_skill"
        skill_path = skills_dir / skill_id / "SKILL.md"
        skill_path.parent.mkdir(parents=True)
        skill_path.write_text("# Test Skill\n\ntest content\n")
        
        recorder = SkillLogRecorder(
            skill_logs_dir=logs_dir,
            skill_dirs=[skills_dir],
            eval_base_dir=eval_dir,
        )
        
        # Hold lock in background thread
        lock_path = eval_dir / skill_id / ".lock"
        lock_held = threading.Event()
        lock_released = threading.Event()
        
        def hold_lock():
            with skill_lock(lock_path, timeout=None):
                lock_held.set()
                lock_released.wait()
        
        thread = threading.Thread(target=hold_lock, daemon=True)
        thread.start()
        lock_held.wait()
        
        try:
            # async_maybe_record should raise RuntimeError with Chinese message
            with pytest.raises(RuntimeError, match=r"技能 .* 正在更新"):
                await recorder.async_maybe_record(
                    skill_id,
                    session_id="test_session",
                    skill_output="test output",
                    context_before="test context",
                )
        finally:
            lock_released.set()
            thread.join(timeout=1.0)


if __name__ == "__main__":
    # Run tests with: PYTHONPATH=src python -m pytest tests/unit/test_s1_async_skill_lock.py -v
    pytest.main([__file__, "-v"])
