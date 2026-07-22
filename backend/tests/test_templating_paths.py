from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

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


if __name__ == "__main__":
    unittest.main()
