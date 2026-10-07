# Copyright 2026 OnePC Technologies LLC
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).
"""Read QuickBooks CSV exports.

Everything in this file is plain Python (no ORM access) so it can be
tested on its own. It deals with the quirks of QuickBooks exports:

* UTF-8 byte order mark (and Windows-1252 files from QuickBooks Desktop)
* report title rows above the real header row (QuickBooks Online reports)
* header names that differ between QuickBooks Online and Desktop
* currency strings such as ``$1,234.50`` or ``(25.00)``
* empty rows and report footers (``TOTAL``, the "printed on" timestamp)
"""

import csv
import io
import re

# --------------------------------------------------------------------------
# Header aliases. Keys are our logical field names; values are normalized
# header names (see ``normalize_header``) in priority order. The first alias
# found in the file wins, so put the most specific names first.
# --------------------------------------------------------------------------

PARTNER_COLUMNS = {
    "name": [
        "customer", "vendor", "displayname", "customerdisplayname",
        "vendordisplayname", "customername", "vendorname", "name",
        "fullname",
    ],
    "company": ["companyname", "company"],
    "first_name": ["firstname"],
    "last_name": ["lastname"],
    "email": ["email", "mainemail", "emailaddress", "primaryemail"],
    "phone": ["phone", "mainphone", "phonenumbers", "phonenumber", "workphone"],
    "mobile": ["mobile", "mobilephone", "cellphone"],
    "street": [
        "billingaddressline1", "billingstreet", "billto1", "billfrom1",
        "streetaddress", "street", "address1", "billingaddress", "address",
    ],
    "street2": ["billingaddressline2", "billto2", "billfrom2", "address2", "street2"],
    "city": ["billingaddresscity", "billingcity", "city"],
    "state": [
        "billingaddressstate", "billingstate", "state", "stateprovince",
        "province", "region",
    ],
    "zip": [
        "billingaddresspostalcode", "billingpostalcode", "billingzip", "zip",
        "zipcode", "postalcode", "postcode",
    ],
    "country": ["billingaddresscountry", "billingcountry", "country"],
    "website": ["website", "websiteurl", "web"],
    "notes": ["notes", "note"],
}

PRODUCT_COLUMNS = {
    "name": [
        "productservicename", "productservice", "productname", "itemname",
        "item", "name", "product",
    ],
    "sku": ["sku", "partnumber", "itemcode"],
    "type": ["type", "itemtype", "producttype"],
    "price": [
        "salespricerate", "salesprice", "price", "rate", "unitprice",
        "salesrate",
    ],
    "cost": ["purchasecost", "cost", "costprice", "unitcost"],
    "description": ["salesdescription", "description", "salesdesc"],
    "purchase_description": ["purchasedescription", "purchasedesc"],
}

ACCOUNT_COLUMNS = {
    "name": ["fullname", "accountname", "account", "name"],
    "code": [
        "accountnumber", "accntnumber", "acctnumber", "accountno", "number",
        "code",
    ],
    "type": ["accounttype", "type"],
    "detail_type": ["detailtype"],
    "description": ["description"],
}

COLUMNS_BY_KIND = {
    "partner": PARTNER_COLUMNS,
    "product": PRODUCT_COLUMNS,
    "account": ACCOUNT_COLUMNS,
}

# --------------------------------------------------------------------------
# QuickBooks account type -> Odoo account_type (identical in Odoo 17 and 18)
# Keys are normalized with ``normalize_key``.
# --------------------------------------------------------------------------

SKIP_ACCOUNT = "__skip__"

ACCOUNT_TYPE_MAP = {
    "bank": "asset_cash",
    "cash": "asset_cash",
    "accountsreceivable": "asset_receivable",
    "accountreceivable": "asset_receivable",
    "ar": "asset_receivable",
    "othercurrentasset": "asset_current",
    "othercurrentassets": "asset_current",
    "currentasset": "asset_current",
    "currentassets": "asset_current",
    "fixedasset": "asset_fixed",
    "fixedassets": "asset_fixed",
    "propertyplantandequipment": "asset_fixed",
    "otherasset": "asset_non_current",
    "otherassets": "asset_non_current",
    "noncurrentasset": "asset_non_current",
    "noncurrentassets": "asset_non_current",
    "accountspayable": "liability_payable",
    "accountpayable": "liability_payable",
    "ap": "liability_payable",
    "creditcard": "liability_credit_card",
    "creditcards": "liability_credit_card",
    "othercurrentliability": "liability_current",
    "othercurrentliabilities": "liability_current",
    "currentliability": "liability_current",
    "currentliabilities": "liability_current",
    "longtermliability": "liability_non_current",
    "longtermliabilities": "liability_non_current",
    "noncurrentliability": "liability_non_current",
    "noncurrentliabilities": "liability_non_current",
    "equity": "equity",
    "income": "income",
    "otherincome": "income_other",
    "costofgoodssold": "expense_direct_cost",
    "cogs": "expense_direct_cost",
    "expense": "expense",
    "expenses": "expense",
    "otherexpense": "expense",
    "otherexpenses": "expense",
    # QuickBooks Desktop "Non-Posting" accounts hold estimates, sales orders
    # and purchase orders. They are not general-ledger accounts in Odoo.
    "nonposting": SKIP_ACCOUNT,
}

