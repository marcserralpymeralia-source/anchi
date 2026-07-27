from __future__ import annotations

import os
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.timezones import format_local_datetime
from app.core.templating import TEMPLATES_DIR, templates


class TemplatingPathTests(unittest.TestCase):
    def test_templates_directory_and_login_template_exist(self):
        self.assertTrue(TEMPLATES_DIR.exists())
        self.assertTrue((TEMPLATES_DIR / "login.html").exists())

    def test_login_template_loads_independently_of_cwd(self):
        current = Path.cwd()
        try:
            os.chdir("/tmp")
            template = templates.get_template("login.html")
            self.assertEqual(template.name, "login.html")
        finally:
            os.chdir(current)

    def test_local_datetime_converts_utc_to_madrid(self):
        value = datetime(2026, 7, 27, 10, 0, tzinfo=timezone.utc)
        self.assertEqual(format_local_datetime(value, "Europe/Madrid", "%d/%m/%Y %H:%M"), "27/07/2026 12:00")


if __name__ == "__main__":
    unittest.main()
