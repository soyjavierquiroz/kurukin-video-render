import unittest

from scripts import check_upstream_updates


class TestCheckUpstreamUpdates(unittest.TestCase):
    def test_classifies_core_service_as_high_risk(self):
        self.assertTrue(
            check_upstream_updates.is_high_risk_path("app/services/task.py")
        )

    def test_classifies_controller_tree_as_high_risk(self):
        self.assertTrue(
            check_upstream_updates.is_high_risk_path("app/controllers/v1/video.py")
        )

    def test_classifies_webui_tree_as_high_risk(self):
        self.assertTrue(check_upstream_updates.is_high_risk_path("webui/Main.py"))

    def test_classifies_docs_as_normal_risk(self):
        self.assertFalse(
            check_upstream_updates.is_high_risk_path(
                "docs/kurukin-render-console-plan.md"
            )
        )

    def test_classify_changed_files_skips_blank_lines(self):
        files = check_upstream_updates.classify_changed_files(
            [
                "app/services/video.py",
                "",
                "README.md",
                " webui/Main.py ",
            ]
        )

        self.assertEqual(
            [(item.path, item.risk) for item in files],
            [
                ("app/services/video.py", "HIGH RISK"),
                ("README.md", "normal"),
                ("webui/Main.py", "HIGH RISK"),
            ],
        )

    def test_parse_lines_returns_non_empty_stripped_lines(self):
        self.assertEqual(
            check_upstream_updates.parse_lines(" a\n\n b \n"),
            ["a", "b"],
        )


if __name__ == "__main__":
    unittest.main()
