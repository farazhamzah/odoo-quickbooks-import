# Copyright 2026 OnePC Technologies LLC
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).
import base64
import logging

from markupsafe import Markup
from psycopg2 import Error as PsycopgError

from odoo import fields, models
from odoo.exceptions import UserError, ValidationError
from odoo.tools import plaintext2html

from ..lib import qb_csv
from ..models.qb_import_log import IMPORT_TYPES, render_rows_html

_logger = logging.getLogger(__name__)

# Which CSV layout each import type uses.
CSV_KIND = {
    "customer": "partner",
    "vendor": "partner",
    "product": "product",
    "account": "account",
}

TARGET_MODEL = {
    "partner": "res.partner",
    "product": "product.template",
    "account": "account.account",
}

FIELD_LABELS = {
    "name": "Name",
    "company": "Company",
    "first_name": "First name",
    "last_name": "Last name",
    "email": "Email",
    "phone": "Phone",
    "mobile": "Mobile",
    "street": "Street",
    "street2": "Street 2",
    "city": "City",
    "state": "State",
    "zip": "ZIP",
    "country": "Country",
    "website": "Website",
    "notes": "Notes",
    "sku": "Internal reference (SKU)",
    "type": "Type",
    "price": "Sales price",
    "cost": "Cost",
    "description": "Description",
    "purchase_description": "Purchase description",
    "code": "Code",
    "detail_type": "Detail type",
}

US_ALIASES = {"us", "usa", "u.s.", "u.s.a.", "united states", "united states of america"}


def like_escape(value):
    """Escape SQL LIKE wildcards so ``=ilike`` does an exact,
    case-insensitive comparison."""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


