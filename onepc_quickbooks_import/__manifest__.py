# Copyright 2026 OnePC Technologies LLC
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).
{
    "name": "QuickBooks Import (CSV)",
    "summary": "Import customers, vendors, products and the chart of accounts "
    "from QuickBooks Online / Desktop CSV exports, with a dry-run preview.",
    "version": "1.0.0",
    "category": "Accounting/Accounting",
    "author": "OnePC Technologies LLC",
    "website": "https://www.onepctechnologies.com/quickbooks-to-odoo/",
    "license": "LGPL-3",
    "depends": ["account", "product"],
    "data": [
        "security/ir.model.access.csv",
        "wizard/qb_import_wizard_views.xml",
        "views/qb_import_log_views.xml",
        "views/menus.xml",
        "data/list_views.xml",
    ],
    "installable": True,
    "application": False,
}
