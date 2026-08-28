"""Compatibility entry points for BEEF+ Venture (implemented by :mod:`beef`)."""
from .beef import VENTURE_BASE, VENTURE_IDNUMBER, fetch_pages, parse_venture_pages

__all__ = ["VENTURE_BASE", "VENTURE_IDNUMBER", "fetch_pages", "parse_venture_pages"]
