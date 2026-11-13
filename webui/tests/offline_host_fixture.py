"""Disable catalog networking in UI fixtures that do not exercise the market."""

from contextlib import contextmanager
from unittest.mock import patch

import heredity.application as application


@contextmanager
def offline_marketplace():
    if not hasattr(application, "Marketplace"):
        yield
        return
    factory = application.Marketplace

    def create(*args, **options):
        options["auto_refresh"] = False

        def offline_url(*values):
            raise AssertionError("This UI fixture has no catalog or package network access")

        options["_open_url"] = offline_url
        return factory(*args, **options)

    with patch.object(application, "Marketplace", side_effect=create):
        yield
