# QuickBooks Import for Odoo 17 and 18

A free Odoo module that imports the CSV exports from **QuickBooks Online** (and
**QuickBooks Desktop**, which uses similar files) into **Odoo 17 and Odoo 18**
(tested on Community; it only uses models that Enterprise shares).

It brings over your master data:

| QuickBooks list         | Odoo record          | Fields                                                                                  |
|-------------------------|----------------------|-----------------------------------------------------------------------------------------|
| Customers               | Contact (`res.partner`), marked as customer | name, company, email, phone, mobile, billing address (street, city, state, ZIP, country), website, notes |
| Vendors                 | Contact (`res.partner`), marked as vendor   | same as customers                                                                      |
| Products and Services   | Product (`product.template`)                 | name, SKU (Internal Reference), type, sales price, cost, sales and purchase descriptions |
| Chart of Accounts       | Account (`account.account`)                  | account number (code), name, type, description                                          |

Every import starts with a **dry run**: you see exactly which rows will be
created, which will be skipped because the record already exists, and which
have errors. Nothing is written until you confirm.

- Technical name: `onepc_quickbooks_import`
- Depends on: `account`, `product` (both part of Odoo Community)
- No extra Python packages
- License: LGPL-3

## Install

1. Download this repository (green **Code** button > **Download ZIP**, or `git clone`).
2. Copy the `onepc_quickbooks_import` folder into your Odoo addons path, for example
   `/mnt/extra-addons` in the official Docker image.
3. Restart Odoo.
4. Turn on developer mode (Settings > Activate the developer mode), then go to
   **Apps > Update Apps List**.
5. Search for **QuickBooks Import** and click **Install**.

The same code installs on Odoo 17.0 and Odoo 18.0. It has been tested on both,
with and without the Inventory app.

You need the **Invoicing / Accounting Administrator** access right to use it.

## Export your data from QuickBooks

Menu names change between QuickBooks releases, so these steps are a guide.
If QuickBooks gives you an Excel file, open it in Excel, Google Sheets or
LibreOffice and save it as **CSV (UTF-8)**. Column names do not have to match
exactly: the module recognises the usual QuickBooks Online and Desktop headers.

**QuickBooks Online**

- **Customers:** Sales (or Customers & leads) > Customers > the export icon
  above the list > Export to Excel. The *Customer Contact List* report also works.
- **Vendors:** Expenses > Vendors > the export icon above the list > Export to Excel.
- **Products and Services:** Sales > Products & services > the export option
  above the list (or run the *Product/Service List* report and export it).
- **Chart of Accounts:** Accounting > Chart of accounts > Run report >
  Export to Excel. This is the *Account List* report.

**QuickBooks Desktop**

- **Customers / Vendors:** Customer Center (or Vendor Center) > Excel >
  Export Customer List (Vendor List) > choose *Create a comma separated values (.csv) file*.
- **Items:** Lists > Item List > Excel > Export all items > export to a .csv file.
- **Chart of Accounts:** Reports > List > Account Listing > Excel > export to a .csv file.

Sample files with made-up data are in
[`onepc_quickbooks_import/tests/data/`](onepc_quickbooks_import/tests/data/).

## Use it

1. Go to **Invoicing (or Accounting) > Configuration > QuickBooks Import > Import from CSV**.
2. Pick the record type, upload the CSV and click **Preview**.
3. Review the dry run:
   - **Columns recognised**: which CSV column feeds which Odoo field.
   - **Rows**: one line per CSV row with the action (*Create*, *Skip* or *Error*)
     and the reason.
4. Click **Import**. The rows marked *Create* are created; errors are left out.
5. Every import is saved under **QuickBooks Import > Import History**, with the
   outcome of each row and a button to open the records that were created.

Import in this order: chart of accounts, then products, then customers and vendors.

## How matching works (no duplicates)

Before creating anything, the module looks for an existing record. If it finds
one, the row is skipped and the existing record is left as it is. Running the
same file twice therefore creates nothing the second time.

| Record type | Matched by (in this order)                         |
|-------------|----------------------------------------------------|
| Customers / vendors | email (case-insensitive), then exact name (case-insensitive) |
| Products    | SKU (Internal Reference), then exact name          |
| Accounts    | account code, then exact name, within the selected company |

Rows that repeat an earlier row of the same file are skipped too.