# A few QuickBooks Online "Detail type" values map to a more precise
# Odoo type than the main QuickBooks type does.
ACCOUNT_DETAIL_TYPE_MAP = {
    "prepaidexpenses": "asset_prepayments",
    "depreciation": "expense_depreciation",
    "amortization": "expense_depreciation",
}

ODOO_ACCOUNT_TYPES = {
    "asset_receivable", "asset_cash", "asset_current", "asset_non_current",
    "asset_prepayments", "asset_fixed", "liability_payable",
    "liability_credit_card", "liability_current", "liability_non_current",
    "equity", "equity_unaffected", "income", "income_other", "expense",
    "expense_depreciation", "expense_direct_cost", "off_balance",
}

# First digit used when we have to generate an account code.
ACCOUNT_CODE_PREFIX = {
    "asset": "1",
    "liability": "2",
    "equity": "3",
    "income": "4",
    "expense_direct_cost": "5",
    "expense": "6",
    "off_balance": "9",
}

# --------------------------------------------------------------------------
# QuickBooks item type -> our generic product kind
# ("goods", "storable", "service" or SKIP_PRODUCT / UNSUPPORTED_PRODUCT)
# --------------------------------------------------------------------------

SKIP_PRODUCT = "__skip__"
UNSUPPORTED_PRODUCT = "__unsupported__"

PRODUCT_TYPE_MAP = {
    "service": "service",
    "services": "service",
    "othercharge": "service",
    "inventory": "storable",
    "inventorypart": "storable",
    "inventoryitem": "storable",
    "inventoryassembly": "storable",
    "noninventory": "goods",
    "noninventorypart": "goods",
    "noninventoryitem": "goods",
    "noninventoryproduct": "goods",
    "bundle": UNSUPPORTED_PRODUCT,
    "group": UNSUPPORTED_PRODUCT,
    "discount": SKIP_PRODUCT,
    "payment": SKIP_PRODUCT,
    "subtotal": SKIP_PRODUCT,
    "salestax": SKIP_PRODUCT,
    "salestaxitem": SKIP_PRODUCT,
    "salestaxgroup": SKIP_PRODUCT,
}

_FOOTER_RE = re.compile(
    r"^((grand )?total|(monday|tuesday|wednesday|thursday|friday|saturday|sunday),.*)$",
    re.IGNORECASE,
)
_EMAIL_SPLIT_RE = re.compile(r"[,;\s]+")
_AMOUNT_RE = re.compile(r"^\d+(\.\d+)?$")


class QuickBooksCSVError(ValueError):
    """Raised when a file cannot be read as a QuickBooks CSV export."""


def normalize_header(value):
    """Lower-case a header and keep only letters and digits.

    ``#`` becomes ``number`` so that ``Account #`` and ``Accnt. #`` stay
    distinguishable from ``Account``.
    """
    value = (value or "").replace("﻿", "").replace("#", "number").lower()
    return re.sub(r"[^a-z0-9]", "", value)


def normalize_key(value):
    """Normalize a value (such as an account type) for dictionary lookups.

    Text in parentheses is dropped, so ``Accounts receivable (A/R)``
    becomes ``accountsreceivable``.
    """
    value = re.sub(r"\(.*?\)", "", value or "")
    return re.sub(r"[^a-z0-9]", "", value.lower())


