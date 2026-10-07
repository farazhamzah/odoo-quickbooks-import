# Copyright 2026 OnePC Technologies LLC
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).
from odoo.tests import TransactionCase, tagged
from odoo.tools import file_open

from ..lib import qb_csv


def read_sample(name):
    with file_open("onepc_quickbooks_import/tests/data/%s" % name, "rb") as f:
        return f.read()


@tagged("post_install", "-at_install")
class TestQuickBooksCSV(TransactionCase):
    """Parsing and mapping helpers (no database writes)."""

    def test_bom_and_online_headers(self):
        data = read_sample("qbo_customers.csv")
        self.assertTrue(data.startswith(b"\xef\xbb\xbf"))
        labels, rows = qb_csv.read_rows(data, "partner")
        # The BOM must not leak into the first header.
        self.assertEqual(labels["name"], "Name")
        self.assertEqual(labels["company"], "Company name")
        self.assertEqual(labels["street"], "Street Address")
        # 7 data lines, one of them completely empty.
        self.assertEqual(len(rows), 6)
        line, first = rows[0]
        self.assertEqual(line, 2)
        self.assertEqual(first["name"], "Test Customer Alpha LLC")
        self.assertEqual(first["zip"], "90001")

    def test_report_title_and_footer_rows(self):
        labels, rows = qb_csv.read_rows(
            read_sample("qbo_customer_contact_list.csv"), "partner"
        )
        self.assertEqual(labels["name"], "Customer")
        self.assertEqual(labels["phone"], "Phone Numbers")
        self.assertEqual(labels["street"], "Billing Address")
        names = [values["name"] for _line, values in rows]
        self.assertEqual(names, ["Test Contact Echo", "Test Contact Foxtrot"])
        self.assertEqual(rows[0][0], 6)

    def test_desktop_headers_and_cp1252(self):
        labels, rows = qb_csv.read_rows(read_sample("qbd_vendors.csv"), "partner")
        self.assertEqual(labels["name"], "Vendor")
        self.assertEqual(labels["email"], "Main Email")
        self.assertEqual(labels["street"], "Bill from 1")
        self.assertEqual(rows[0][1]["name"], "Test Vendor Café Supplies")

        labels, _rows = qb_csv.read_rows(read_sample("qbd_accounts.csv"), "account")
        self.assertEqual(labels["name"], "Account")
        self.assertEqual(labels["code"], "Accnt. #")

        labels, _rows = qb_csv.read_rows(read_sample("qbo_accounts.csv"), "account")
        self.assertEqual(labels["name"], "Full name")
        self.assertEqual(labels["detail_type"], "Detail type")
        self.assertNotIn("code", labels)

    def test_product_headers(self):
        labels, rows = qb_csv.read_rows(read_sample("qbo_products.csv"), "product")
        self.assertEqual(labels["name"], "Product/Service Name")
        self.assertEqual(labels["price"], "Sales Price / Rate")
        self.assertEqual(labels["cost"], "Purchase Cost")
        self.assertEqual(labels["description"], "Sales Description")
        self.assertEqual(len(rows), 6)

        labels, _rows = qb_csv.read_rows(read_sample("qbd_items.csv"), "product")
        self.assertEqual(labels["name"], "Item")
        self.assertEqual(labels["price"], "Price")

    def test_missing_header(self):
        with self.assertRaises(qb_csv.QuickBooksCSVError):
            qb_csv.read_rows(b"foo,bar\n1,2\n", "partner")
        with self.assertRaises(qb_csv.QuickBooksCSVError):
            qb_csv.read_rows(b"", "product")

    def test_semicolon_delimiter(self):
        _labels, rows = qb_csv.read_rows(
            "Name;Email\nTest Semi;semi@example.com\n".encode(), "partner"
        )
        self.assertEqual(rows[0][1]["email"], "semi@example.com")

    def test_parse_amount(self):
        cases = {
            "$1,234.50": 1234.5,
            "  $3.99 ": 3.99,
            "USD 10": 10.0,
            "1050": 1050.0,
            "(25.00)": -25.0,
            "-$5.25": -5.25,
            "12.00-": -12.0,
            "$0.00": 0.0,
        }
        for value, expected in cases.items():
            self.assertAlmostEqual(qb_csv.parse_amount(value), expected, msg=value)
        self.assertIsNone(qb_csv.parse_amount(""))
        self.assertIsNone(qb_csv.parse_amount(None))
        for bad in ("twelve dollars", "1.2.3", "$"):
            with self.assertRaises(ValueError, msg=bad):
                qb_csv.parse_amount(bad)

    def test_first_email(self):
        self.assertEqual(
            qb_csv.first_email("Charlie@Example.com, billing@example.com"),
            "charlie@example.com",
        )
        self.assertEqual(qb_csv.first_email(""), "")
        self.assertEqual(qb_csv.first_email("n/a"), "")

    def test_map_account_type(self):
        cases = {
            ("Bank", ""): "asset_cash",
            ("Accounts receivable (A/R)", ""): "asset_receivable",
            ("Accounts Receivable", ""): "asset_receivable",
            ("Other Current Assets", "Prepaid Expenses"): "asset_prepayments",
            ("Other Current Asset", ""): "asset_current",
            ("Fixed Assets", ""): "asset_fixed",
            ("Other Asset", ""): "asset_non_current",
            ("Accounts payable (A/P)", ""): "liability_payable",
            ("Credit Card", ""): "liability_credit_card",
            ("Other Current Liabilities", ""): "liability_current",
            ("Long Term Liability", ""): "liability_non_current",
            ("Equity", ""): "equity",
            ("Income", ""): "income",
            ("Other Income", ""): "income_other",
            ("Cost of Goods Sold", ""): "expense_direct_cost",
            ("Expenses", ""): "expense",
            ("Other Expense", "Depreciation"): "expense_depreciation",
            ("asset_cash", ""): "asset_cash",
        }
        for (qb_type, detail), expected in cases.items():
            self.assertEqual(qb_csv.map_account_type(qb_type, detail), expected, qb_type)
        self.assertEqual(qb_csv.map_account_type("Non-Posting"), qb_csv.SKIP_ACCOUNT)
        self.assertIsNone(qb_csv.map_account_type("Space Station"))

    def test_map_product_type(self):
        self.assertEqual(qb_csv.map_product_type("Service"), "service")
        self.assertEqual(qb_csv.map_product_type("Other Charge"), "service")
        self.assertEqual(qb_csv.map_product_type("Inventory"), "storable")
        self.assertEqual(qb_csv.map_product_type("Inventory Part"), "storable")
        self.assertEqual(qb_csv.map_product_type("Non-inventory"), "goods")
        self.assertEqual(qb_csv.map_product_type("Non-inventory Part"), "goods")
        self.assertEqual(qb_csv.map_product_type(""), "goods")
        self.assertEqual(qb_csv.map_product_type("Discount"), qb_csv.SKIP_PRODUCT)
        self.assertEqual(qb_csv.map_product_type("Bundle"), qb_csv.UNSUPPORTED_PRODUCT)
        self.assertIsNone(qb_csv.map_product_type("Spaceship"))

    def test_account_code_helpers(self):
        self.assertEqual(qb_csv.sanitize_account_code("9602-01"), "9602.01")
        self.assertEqual(qb_csv.sanitize_account_code(" 1000 "), "1000")
        self.assertEqual(qb_csv.sanitize_account_code("10#00"), "")
        self.assertEqual(qb_csv.account_code_prefix("asset_cash"), "1")
        self.assertEqual(qb_csv.account_code_prefix("liability_payable"), "2")
        self.assertEqual(qb_csv.account_code_prefix("expense_direct_cost"), "5")
        self.assertEqual(qb_csv.account_code_prefix("expense_depreciation"), "6")
        self.assertEqual(qb_csv.account_code_prefix("income_other"), "4")