One exception to "left as it is": if a vendor import matches a contact that is
only a customer (or the other way round), that contact is also flagged as a
vendor (customer), so it appears in the right lists. No other field is changed.

## What it does with QuickBooks quirks

- UTF-8 files with a byte order mark, and Windows-1252 files from QuickBooks Desktop.
- Report title rows above the header (for example *Account List / Company name / All dates*)
  and the date footer at the bottom of QuickBooks Online reports.
- Different column names between QuickBooks Online and Desktop
  (`Name` / `Customer`, `Email` / `Main Email`, `Account #` / `Accnt. #`, `Sales Price / Rate` / `Price`, and so on).
- Amounts such as `$1,234.50`, `USD 10`, `(25.00)` and `12.00-`.
- Empty rows, and cells with several email addresses (the first one is used).
- Country names such as `USA` or `United States`, and US state codes or names.

### Product types

| QuickBooks type                    | Odoo product                                                     |
|-----------------------------------|------------------------------------------------------------------|
| Service, Other Charge              | Service                                                          |
| Inventory, Inventory Part, Inventory Assembly | Storable product if the Inventory app is installed, otherwise Goods |
| Non-inventory, Non-inventory Part  | Goods (consumable)                                               |
| Bundle, Group                      | Not imported (listed as an error so you can rebuild it)          |
| Discount, Payment, Subtotal, Sales Tax Item / Group | Skipped (these are not products in Odoo)           |

### Account types

| QuickBooks type                         | Odoo account type          |
|-----------------------------------------|----------------------------|
| Bank                                    | Bank and Cash              |
| Accounts receivable (A/R)               | Receivable                 |
| Other Current Assets                    | Current Assets (Prepayments when the detail type is *Prepaid Expenses*) |
| Fixed Assets                            | Fixed Assets               |
| Other Assets                            | Non-current Assets         |
| Accounts payable (A/P)                  | Payable                    |
| Credit Card                             | Credit Card                |
| Other Current Liabilities               | Current Liabilities        |
| Long Term Liabilities                   | Non-current Liabilities    |
| Equity                                  | Equity                     |
| Income                                  | Income                     |
| Other Income                            | Other Income               |
| Cost of Goods Sold                      | Cost of Revenue            |
| Expenses, Other Expense                 | Expenses (Depreciation when the detail type is *Depreciation*) |
| Non-Posting (Desktop)                   | Skipped                    |

QuickBooks Online often has no account numbers. Odoo needs a code, so the
module assigns one: the first digit follows the type (1 assets, 2 liabilities,
3 equity, 4 income, 5 cost of revenue, 6 expenses) followed by `9` and a
counter, for example `190001`. You can change the codes afterwards. Account
numbers that contain characters Odoo does not accept (such as `-`) are
converted to dots: `6000-01` becomes `6000.01`.

## Limitations

This module moves master data only. It does **not** import:

- invoices, bills, payments, journal entries or any transaction history
- opening balances or the *Balance* column of the account list
- payroll, employees or time tracking
- attachments, notes history or documents
- quantities on hand, bundles/groups, sales tax codes, or the income and
  expense accounts linked to products
- shipping addresses or extra contact persons (only the billing address)

Other points to know:

- Existing records are never updated. To change data that is already in Odoo,
  edit it in Odoo or use Odoo's standard import with the *ID* column.
- Sub-accounts and sub-customers keep their full QuickBooks name
  (for example `Utilities:Electric`); no parent/child link is created.
- Amounts must use a dot as the decimal separator (the US format QuickBooks uses).
- Bank accounts are created as general-ledger accounts only. Create the
  matching bank journal in Odoo if you want to reconcile them.

## Run the tests

```bash
odoo -d <db> -i onepc_quickbooks_import --test-enable \
     --test-tags /onepc_quickbooks_import --stop-after-init
```

The tests cover CSV parsing, header variants, amounts, type mapping, matching,
idempotency and the dry run.

## Need a full migration?

Need a full migration (open invoices, bills, reconciliation, cut-over)?
OnePC Technologies does fixed-price QuickBooks to Odoo migrations:
https://www.onepctechnologies.com/quickbooks-to-odoo/ · faraz@onepctechnologies.com

## License

[LGPL-3](LICENSE). Copyright 2026 OnePC Technologies LLC.

Bug reports and pull requests are welcome.