def decode_bytes(data):
    """Decode file content, trying UTF-8 (with or without BOM) first."""
    if isinstance(data, str):
        return data.lstrip("﻿")
    for encoding in ("utf-8-sig", "cp1252"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("latin-1")


def _sniff_dialect(text):
    sample = text[:8192]
    try:
        return csv.Sniffer().sniff(sample, delimiters=",;\t")
    except csv.Error:
        return csv.excel


def resolve_columns(header_row, aliases):
    """Map logical field names to column indexes for a header row."""
    normalized = [normalize_header(h) for h in header_row]
    columns = {}
    for field, names in aliases.items():
        for name in names:
            if name in normalized:
                columns[field] = normalized.index(name)
                break
    return columns


def read_rows(data, kind, max_header_scan=15):
    """Parse a QuickBooks CSV export.

    :param data: file content (bytes or str)
    :param kind: ``"partner"``, ``"product"`` or ``"account"``
    :return: ``(columns, rows)`` where ``columns`` maps logical field names
        to header labels and ``rows`` is a list of ``(line_number, dict)``
        with stripped string values. Empty rows and report footers are left
        out.
    :raise QuickBooksCSVError: when no usable header row is found.
    """
    aliases = COLUMNS_BY_KIND[kind]
    text = decode_bytes(data)
    if not text.strip():
        raise QuickBooksCSVError("The file is empty.")
    reader = csv.reader(io.StringIO(text, newline=""), _sniff_dialect(text))
    try:
        all_rows = list(reader)
    except csv.Error as exc:
        raise QuickBooksCSVError("The file is not a valid CSV file: %s" % exc) from exc

    header_index = None
    columns = {}
    for index, row in enumerate(all_rows[:max_header_scan]):
        candidate = resolve_columns(row, aliases)
        if "name" in candidate:
            header_index, columns = index, candidate
            break
    if header_index is None:
        expected = ", ".join(sorted({a for a in aliases["name"]}))
        raise QuickBooksCSVError(
            "Could not find the header row. The file needs a name column "
            "(normalized header one of: %s)." % expected
        )

    header = all_rows[header_index]
    labels = {field: header[i].replace("﻿", "").strip() for field, i in columns.items()}
    rows = []
    for offset, raw in enumerate(all_rows[header_index + 1:], start=header_index + 2):
        cells = [(c or "").strip() for c in raw]
        if not any(cells):
            continue
        first = next((c for c in cells if c), "")
        if _FOOTER_RE.match(first) and sum(1 for c in cells if c) <= 2:
            continue
        values = {
            field: cells[i] if i < len(cells) else ""
            for field, i in columns.items()
        }
        rows.append((offset, values))
    return labels, rows


def parse_amount(value):
    """Turn a QuickBooks amount into a float.

    Handles ``$1,234.50``, ``USD 1,234.50``, ``-12``, ``(12.00)`` and
    ``12.00-``. Returns ``None`` for an empty value.

    :raise ValueError: if the value is not an amount.
    """
    text = (value or "").strip()
    if not text:
        return None
    negative = (
        (text.startswith("(") and text.endswith(")"))
        or text.startswith("-")
        or text.endswith("-")
    )
    cleaned = re.sub(r"[^\d.,]", "", text).replace(",", "")
    if not cleaned or not _AMOUNT_RE.match(cleaned):
        raise ValueError("%r is not an amount" % value)
    amount = float(cleaned)
    return -amount if negative else amount


def first_email(value):
    """Return the first address from a QuickBooks email cell, lower-cased.

    QuickBooks allows several addresses in one cell, separated by commas.
    """
    for part in _EMAIL_SPLIT_RE.split(value or ""):
        if "@" in part:
            return part.strip().lower()
    return ""


def map_account_type(qb_type, detail_type=""):
    """Map a QuickBooks account type to an Odoo ``account_type``.

    Returns ``SKIP_ACCOUNT`` for non-posting accounts and ``None`` for an
    unknown type. An Odoo technical value (``asset_cash``) is accepted too.
    """
    raw = (qb_type or "").strip()
    if raw in ODOO_ACCOUNT_TYPES:
        return raw
    mapped = ACCOUNT_TYPE_MAP.get(normalize_key(raw))
    if mapped and mapped != SKIP_ACCOUNT:
        detail = ACCOUNT_DETAIL_TYPE_MAP.get(normalize_key(detail_type))
        if detail == "asset_prepayments" and mapped.startswith("asset"):
            return detail
        if detail == "expense_depreciation" and mapped == "expense":
            return detail
    return mapped


def map_product_type(qb_type):
    """Map a QuickBooks item type to ``goods``, ``storable`` or ``service``.

    Returns ``SKIP_PRODUCT`` for items that are not products in Odoo
    (discounts, payments, sales tax items), ``UNSUPPORTED_PRODUCT`` for
    bundles/groups and ``None`` for an unknown type. An empty type is
    treated as goods.
    """
    key = normalize_key(qb_type)
    if not key:
        return "goods"
    return PRODUCT_TYPE_MAP.get(key)


def account_code_prefix(account_type):
    """First digit for a generated account code of the given Odoo type."""
    if account_type in ACCOUNT_CODE_PREFIX:
        return ACCOUNT_CODE_PREFIX[account_type]
    return ACCOUNT_CODE_PREFIX.get(account_type.split("_")[0], "9")


def sanitize_account_code(code):
    """Make a QuickBooks account number acceptable to Odoo.

    Odoo allows letters, digits and dots. Spaces are removed and other
    separators (``-``, ``/``, ``_``) become dots. Returns ``""`` when
    nothing usable is left.
    """
    code = re.sub(r"\s+", "", code or "")
    code = re.sub(r"[-/_:]", ".", code)
    return code if re.match(r"^[A-Za-z0-9.]+$", code) else ""
