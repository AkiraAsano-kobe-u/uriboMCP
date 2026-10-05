"""Read-only support for the Moodle-based LMS BEEF (beef.center.kobe-u.ac.jp)."""

from uribo_mcp.moodle.client import MoodleReadOnlyClient, moodle_base_url

__all__ = ["MoodleReadOnlyClient", "moodle_base_url"]
