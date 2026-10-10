import unittest

from tools import collect_control_plane as cp


class MarkdownProjectionTests(unittest.TestCase):
    def test_table_parser_is_bounded_to_named_section(self):
        text = """## Physical fleet
| Host | Primary role |
|---|---|
| **alpha** | Control plane |
| `charlie` | Compute |

## Other
| Ignore | Me |
|---|---|
| x | y |
"""
        rows = cp._table(text, "Physical fleet")
        self.assertEqual(rows, [
            {"host": "alpha", "primary_role": "Control plane"},
            {"host": "charlie", "primary_role": "Compute"},
        ])

    def test_revision_extractors_cover_five_indexes(self):
        fixtures = {
            "fleet-index.md": "Fleet Index version: 2026-08-01.5",
            "roadmap-index.md": "Roadmap index revision 2026-08-01.3",
            "conventions-index.md": "Index revision 2026-08-01.2",
            "instructions-index.md": "Parity revision: `INSTRUCTION-PARITY-1`",
            "automation-index.md": "Registry revision: `AUTOMATION-REGISTRY-1`",
        }
        for name, text in fixtures.items():
            self.assertNotEqual(cp._revision(name, text), "unversioned")

    def test_project_stamp_summary_uses_declared_projects_and_ok_count(self):
        stamps = {"projects": [
            {"project": "a", "status": "ok"},
            {"project": "b", "status": "missing"},
            {"project": "c", "status": "ok"},
        ]}
        self.assertEqual(cp._project_stamp_summary(stamps), "2/3 project stamps")


if __name__ == "__main__":
    unittest.main()


class LintLegRenderingTests(unittest.TestCase):
    """Regression: vault_lint.py emits ok/issues and no `overall`, while
    _run_json's failure envelope emits `overall` and no `ok`. Keying on
    `overall` inverted them -- a real lint failure rendered as the
    probe-failure word "unknown" and never named the offending file, while an
    unreachable linter rendered as a definite "error". Cost ~25 minutes of
    misdirected diagnosis on 2026-08-03 (a stray .DS_Store read as a
    conventions-index problem)."""

    def test_real_lint_failure_names_the_offending_file(self):
        state, text = cp._lint_leg({
            "ok": False, "error_count": 1,
            "issues": [{"code": "ds-store", "detail": "stray .DS_Store",
                        "path": "homelab-vault/.DS_Store"}],
        })
        self.assertEqual(state, "error")
        self.assertIn("stray .DS_Store", text)
        self.assertIn("homelab-vault/.DS_Store", text)
        self.assertNotIn("unknown", text)

    def test_multiple_issues_are_counted_and_truncated(self):
        state, text = cp._lint_leg({
            "ok": False, "error_count": 3,
            "issues": [{"code": "a", "detail": "first", "path": "p"},
                       {"code": "b", "detail": "second"},
                       {"code": "c", "detail": "third"}],
        })
        self.assertEqual(state, "error")
        self.assertIn("3 errors", text)
        self.assertIn("+2 more", text)

    def test_healthy_lint(self):
        self.assertEqual(cp._lint_leg({"ok": True, "error_count": 0, "issues": []}),
                         ("ok", "lint ok"))

    def test_unreachable_linter_is_unknown_not_error(self):
        state, text = cp._lint_leg({"overall": "error", "error": "TimeoutExpired"})
        self.assertEqual(state, "unknown")
        self.assertIn("unavailable", text)

    def test_status_precedence_error_beats_unknown_beats_ok(self):
        self.assertEqual(cp._conventions_status("error", {"overall": "ok"}), "error")
        self.assertEqual(cp._conventions_status("unknown", {"overall": "ok"}), "unknown")
        self.assertEqual(cp._conventions_status("ok", {"overall": "ok"}), "ok")
        self.assertEqual(cp._conventions_status("ok", {"overall": "error"}), "error")
        self.assertEqual(cp._conventions_status("unknown", {"overall": "error"}), "error")
