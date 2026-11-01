"""The npm tarball is the Pi product boundary, not just package metadata."""
from __future__ import annotations

import json
import os
import subprocess
import unittest


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class PiPackage(unittest.TestCase):
    def manifest(self):
        with open(os.path.join(ROOT, "package.json"), encoding="utf-8") as fh:
            return json.load(fh)

    def test_manifest_declares_the_extension_and_skill(self):
        pkg = self.manifest()
        self.assertEqual(pkg["name"], "pi-sleep-agent")
        self.assertIn("pi-package", pkg["keywords"])
        self.assertEqual(pkg["pi"]["extensions"], ["./extension/index.ts"])
        self.assertEqual(pkg["pi"]["skills"], ["./skills"])
        for relative in ("extension/index.ts", "skills/sleep/SKILL.md", "sleep.py"):
            self.assertTrue(os.path.isfile(os.path.join(ROOT, relative)), relative)

    def test_tarball_contains_runtime_but_no_python_cache(self):
        got = subprocess.run(
            ["npm", "pack", "--dry-run", "--json", "--ignore-scripts"],
            cwd=ROOT, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=60, check=False)
        self.assertEqual(got.returncode, 0, got.stdout + got.stderr)
        payload = json.loads(got.stdout)
        # npm 11: [report]; npm 12: {name: report}
        report = (
            payload[0]
            if isinstance(payload, list)
            else next(iter(payload.values()))
        )
        paths = {item["path"] for item in report["files"]}
        required = {"package.json", "extension/index.ts", "skills/sleep/SKILL.md",
                    "sleep.py", "sleepkit/cli.py", "prompts/consolidate.md",
                    "replay/tasks.json"}
        self.assertEqual(required - paths, set(), sorted(paths))
        self.assertFalse(any("__pycache__" in path or path.endswith((".pyc", ".pyo"))
                             for path in paths), sorted(paths))
        self.assertFalse(any(path.startswith("tests/") for path in paths), sorted(paths))

    def test_packaged_skill_uses_the_runtime_exported_by_extension(self):
        with open(os.path.join(ROOT, "extension", "index.ts"), encoding="utf-8") as fh:
            extension = fh.read()
        with open(os.path.join(ROOT, "skills", "sleep", "SKILL.md"), encoding="utf-8") as fh:
            skill = fh.read()
        self.assertIn("process.env.SLEEPCTL ||= SLEEPCTL", extension)
        self.assertIn('python3 "$SLEEPCTL"', skill)
        self.assertNotIn("~/Research/sleep", skill)


if __name__ == "__main__":
    unittest.main()
