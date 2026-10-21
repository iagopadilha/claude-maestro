"""Contract merge of engine/system_map.py: contract.md deltas upserted into the CONTRACT section of SYSTEM.md, conflicts,
remove, malformed lines, byte-identical outside the markers (CRLF too), missing SYSTEM.md, pending and the CLI. Temp folders only."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import engine_env  # noqa: E402,F401
import system_map  # noqa: E402

SYSTEM_SCRIPT = os.path.join(engine_env.ENGINE_DIR, "system_map.py")
HEAD = "# System\nintro line\n\n<!-- CONTRACT:BEGIN -->\n"
TAIL = "<!-- CONTRACT:END -->\n\n## Run\npointers\n"


def write_bytes(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as handle:
        handle.write(text.encode("utf-8"))


def read_text(path):
    with open(path, "rb") as handle:
        return handle.read().decode("utf-8")


class TestSystemMap(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="sysmap-")
        self.addCleanup(shutil.rmtree, self.root, True)
        self.system_path = os.path.join(self.root, system_map.SYSTEM_RELATIVE_PATH)

    def system(self, head=HEAD, tail=TAIL, body="Base URL env: API_URL\n"):
        write_bytes(self.system_path, head + body + tail)

    def contract(self, slug, text):
        write_bytes(system_map.contract_path(self.root, slug), text)

    def merged(self):
        return read_text(self.system_path)

    def test_two_routes_merge_grouped_by_area(self):
        self.system()
        self.contract("orders", "- [add] route GET /orders/{id} — get one order (Api/OrdersController.cs)\n"
                                "- [add] route post /users — create user (Api/UsersController.cs)\n"
                                "- [add] dto OrderDto — order payload (Domain/OrderDto.cs)\n"
                                "- [add] env ORDERS_URL — orders base url (appsettings.json)\n"
                                "- [add] auth Bearer — jwt bearer (Api/Program.cs)\n")
        result = system_map.merge_contract_deltas(self.root, ["orders"])
        self.assertTrue(result["changed"])
        self.assertEqual(result["warnings"], [])
        text = self.merged()
        self.assertIn("Base URL env: API_URL", text)
        self.assertIn("#### Routes: orders\n- route GET /orders/{id} — get one order (Api/OrdersController.cs)", text)
        self.assertIn("#### Routes: users\n- route POST /users — create user (Api/UsersController.cs)", text)
        for needle in ("#### DTOs\n- dto OrderDto", "#### Auth\n- auth Bearer", "#### Env\n- env ORDERS_URL"):
            self.assertIn(needle, text)

    def test_conflict_keeps_both_and_warns(self):
        self.system()
        self.contract("first", "- [add] route GET /a — one (x.cs)\n")
        self.contract("second", "- [add] route GET /a — two (y.cs)\n")
        system_map.merge_contract_deltas(self.root, ["first"])
        result = system_map.merge_contract_deltas(self.root, ["second"])
        text = self.merged()
        self.assertIn("- route GET /a — one (x.cs)\n", text)
        self.assertIn("- route GET /a — two (y.cs) (conflict with first)", text)
        self.assertEqual(len(result["warnings"]), 1)
        self.assertIn("GET /a", result["warnings"][0])

    def test_identical_repeat_is_merged_once(self):
        self.system()
        self.contract("first", "- [add] route GET /a — same (x.cs)\n")
        self.contract("second", "- [add] route GET /a — same (x.cs)\n")
        result = system_map.merge_contract_deltas(self.root, ["first", "second"])
        self.assertEqual(self.merged().count("GET /a"), 1)
        self.assertEqual(result["warnings"], [])

    def test_same_feature_rewrite_replaces_and_remove_deletes(self):
        self.system()
        self.contract("orders", "- [add] route GET /a — one (x.cs)\n- [add] route GET /b - keep (x.cs)\n")
        system_map.merge_contract_deltas(self.root, ["orders"])
        self.contract("orders", "- [change] route GET /a - changed (x.cs)\n- [add] route GET /b - keep (x.cs)\n")
        result = system_map.merge_contract_deltas(self.root, ["orders"])
        self.assertEqual(result["warnings"], [])
        self.assertIn("GET /a — changed (x.cs)", self.merged())
        self.assertNotIn("one (x.cs)", self.merged())
        self.contract("drop", "- [remove] route GET /a\n")
        self.assertTrue(system_map.merge_contract_deltas(self.root, ["drop"])["changed"])
        self.assertNotIn("GET /a", self.merged())
        self.assertIn("GET /b", self.merged())

    def test_malformed_lines_listed_and_good_ones_merged(self):
        self.system()
        self.contract("mixed", "# Contract\nnotes are ignored\n- [add] route GET /ok — fine (x.cs)\n"
                               "- [add] route nonsense — bad route (x.cs)\n"
                               "- [add] widget Foo — bad kind (x.cs)\n"
                               "- [add] dto NoFile — missing file\n")
        result = system_map.merge_contract_deltas(self.root, ["mixed"])
        self.assertTrue(result["changed"])
        self.assertEqual(len(result["warnings"]), 3)
        self.assertIn("GET /ok", self.merged())
        self.assertNotIn("NoFile", self.merged())

    def test_outside_markers_byte_identical_with_crlf(self):
        head = "# System\r\n\r\n<!-- CONTRACT:BEGIN -->\r\n"
        tail = "<!-- CONTRACT:END -->\r\n\r\ntrailing é text\r\nno final newline"
        self.system(head=head, tail=tail, body="")
        self.contract("orders", "- [add] route GET /a — one (x.cs)\n")
        system_map.merge_contract_deltas(self.root, ["orders"])
        text = self.merged()
        self.assertTrue(text.startswith(head))
        self.assertTrue(text.endswith(tail))
        inner = text[len(head):-len(tail)]
        self.assertNotIn("\n", inner.replace("\r\n", ""))
        self.assertIn("GET /a", inner)

    def test_contract_with_no_grammar_line_warns_and_is_not_silent(self):
        self.system()
        self.contract("t", "| Action | Route |\n|---|---|\n| add | GET /t |\n")
        result = system_map.merge_contract_deltas(self.root, ["t"])
        self.assertFalse(result["changed"])
        self.assertEqual(result["warnings"], ["contract.md of t has no line in the contract grammar: nothing merged"])

    def test_missing_system_md_skips_with_warning_and_stays_pending(self):
        self.contract("orders", "- [add] route GET /a — one (x.cs)\n")
        result = system_map.merge_contract_deltas(self.root, ["orders"])
        self.assertFalse(result["changed"])
        self.assertEqual(len(result["warnings"]), 1)
        self.assertEqual(system_map.pending_slugs(self.root, ["orders"]), ["orders"])

    def test_pending_lists_only_unmerged_contracts(self):
        self.system()
        self.contract("one", "- [add] route GET /a — one (x.cs)\n")
        self.contract("two", "- [add] route GET /b — two (x.cs)\n")
        system_map.merge_contract_deltas(self.root, ["one"])
        self.assertEqual(system_map.pending_slugs(self.root, ["one", "two", "none"]), ["two"])

    def test_cli_merge_and_pending(self):
        self.system()
        self.contract("one", "- [add] route GET /a — one (x.cs)\n")
        pending = subprocess.run([sys.executable, SYSTEM_SCRIPT, "pending", self.root, "--slugs", "one,none"],
                                 capture_output=True, text=True)
        self.assertEqual(pending.stdout.strip(), "CONTRACT-PENDING: .specs/features/one/contract.md")
        merged = subprocess.run([sys.executable, SYSTEM_SCRIPT, "merge", self.root, "--slugs", "one", "--json"],
                                capture_output=True, text=True)
        self.assertEqual(merged.returncode, 0)
        self.assertEqual(json.loads(merged.stdout), {"changed": True, "warnings": []})
        pending = subprocess.run([sys.executable, SYSTEM_SCRIPT, "pending", self.root, "--slugs", "one"],
                                 capture_output=True, text=True)
        self.assertEqual(pending.stdout, "")


if __name__ == "__main__":
    unittest.main()
