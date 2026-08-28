"""Pure parsers for BEEF+ course pages and their detail dialogs.

This package deliberately performs no HTTP requests.  A sync worker supplies
saved HTML and persists the returned dictionaries in its own database layer.
"""

from .parser import (
    parse_announcement_detail,
    parse_assignment_detail,
    parse_course_page,
)

__all__ = [
    "parse_course_page",
    "parse_announcement_detail",
    "parse_assignment_detail",
]
