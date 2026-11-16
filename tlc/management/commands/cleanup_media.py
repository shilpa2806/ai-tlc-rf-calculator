"""Management command that prunes old files from ``MEDIA_ROOT`` now.

The app also prunes by itself (see :mod:`tlc.media`); this command is for
running a sweep on demand, or from cron with automatic sweeps turned off::

    python manage.py cleanup_media --hours 24 [--dry-run]
"""

from __future__ import annotations

import os
from typing import Any

from django.conf import settings
from django.core.management.base import BaseCommand, CommandParser

from tlc import media


class Command(BaseCommand):
    help = "Delete media files unused for the given age (default: MEDIA_MAX_AGE_HOURS, or 24)."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument(
            "--hours",
            type=float,
            default=None,
            help="Delete files unused for this many hours "
            "(default: MEDIA_MAX_AGE_HOURS, or 24 when that is 0).",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="List what would be deleted without removing anything.",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        if not os.path.isdir(settings.MEDIA_ROOT):
            self.stdout.write(f"MEDIA_ROOT does not exist: {settings.MEDIA_ROOT}")
            return
        hours = options["hours"]
        if hours is None:
            hours = settings.MEDIA_MAX_AGE_HOURS or 24.0

        if options["dry_run"]:
            names = [entry.name for entry in media.stale_files(hours)]
            for name in names:
                self.stdout.write(f"[dry-run] would delete {name}")
            self.stdout.write(self.style.SUCCESS(f"Done: would remove {len(names)} file(s)."))
            return
        removed = media.prune(hours)
        self.stdout.write(self.style.SUCCESS(f"Done: removed {removed} file(s)."))
