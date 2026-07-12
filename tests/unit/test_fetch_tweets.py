"""Unit tests for skills/twitter-watch/scripts/fetch_tweets.py.

Tests cover two bugs:
1. Long tweets truncated by X's "Show more" fold were never expanded.
2. Scroll-loop exit condition counted pinned tweets, causing under-collection.
"""
import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType
from unittest.mock import MagicMock, patch


def _load_module() -> ModuleType:
    path = Path("skills/twitter-watch/scripts/fetch_tweets.py").resolve()
    spec = importlib.util.spec_from_file_location("fetch_tweets_test", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------
# TSX script structure — verify browser-side behaviour
# ---------------------------------------------------------------------------

def test_tsx_detects_show_more_link():
    """_TSX must query tweet-text-show-more-link to detect truncated tweets."""
    m = _load_module()
    assert "tweet-text-show-more-link" in m._TSX, (
        "_TSX should detect the 'Show more' link via [data-testid='tweet-text-show-more-link']"
    )


def test_tsx_navigates_to_tweet_url_for_full_text():
    """_TSX must navigate to individual tweet URL to fetch full text for truncated tweets."""
    m = _load_module()
    # Should contain logic to go to t.url when t.truncated is true
    assert "t.truncated" in m._TSX or "truncated" in m._TSX, (
        "_TSX should handle the 'truncated' flag set on each tweet object"
    )
    assert "page.goto" in m._TSX, (
        "_TSX should navigate (page.goto) to individual tweet URLs to expand full text"
    )


def test_tsx_loop_exits_on_non_pinned_count():
    """Scroll loop must count only non-pinned tweets, not all collected tweets."""
    m = _load_module()
    # Old (broken) condition: seen.size < count
    # New condition should check non-pinned count, NOT seen.size
    assert "seen.size < count" not in m._TSX, (
        "Loop exit condition must not use seen.size (which includes pinned tweets); "
        "use a non-pinned count check instead"
    )
    # Verify the script filters is_pinned before checking count
    assert "is_pinned" in m._TSX, "_TSX should reference is_pinned for the count check"


def test_tsx_strips_truncated_field_from_output():
    """The internal 'truncated' flag must not leak into the JSON output."""
    m = _load_module()
    # delete t.truncated should appear before the final console.log
    assert "delete t.truncated" in m._TSX, (
        "_TSX should delete the internal 'truncated' field before serializing output"
    )


# ---------------------------------------------------------------------------
# Python wrapper — JSON extraction and handle normalization
# ---------------------------------------------------------------------------

def test_handle_strips_at_sign():
    """main() must strip a leading '@' from the handle before passing TW_HANDLE to tsx."""
    m = _load_module()

    captured_env = {}
    fake_proc = MagicMock()
    fake_proc.returncode = 0
    fake_proc.stdout = (
        '{"handle": "SerenityBot", "count": 1, "tweets": ['
        '{"text": "hi", "ts": "2024-01-01T00:00:00Z",'
        ' "url": "https://x.com/SerenityBot/status/1", "is_pinned": false, "metrics": ""}'
        "]}\n"
    )
    fake_proc.stderr = ""

    def capture_env(*args, **kwargs):
        captured_env.update(kwargs.get("env", {}))
        return fake_proc

    with patch("subprocess.run", side_effect=capture_env), \
         patch.object(m, "_ensure_browser_server", return_value=None), \
         patch("sys.argv", ["fetch_tweets.py", "@SerenityBot"]):
        m.main()

    assert captured_env.get("TW_HANDLE") == "SerenityBot", (
        "main() must strip '@' before passing TW_HANDLE to tsx"
    )


def test_json_extraction_picks_last_json_line(capsys):
    """Python wrapper must pick the last JSON line from tsx stdout (ignoring loader noise)."""
    m = _load_module()

    fake_proc = MagicMock()
    fake_proc.returncode = 0
    fake_proc.stdout = (
        "npx notice: some loader line\n"
        '{"handle": "foo", "count": 1, "tweets": ['
        '{"text": "hi", "ts": "2024-01-01T00:00:00Z",'
        ' "url": "https://x.com/foo/status/1", "is_pinned": false, "metrics": ""}'
        "]}\n"
    )
    fake_proc.stderr = ""

    with patch("subprocess.run", return_value=fake_proc), \
         patch.object(m, "_ensure_browser_server", return_value=None), \
         patch("sys.argv", ["fetch_tweets.py", "foo"]):
        m.main()

    out = capsys.readouterr().out
    data = json.loads(out)
    assert data["handle"] == "foo"
    assert data["count"] == 1


def test_main_exits_on_empty_tweets(capsys, monkeypatch):
    """main() must sys.exit when the JSON tweets list is empty."""
    m = _load_module()

    fake_proc = MagicMock()
    fake_proc.returncode = 0
    fake_proc.stdout = '{"handle": "nobody", "count": 0, "tweets": []}\n'
    fake_proc.stderr = ""

    monkeypatch.setattr("socket.socket", MagicMock())
    with patch("subprocess.run", return_value=fake_proc), \
         patch.object(m, "_ensure_browser_server", return_value=None), \
         patch("sys.argv", ["fetch_tweets.py", "nobody"]):
        with pytest.raises(SystemExit):
            m.main()


def test_browser_start_uses_the_owned_lifecycle_entrypoint_once():
    """A missing server is started once through server.sh, never through an ad-hoc Popen."""
    m = _load_module()
    started = MagicMock(return_value=MagicMock(returncode=0, stdout="", stderr=""))

    with patch.object(m, "_port_open", side_effect=[False, True]), \
         patch("subprocess.run", started), \
         patch("time.sleep", return_value=None):
        m._ensure_browser_server()

    started.assert_called_once()
    command = started.call_args.args[0]
    assert command[:3] == ["bash", str(Path(m.WEB_SKILL_DIR) / "server.sh"), "start"]


# Make pytest available in namespace for the last test
import pytest
