import pathlib
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]


class WorkflowConfigurationTests(unittest.TestCase):
    def test_update_workflow_deploys_the_exact_published_commit(self):
        workflow = (ROOT / ".github/workflows/update.yml").read_text(encoding="utf-8")

        self.assertIn("feed_changed: ${{ steps.publish.outputs.feed_changed }}", workflow)
        self.assertIn("commit_sha: ${{ steps.publish.outputs.commit_sha }}", workflow)
        self.assertIn("needs: update", workflow)
        self.assertIn("needs.update.outputs.feed_changed == 'true'", workflow)
        self.assertIn("ref: ${{ needs.update.outputs.commit_sha }}", workflow)
        self.assertIn("uses: actions/deploy-pages@v4", workflow)

    def test_pages_artifact_has_an_explicit_source_directory(self):
        for relative_path in (
            ".github/workflows/update.yml",
            ".github/workflows/pages.yml",
        ):
            with self.subTest(workflow=relative_path):
                workflow = (ROOT / relative_path).read_text(encoding="utf-8")
                self.assertIn("uses: actions/upload-pages-artifact@v3", workflow)
                self.assertIn("path: _site", workflow)


if __name__ == "__main__":
    unittest.main()