class QbImportWizard(models.TransientModel):
    _name = "onepc.qb.import.wizard"
    _description = "QuickBooks CSV Import"

    import_type = fields.Selection(
        IMPORT_TYPES, string="Record type", required=True, default="customer"
    )
    data_file = fields.Binary(string="CSV file", attachment=False)
    filename = fields.Char()
    company_id = fields.Many2one(
        "res.company", required=True, default=lambda self: self.env.company
    )
    state = fields.Selection(
        [("upload", "Upload"), ("preview", "Preview"), ("done", "Done")],
        default="upload",
        required=True,
    )
    create_count = fields.Integer(readonly=True)
    skip_count = fields.Integer(readonly=True)
    error_count = fields.Integer(readonly=True)
    columns_html = fields.Html(readonly=True, sanitize=False)
    preview_html = fields.Html(readonly=True, sanitize=False)
    log_id = fields.Many2one("onepc.qb.import.log", readonly=True)

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    def action_preview(self):
        """Dry run: work out what would happen without writing anything."""
        self.ensure_one()
        labels, plan = self._build_plan()
        self.write(self._summary_vals(labels, plan, "Create") | {"state": "preview"})
        return self._reopen()

    def action_back(self):
        self.ensure_one()
        self.write(
            {
                "state": "upload",
                "preview_html": False,
                "columns_html": False,
                "create_count": 0,
                "skip_count": 0,
                "error_count": 0,
            }
        )
        return self._reopen()

    def action_import(self):
        """Import for real. The plan is rebuilt so that records created
        since the preview are still matched and never duplicated."""
        self.ensure_one()
        labels, plan = self._build_plan()
        kind = CSV_KIND[self.import_type]
        model = self.env[TARGET_MODEL[kind]].with_company(self.company_id)
        for entry in plan:
            if entry["status"] == "create":
                try:
                    with self.env.cr.savepoint():
                        record = model.create(entry["vals"])
                except (UserError, ValidationError, ValueError, PsycopgError) as exc:
                    entry["status"] = "error"
                    entry["message"] = "Could not create: %s" % exc
                else:
                    entry["record"] = record
            elif entry["status"] == "skip" and entry.get("write"):
                entry["record"].write(entry["write"])

        log = self.env["onepc.qb.import.log"].create(
            {
                "name": "%s: %s"
                % (dict(IMPORT_TYPES)[self.import_type], self.filename or "CSV file"),
                "import_type": self.import_type,
                "filename": self.filename,
                "company_id": self.company_id.id,
                "created_count": sum(1 for e in plan if e["status"] == "create"),
                "skipped_count": sum(1 for e in plan if e["status"] == "skip"),
                "error_count": sum(1 for e in plan if e["status"] == "error"),
                "line_ids": [
                    (
                        0,
                        0,
                        {
                            "row": e["row"],
                            "label": e["label"],
                            "status": e["status"],
                            "message": e["message"],
                            "res_model": TARGET_MODEL[kind] if e.get("record") else False,
                            "res_id": e["record"].id if e.get("record") else 0,
                        },
                    )
                    for e in plan
                ],
            }
        )
        _logger.info(
            "QuickBooks import (%s, file %s): %s created, %s skipped, %s errors",
            self.import_type,
            self.filename,
            log.created_count,
            log.skipped_count,
            log.error_count,
        )
        self.write(
            self._summary_vals(labels, plan, "Created")
            | {"state": "done", "log_id": log.id}
        )
        return self._reopen()

    def action_open_log(self):
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "res_model": "onepc.qb.import.log",
            "res_id": self.log_id.id,
            "view_mode": "form",
            "target": "current",
        }

    # ------------------------------------------------------------------
    # Planning (shared by preview and import)
    # ------------------------------------------------------------------

    def _build_plan(self):
        """Parse the file and decide, row by row, what to do.

        :return: ``(labels, plan)``. ``plan`` is a list of dicts with keys
            ``row``, ``label``, ``status`` (create/skip/error), ``message``,
            ``vals`` (values to create), ``record`` (matched record) and
            ``write`` (values to write on the matched record).
        """
        self.ensure_one()
        if not self.data_file:
            raise UserError("Please upload a CSV file first.")
        kind = CSV_KIND[self.import_type]
        try:
            labels, rows = qb_csv.read_rows(base64.b64decode(self.data_file), kind)
        except qb_csv.QuickBooksCSVError as exc:
            raise UserError(str(exc)) from exc
        planner = getattr(self, "_plan_%s" % kind)
        return labels, planner(rows)

    @staticmethod
    def _entry(row, label):
        return {
            "row": row,
            "label": label,
            "status": "error",
            "message": "",
            "vals": None,
            "record": None,
            "write": None,
        }

    @staticmethod
    def _mark_duplicate(entry, seen, keys):
        """Skip a row that repeats an earlier row of the same file."""
        for key in keys:
            if key in seen:
                entry["status"] = "skip"
                entry["message"] = "Duplicate of CSV line %s." % seen[key]
                return True
        for key in keys:
            seen[key] = entry["row"]
        return False

    # Partners ------------------------------------------------------------

    def _plan_partner(self, rows):
        partners = self.env["res.partner"]
        is_customer = self.import_type == "customer"
        rank_field = "customer_rank" if is_customer else "supplier_rank"
        role = "customer" if is_customer else "vendor"
        seen = {}
        plan = []
        for line, values in rows:
            name = (
                values.get("name")
                or values.get("company")
                or " ".join(
                    p for p in (values.get("first_name"), values.get("last_name")) if p
                )
            ).strip()
            entry = self._entry(line, name)
            plan.append(entry)
            if not name:
                entry["message"] = "Missing name."
                continue
            email = qb_csv.first_email(values.get("email"))
            keys = ["name:" + name.lower()] + (["email:" + email] if email else [])
            if self._mark_duplicate(entry, seen, keys):
                continue

            existing, matched_by = partners.browse(), ""
            if email:
                existing = partners.search([("email", "=ilike", like_escape(email))], limit=1)
                matched_by = "email"
            if not existing:
                existing = partners.search([("name", "=ilike", like_escape(name))], limit=1)
                matched_by = "name"
            if existing:
                entry.update(status="skip", record=existing)
                entry["message"] = "Already exists (matched by %s): %s." % (
                    matched_by,
                    existing.display_name,
                )
                if not existing[rank_field]:
                    entry["write"] = {rank_field: 1}
                    entry["message"] += " It will be marked as a %s." % role
                continue

            vals, notes = self._partner_vals(values, name, email, rank_field)
            entry.update(status="create", vals=vals, message=" ".join(notes))
        return plan

    def _partner_vals(self, values, name, email, rank_field):
        notes = []
        vals = {"name": name, rank_field: 1}
        company = values.get("company")
        if company and company.lower() == name.lower():
            vals["is_company"] = True
        elif company:
            vals["company_name"] = company
        if email:
            vals["email"] = email
        for field in ("phone", "mobile", "street", "street2", "city", "zip", "website"):
            if values.get(field):
                vals[field] = values[field]
        if values.get("notes"):
            vals["comment"] = plaintext2html(values["notes"])

        country = self._find_country(values.get("country"))
        if values.get("country") and not country:
            notes.append('Country "%s" not found.' % values["country"])
        if country:
            vals["country_id"] = country.id
        if values.get("state"):
            state = self._find_state(values["state"], country or self.company_id.country_id)
            if state:
                vals["state_id"] = state.id
                vals.setdefault("country_id", state.country_id.id)
            else:
                notes.append('State "%s" not found.' % values["state"])
        return vals, notes

    def _find_country(self, value):
        countries = self.env["res.country"]
        value = (value or "").strip()
        if not value:
            return countries
        if value.lower() in US_ALIASES:
            return countries.search([("code", "=", "US")], limit=1)
        if len(value) == 2:
            return countries.search([("code", "=ilike", value)], limit=1)
        return countries.search([("name", "=ilike", like_escape(value))], limit=1)

    def _find_state(self, value, country):
        states = self.env["res.country.state"]
        if not country:
            return states
        return states.search(
            [
                ("country_id", "=", country.id),
                "|",
                ("code", "=ilike", like_escape(value)),
                ("name", "=ilike", like_escape(value)),
            ],
            limit=1,
        )

    # Products ------------------------------------------------------------

    def _plan_product(self, rows):
        products = self.env["product.template"].with_company(self.company_id)
        seen = {}
        plan = []
        for line, values in rows:
            name = values.get("name", "")
            sku = values.get("sku", "")
            entry = self._entry(line, "[%s] %s" % (sku, name) if sku else name)
            plan.append(entry)
            if not name:
                entry["message"] = "Missing name."
                continue
            qb_type = values.get("type", "")
            kind = qb_csv.map_product_type(qb_type)
            if kind is None:
                entry["message"] = 'Unknown QuickBooks item type "%s".' % qb_type
                continue
            if kind == qb_csv.SKIP_PRODUCT:
                entry["status"] = "skip"
                entry["message"] = 'QuickBooks "%s" items are not products in Odoo.' % qb_type
                continue
            if kind == qb_csv.UNSUPPORTED_PRODUCT:
                entry["message"] = (
                    'QuickBooks "%s" items are not imported. Create their '
                    "components first, then rebuild the bundle in Odoo." % qb_type
                )
                continue
            try:
                price = qb_csv.parse_amount(values.get("price"))
                cost = qb_csv.parse_amount(values.get("cost"))
            except ValueError as exc:
                entry["message"] = "Invalid amount: %s." % exc
                continue
            keys = ["name:" + name.lower()] + (["sku:" + sku.lower()] if sku else [])
            if self._mark_duplicate(entry, seen, keys):
                continue

            existing, matched_by = products.browse(), ""
            if sku:
                existing = products.search([("default_code", "=", sku)], limit=1)
                matched_by = "SKU"
            if not existing:
                existing = products.search([("name", "=ilike", like_escape(name))], limit=1)
                matched_by = "name"
            if existing:
                entry.update(status="skip", record=existing)
                entry["message"] = "Already exists (matched by %s): %s." % (
                    matched_by,
                    existing.display_name,
                )
                continue

            notes = []
            vals = {
                "name": name,
                "list_price": price or 0.0,
                "standard_price": cost or 0.0,
                "sale_ok": True,
                "purchase_ok": True,
            }
            if sku:
                vals["default_code"] = sku
            if values.get("description"):
                vals["description_sale"] = values["description"]
            if values.get("purchase_description"):
                vals["description_purchase"] = values["purchase_description"]
            vals.update(self._product_type_vals(kind, notes))
            entry.update(status="create", vals=vals, message=" ".join(notes))
        return plan

    def _product_type_vals(self, kind, notes):
        """Product type values for Odoo 17 and 18.

        Odoo 17 uses ``detailed_type`` (``product`` = storable, only with the
        Inventory app). Odoo 18 uses ``type`` plus the ``is_storable`` flag
        added by the Inventory app.
        """
        template = self.env["product.template"]
        type_field = "detailed_type" if "detailed_type" in template._fields else "type"
        if kind == "service":
            return {type_field: "service"}
        if kind == "storable":
            if "is_storable" in template._fields:
                return {"type": "consu", "is_storable": True}
            if type_field == "detailed_type":
                selection = template.fields_get(["detailed_type"])["detailed_type"]["selection"]
                if "product" in dict(selection):
                    return {"detailed_type": "product"}
            notes.append(
                "Inventory app not installed: imported as goods without stock tracking."
            )
        return {type_field: "consu"}

    # Chart of accounts -------------------------------------------------

    def _plan_account(self, rows):
        accounts = self.env["account.account"].with_company(self.company_id)
        company_domain = accounts._check_company_domain(self.company_id)
        seen = {}
        used_codes = set()
        plan = []
        for line, values in rows:
            name = values.get("name", "")
            raw_code = values.get("code", "")
            entry = self._entry(line, "%s %s" % (raw_code, name) if raw_code else name)
            plan.append(entry)
            if not name:
                entry["message"] = "Missing account name."
                continue
            qb_type = values.get("type", "")
            account_type = qb_csv.map_account_type(qb_type, values.get("detail_type"))
            if account_type == qb_csv.SKIP_ACCOUNT:
                entry["status"] = "skip"
                entry["message"] = (
                    "Non-posting account (estimates, orders): not a general "
                    "ledger account in Odoo."
                )
                continue
            if not account_type:
                entry["message"] = (
                    'Unknown QuickBooks account type "%s".' % qb_type
                    if qb_type
                    else "Missing account type."
                )
                continue
            code = qb_csv.sanitize_account_code(raw_code) if raw_code else ""
            if raw_code and not code:
                entry["message"] = 'Account number "%s" cannot be used as an Odoo code.' % raw_code
                continue
            keys = ["name:" + name.lower()] + (["code:" + code.lower()] if code else [])
            if self._mark_duplicate(entry, seen, keys):
                continue

            existing, matched_by = accounts.browse(), ""
            if code:
                existing = accounts.search(company_domain + [("code", "=", code)], limit=1)
                matched_by = "code"
            if not existing:
                existing = accounts.search(
                    company_domain + [("name", "=ilike", like_escape(name))], limit=1
                )
                matched_by = "name"
            if existing:
                entry.update(status="skip", record=existing)
                entry["message"] = "Already exists (matched by %s): %s %s." % (
                    matched_by,
                    existing.code,
                    existing.name,
                )
                continue

            notes = []
            if not code:
                code = self._next_account_code(accounts, company_domain, account_type, used_codes)
                notes.append("No account number in the file: code %s assigned." % code)
            used_codes.add(code)
            vals = {
                "name": name,
                "code": code,
                "account_type": account_type,
                "reconcile": account_type in ("asset_receivable", "liability_payable"),
            }
            if values.get("description"):
                vals["note"] = values["description"]
            notes.insert(0, "Type: %s." % account_type)
            entry.update(status="create", vals=vals, message=" ".join(notes))
        return plan

    @staticmethod
    def _next_account_code(accounts, company_domain, account_type, used_codes):
        """Generate a free code such as ``190001`` (assets), ``690003``
        (expenses) for accounts exported without account numbers."""
        prefix = qb_csv.account_code_prefix(account_type) + "9"
        number = 1
        while True:
            code = "%s%04d" % (prefix, number)
            if code not in used_codes and not accounts.search_count(
                company_domain + [("code", "=", code)], limit=1
            ):
                return code
            number += 1

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _summary_vals(self, labels, plan, create_label):
        columns = Markup("").join(
            Markup("<li><b>%s</b> &#8592; %s</li>") % (FIELD_LABELS.get(field, field), header)
            for field, header in labels.items()
        )
        return {
            "create_count": sum(1 for e in plan if e["status"] == "create"),
            "skip_count": sum(1 for e in plan if e["status"] == "skip"),
            "error_count": sum(1 for e in plan if e["status"] == "error"),
            "columns_html": Markup("<ul class='mb-0'>%s</ul>") % columns,
            "preview_html": render_rows_html(plan, created_label=create_label),
        }

    def _reopen(self):
        return {
            "type": "ir.actions.act_window",
            "name": "QuickBooks Import",
            "res_model": self._name,
            "res_id": self.id,
            "view_mode": "form",
            "target": "new",
        }
