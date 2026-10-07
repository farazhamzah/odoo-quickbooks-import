# Copyright 2026 OnePC Technologies LLC
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).
import base64

from odoo.exceptions import UserError
from odoo.tests import TransactionCase, tagged

from .test_qb_csv import read_sample


@tagged("post_install", "-at_install")
class TestQuickBooksImport(TransactionCase):
    """End-to-end wizard tests: dry run, import, matching, idempotency."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls.env.company
        cls.Partner = cls.env["res.partner"]
        cls.Template = cls.env["product.template"]
        cls.Account = cls.env["account.account"].with_company(cls.company)
        cls.company_domain = cls.Account._check_company_domain(cls.company)

    def _wizard(self, import_type, sample):
        return self.env["onepc.qb.import.wizard"].create(
            {
                "import_type": import_type,
                "data_file": base64.b64encode(read_sample(sample)),
                "filename": sample,
                "company_id": self.company.id,
            }
        )

    def _run(self, import_type, sample):
        wizard = self._wizard(import_type, sample)
        wizard.action_import()
        return wizard

    def _accounts(self, name_prefix="Test "):
        return self.Account.search(
            self.company_domain + [("name", "=like", name_prefix + "%")]
        )

    # ------------------------------------------------------------------
    # Dry run
    # ------------------------------------------------------------------

    def test_dry_run_writes_nothing(self):
        partners_before = self.Partner.search_count([])
        logs_before = self.env["onepc.qb.import.log"].search_count([])
        wizard = self._wizard("customer", "qbo_customers.csv")
        wizard.action_preview()
        self.assertEqual(wizard.state, "preview")
        self.assertEqual(
            (wizard.create_count, wizard.skip_count, wizard.error_count), (4, 1, 1)
        )
        self.assertIn("Test Customer Bravo", wizard.preview_html)
        self.assertIn("Duplicate of CSV line 2", wizard.preview_html)
        self.assertIn("Missing name", wizard.preview_html)
        self.assertEqual(self.Partner.search_count([]), partners_before)
        self.assertEqual(self.env["onepc.qb.import.log"].search_count([]), logs_before)

        wizard.action_back()
        self.assertEqual(wizard.state, "upload")
        self.assertFalse(wizard.preview_html)

    def test_dry_run_other_types_write_nothing(self):
        for import_type, sample in (
            ("product", "qbo_products.csv"),
            ("account", "qbo_accounts.csv"),
        ):
            templates_before = self.Template.search_count([])
            accounts_before = self.Account.search_count(self.company_domain)
            wizard = self._wizard(import_type, sample)
            wizard.action_preview()
            self.assertEqual(self.Template.search_count([]), templates_before)
            self.assertEqual(self.Account.search_count(self.company_domain), accounts_before)

    def test_bad_file(self):
        wizard = self.env["onepc.qb.import.wizard"].create(
            {
                "import_type": "customer",
                "data_file": base64.b64encode(b"foo,bar\n1,2\n"),
                "filename": "bad.csv",
            }
        )
        with self.assertRaises(UserError):
            wizard.action_preview()
        empty = self.env["onepc.qb.import.wizard"].create({"import_type": "customer"})
        with self.assertRaises(UserError):
            empty.action_preview()

    # ------------------------------------------------------------------
    # Partners
    # ------------------------------------------------------------------

    def test_customer_import_and_idempotency(self):
        wizard = self._run("customer", "qbo_customers.csv")
        self.assertEqual(wizard.state, "done")
        self.assertEqual(
            (wizard.create_count, wizard.skip_count, wizard.error_count), (4, 1, 1)
        )

        alpha = self.Partner.search([("email", "=", "alpha.test@example.com")])
        self.assertEqual(len(alpha), 1)
        self.assertEqual(alpha.name, "Test Customer Alpha LLC")
        self.assertTrue(alpha.is_company)
        self.assertEqual(alpha.customer_rank, 1)
        self.assertEqual(alpha.city, "Springfield")
        self.assertEqual(alpha.country_id.code, "US")
        self.assertEqual(alpha.state_id.code, "CA")
        self.assertIn("Fictional test customer", alpha.comment)

        bravo = self.Partner.search([("name", "=", "Test Customer Bravo")])
        self.assertEqual(bravo.email, "bravo.test@example.com")
        self.assertEqual(bravo.state_id.code, "NY")
        charlie = self.Partner.search([("name", "=", "Test Customer Charlie, Inc.")])
        self.assertEqual(charlie.email, "charlie.test@example.com")
        delta = self.Partner.search([("name", "=", "Test Customer Delta Co")])
        self.assertTrue(delta.is_company)

        # The log records every row and points at the created records.
        log = wizard.log_id
        self.assertEqual(log.created_count, 4)
        self.assertEqual(len(log.line_ids), 6)
        created = log.line_ids.filtered(lambda l: l.status == "create")
        self.assertEqual(set(created.mapped("res_model")), {"res.partner"})
        self.assertIn(alpha.id, created.mapped("res_id"))
        action = log.action_view_created_records()
        self.assertEqual(self.Partner.search(action["domain"]), alpha | bravo | charlie | delta)

        # Second run: everything already exists, nothing is duplicated.
        count = self.Partner.search_count([])
        again = self._run("customer", "qbo_customers.csv")
        self.assertEqual(
            (again.create_count, again.skip_count, again.error_count), (0, 5, 1)
        )
        self.assertEqual(self.Partner.search_count([]), count)

    def test_contact_list_report(self):
        wizard = self._run("customer", "qbo_customer_contact_list.csv")
        self.assertEqual((wizard.create_count, wizard.error_count), (2, 0))
        echo = self.Partner.search([("email", "=", "echo.test@example.com")])
        self.assertEqual(echo.name, "Test Contact Echo")
        self.assertEqual(echo.street, "400 Mock Lane Springfield, CA 90002")

    def test_partner_matching_by_email_and_name(self):
        existing = self.Partner.create(
            {"name": "Existing Test Partner", "email": "ALPHA.TEST@example.com"}
        )
        by_name = self.Partner.create({"name": "test vendor golf parts"})
        self.assertFalse(existing.supplier_rank)
        wizard = self._run("vendor", "qbd_vendors.csv")
        # Café is new; Golf matches by name (case-insensitive); the alias row
        # matches the existing partner by email.
        self.assertEqual(
            (wizard.create_count, wizard.skip_count, wizard.error_count), (1, 2, 0)
        )
        self.assertEqual(existing.supplier_rank, 1)
        self.assertEqual(by_name.supplier_rank, 1)
        self.assertEqual(existing.name, "Existing Test Partner")
        cafe = self.Partner.search([("email", "=", "cafe.vendor@example.com")])
        self.assertEqual(cafe.name, "Test Vendor Café Supplies")
        self.assertEqual(cafe.supplier_rank, 1)
        self.assertEqual(cafe.customer_rank, 0)
        self.assertEqual(cafe.street2, "Suite 5")

    def test_vendor_import_after_customer_import(self):
        self._run("customer", "qbo_customers.csv")
        self._run("vendor", "qbd_vendors.csv")
        alpha = self.Partner.search([("email", "=", "alpha.test@example.com")])
        self.assertEqual(len(alpha), 1)
        self.assertEqual((alpha.customer_rank, alpha.supplier_rank), (1, 1))

    def test_like_wildcards_are_literal(self):
        # "_" and "%" in a CSV name must not act as LIKE wildcards.
        self.Partner.create({"name": "TestXWildYCard"})
        wizard = self.env["onepc.qb.import.wizard"].create(
            {
                "import_type": "customer",
                "data_file": base64.b64encode(b"Name\nTestXWildYCard\nTest_Wild%Card\n"),
                "filename": "wild.csv",
            }
        )
        wizard.action_preview()
        self.assertEqual((wizard.create_count, wizard.skip_count), (1, 1))

    # ------------------------------------------------------------------
    # Products
    # ------------------------------------------------------------------

    def test_product_import_and_idempotency(self):
        wizard = self._run("product", "qbo_products.csv")
        self.assertEqual(
            (wizard.create_count, wizard.skip_count, wizard.error_count), (3, 1, 2)
        )
        self.assertIn("Invalid amount", wizard.preview_html)
        self.assertIn("Bundle", wizard.preview_html)

        service = self.Template.search([("default_code", "=", "TST-SVC-01")])
        self.assertEqual(service.type, "service")
        self.assertAlmostEqual(service.list_price, 1234.5)
        self.assertEqual(service.description_sale, "One hour of fictional consulting")

        widget = self.Template.search([("default_code", "=", "TST-INV-01")])
        self.assertEqual(len(widget), 1)
        self.assertAlmostEqual(widget.list_price, 25.0)
        self.assertAlmostEqual(widget.standard_price, 10.5)
        self.assertEqual(widget.description_purchase, "Widget from test vendor")
        # A QuickBooks "Inventory" item becomes a storable product when the
        # Inventory app is installed, and plain goods otherwise.
        if "is_storable" in widget._fields:  # Odoo 18 with Inventory
            self.assertEqual(widget.type, "consu")
            self.assertTrue(widget.is_storable)
        elif "detailed_type" in widget._fields:  # Odoo 17
            selection = self.Template.fields_get(["detailed_type"])["detailed_type"]["selection"]
            expected = "product" if "product" in dict(selection) else "consu"
            self.assertEqual(widget.detailed_type, expected)
        else:  # Odoo 18 without Inventory
            self.assertEqual(widget.type, "consu")

        sticker = self.Template.search([("default_code", "=", "TST-NI-01")])
        self.assertAlmostEqual(sticker.list_price, 3.99)
        self.assertFalse(self.Template.search([("default_code", "in", ("TST-BND-01", "TST-INV-02"))]))

        count = self.Template.search_count([])
        again = self._run("product", "qbo_products.csv")
        self.assertEqual(again.create_count, 0)
        self.assertEqual(self.Template.search_count([]), count)

    def test_product_desktop_items(self):
        wizard = self._run("product", "qbd_items.csv")
        self.assertEqual(
            (wizard.create_count, wizard.skip_count, wizard.error_count), (3, 2, 0)
        )
        cable = self.Template.search([("name", "=", "Test Cable 10m")])
        self.assertAlmostEqual(cable.list_price, 1050.0)
        shipping = self.Template.search([("name", "=", "Test Shipping Charge")])
        self.assertEqual(shipping.type, "service")
        self.assertFalse(self.Template.search([("name", "=", "Test Promo Discount")]))

    def test_product_matching_by_sku_then_name(self):
        by_sku = self.Template.create({"name": "Renamed Widget", "default_code": "TST-INV-01"})
        by_name = self.Template.create({"name": "test sticker pack"})
        wizard = self._wizard("product", "qbo_products.csv")
        wizard.action_import()
        self.assertEqual(wizard.create_count, 1)  # only the consulting hour
        self.assertEqual(self.Template.search_count([("default_code", "=", "TST-INV-01")]), 1)
        self.assertEqual(by_sku.name, "Renamed Widget")
        self.assertFalse(by_name.default_code)

    # ------------------------------------------------------------------
    # Chart of accounts
    # ------------------------------------------------------------------

    def test_account_import_without_numbers(self):
        wizard = self._run("account", "qbo_accounts.csv")
        self.assertEqual(
            (wizard.create_count, wizard.skip_count, wizard.error_count), (10, 0, 1)
        )
        self.assertIn("Space Station", wizard.preview_html)
        expected = {
            "Test Checking": "asset_cash",
            "Test Accounts Receivable": "asset_receivable",
            "Test Prepaid Insurance": "asset_prepayments",
            "Test Office Equipment": "asset_fixed",
            "Test Accounts Payable": "liability_payable",
            "Test Company Card": "liability_credit_card",
            "Test Sales": "income",
            "Test Cost of Goods Sold": "expense_direct_cost",
            "Test Depreciation": "expense_depreciation",
            "Test Rent": "expense",
        }
        accounts = {a.name: a for a in self._accounts()}
        self.assertEqual(set(accounts), set(expected))
        for name, account_type in expected.items():
            self.assertEqual(accounts[name].account_type, account_type, name)
        self.assertTrue(accounts["Test Checking"].code.startswith("19"))
        self.assertTrue(accounts["Test Accounts Payable"].code.startswith("29"))
        self.assertTrue(accounts["Test Rent"].code.startswith("69"))
        self.assertTrue(accounts["Test Accounts Receivable"].reconcile)
        self.assertEqual(accounts["Test Checking"].note, "Fictional bank account")
        codes = [a.code for a in accounts.values()]
        self.assertEqual(len(codes), len(set(codes)))

        again = self._run("account", "qbo_accounts.csv")
        self.assertEqual((again.create_count, again.skip_count), (0, 10))
        self.assertEqual(len(self._accounts()), 10)

    def test_account_import_with_numbers(self):
        existing = self.Account.create(
            {"name": "Existing Test Rent", "code": "9601", "account_type": "expense"}
        )
        wizard = self._run("account", "qbd_accounts.csv")
        self.assertEqual(
            (wizard.create_count, wizard.skip_count, wizard.error_count), (4, 2, 0)
        )
        self.assertIn("Non-posting", wizard.preview_html)
        by_code = {a.code: a for a in self._accounts()}
        self.assertEqual(by_code["9101"].account_type, "asset_cash")
        self.assertEqual(by_code["9201"].account_type, "liability_non_current")
        self.assertEqual(by_code["9301"].account_type, "equity")
        self.assertEqual(by_code["9602.01"].name, "Test Sub:Utilities")
        self.assertNotIn("9901", by_code)
        # Matched by code: the existing account is left alone, not duplicated.
        self.assertEqual(existing.name, "Existing Test Rent")
        self.assertEqual(
            self.Account.search_count(self.company_domain + [("code", "=", "9601")]), 1
        )
