"""Compatibility layer for the original KOBEDAI dashboard collectors.

The modules in this package deliberately keep parsing separate from network and
credential handling so that the new sync service can inject its own clients.
"""

from . import (
    beef,
    classifier,
    csi,
    dedup,
    gcal,
    inference,
    mail,
    newsnet,
    pdf_calendar,
    sso,
    uribo,
    venture,
    webdata,
)

__all__ = ["beef", "classifier", "csi", "dedup", "gcal", "inference", "mail", "newsnet", "pdf_calendar", "sso", "uribo", "venture", "webdata"]
