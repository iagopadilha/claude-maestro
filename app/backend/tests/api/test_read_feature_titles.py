"""Readable feature names: the `title="..."` of the ROADMAP line is `features[].name` in `GET state`, `GET pipeline` and `GET turn/<n>`;
without it the name falls back to the first heading of the spec, then to the slug."""
import os

from .support_read import ReadTestCase

TITLED_ROADMAP = """# Roadmap

## Stage 1 - Customers

- [customer-registration-api] status=pending title="Register a customer with a tax id" scope=Small kind=back paths=src/customers
- [customer-signup-screen] status=pending title="Sign up from the web" kind=front paths=src/web deps=customer-registration-api
- [invoice-pdf-download] status=pending scope=Small kind=back paths=src/invoices
- [legacy-slug-only] status=pending
"""


class FeatureTitleTests(ReadTestCase):
    INITIAL_ROADMAP = TITLED_ROADMAP

    def names(self, features):
        return {feature["id"]: feature["name"] for feature in features}

    def test_state_uses_the_roadmap_title(self):
        self.fixture.start()
        names = self.names(self.state()["features"])
        self.assertEqual(names["customer-registration-api"], "Register a customer with a tax id")
        self.assertEqual(names["customer-signup-screen"], "Sign up from the web")

    def test_without_title_the_spec_heading_then_the_slug_are_used(self):
        self.fixture.write(os.path.join(self.fixture.work, ".specs", "features", "invoice-pdf-download", "spec.md"),
                           "# Spec: invoice-pdf-download - Download an invoice as PDF\n")
        self.fixture.start()
        names = self.names(self.state()["features"])
        self.assertEqual(names["invoice-pdf-download"], "Download an invoice as PDF")
        self.assertEqual(names["legacy-slug-only"], "legacy-slug-only")

    def test_the_roadmap_title_wins_over_the_spec_heading(self):
        self.fixture.write(os.path.join(self.fixture.work, ".specs", "features", "customer-signup-screen", "spec.md"), "# Something else\n")
        self.fixture.start()
        self.assertEqual(self.names(self.state()["features"])["customer-signup-screen"], "Sign up from the web")

    def test_pipeline_and_turn_snapshot_carry_the_same_names(self):
        self.fixture.write_json("turns/001.json", {"total_cost_usd": 0.1})
        self.fixture.snapshot(1, roadmap=TITLED_ROADMAP)
        self.fixture.start()
        status, pipeline = self.fixture.call("/api/pipeline")
        self.assertEqual(status, 200, pipeline)
        self.assertEqual(self.names(pipeline["features"])["customer-registration-api"], "Register a customer with a tax id")
        status, turn = self.fixture.call("/api/turn/1")
        self.assertEqual(status, 200, turn)
        self.assertEqual(self.names(turn["features"])["customer-signup-screen"], "Sign up from the web")

    def test_the_title_is_not_taken_as_the_description_of_the_roadmap_summary(self):
        self.fixture.start()
        summary = {feature["id"]: feature["summary"] for feature in self.state()["features"]}["customer-registration-api"]
        self.assertNotIn("title", str(summary.get("goal") or ""))
