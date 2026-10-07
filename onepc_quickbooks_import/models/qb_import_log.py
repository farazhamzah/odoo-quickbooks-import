# Copyright 2026 OnePC Technologies LLC
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).
from markupsafe import Markup

from odoo import api, fields, models
from odoo.release import version_info

IMPORT_TYPES = [
    ("customer", "Customers"),
    ("vendor", "Vendors"),
    ("product", "Products & Services"),
    ("account", "Chart of Accounts"),
]

ROW_STATUSES = [
    ("create", "Create"),
    ("skip", "Skip"),
    ("error", "Error"),
]

STATUS_BADGES = {
    "create": "text-bg-success",
    "skip": "text-bg-secondary",
    "error": "text-bg-danger",
}

# Odoo 18 renamed the "tree" view type to "list".
LIST_VIEW_TYPE = "list" if version_info[0] >= 18 else "tree"


def render_rows_html(rows, created_label="Created"):
    """Render import rows as an HTML table.

    ``rows`` is an iterable of dicts with ``row``, ``label``, ``status``
    and ``message`` keys.
    """
    labels = {"create": created_label, "skip": "Skip", "error": "Error"}
    body = Markup("").join(
        Markup(
            "<tr><td>%s</td><td>%s</td>"
            '<td><span class="badge %s">%s</span></td><td>%s</td></tr>'
        )
        % (
            row["row"],
            row["label"] or "",
            STATUS_BADGES.get(row["status"], "text-bg-light"),
            labels.get(row["status"], row["status"]),
            row["message"] or "",
        )
        for row in rows
    )
    if not body:
        return Markup("<p>No data rows found.</p>")
    return (
        Markup(
            '<table class="table table-sm table-striped o_qb_import_table">'
            "<thead><tr><th>CSV line</th><th>Record</th><th>Action</th>"
            "<th>Details</th></tr></thead><tbody>"
        )
        + body
        + Markup("</tbody></table>")
    )


class QbImportLog(models.Model):
    _name = "onepc.qb.import.log"
    _description = "QuickBooks Import Log"
    _order = "id desc"

    name = fields.Char(required=True, readonly=True)
    import_type = fields.Selection(IMPORT_TYPES, required=True, readonly=True)
    filename = fields.Char(readonly=True)
    user_id = fields.Many2one(
        "res.users", default=lambda self: self.env.user, readonly=True
    )
    company_id = fields.Many2one(
        "res.company", default=lambda self: self.env.company, readonly=True
    )
    line_ids = fields.One2many(
        "onepc.qb.import.log.line", "log_id", readonly=True
    )
    created_count = fields.Integer(readonly=True)
    skipped_count = fields.Integer(readonly=True)
    error_count = fields.Integer(readonly=True)
    details_html = fields.Html(
        compute="_compute_details_html", sanitize=False, string="Details"
    )

    @api.depends("line_ids")
    def _compute_details_html(self):
        for log in self:
            log.details_html = render_rows_html(
                [
                    {
                        "row": line.row,
                        "label": line.label,
                        "status": line.status,
                        "message": line.message,
                    }
                    for line in log.line_ids
                ]
            )

    def action_view_created_records(self):
        self.ensure_one()
        lines = self.line_ids.filtered(lambda l: l.status == "create" and l.res_id)
        res_model = lines[:1].res_model
        return {
            "type": "ir.actions.act_window",
            "name": "Imported records",
            "res_model": res_model or "res.partner",
            "view_mode": "%s,form" % LIST_VIEW_TYPE,
            "domain": [("id", "in", lines.mapped("res_id"))],
            "context": {"active_test": False},
        }

    @api.model
    def _setup_version_specific_views(self):
        """Create the list view for this model.

        Odoo 17 calls the view type ``tree`` and Odoo 18 calls it ``list``.
        A single XML file cannot serve both, so the view is created here
        with an XML id owned by this module (it is removed on uninstall).
        """
        arch = (
            '<{tag} create="false" edit="false">'
            '<field name="create_date" string="Date"/>'
            '<field name="name"/>'
            '<field name="import_type"/>'
            '<field name="filename"/>'
            '<field name="user_id"/>'
            '<field name="created_count" string="Created"/>'
            '<field name="skipped_count" string="Skipped"/>'
            '<field name="error_count" string="Errors"/>'
            "</{tag}>"
        ).format(tag=LIST_VIEW_TYPE)
        self.env["ir.ui.view"]._load_records(
            [
                {
                    "xml_id": "onepc_quickbooks_import.qb_import_log_view_list",
                    "values": {
                        "name": "onepc.qb.import.log.list",
                        "model": self._name,
                        "arch": arch,
                        "priority": 16,
                    },
                    "noupdate": False,
                }
            ]
        )
        action = self.env.ref("onepc_quickbooks_import.action_qb_import_log")
        action.view_mode = "%s,form" % LIST_VIEW_TYPE


class QbImportLogLine(models.Model):
    _name = "onepc.qb.import.log.line"
    _description = "QuickBooks Import Log Line"
    _order = "log_id, row, id"

    log_id = fields.Many2one(
        "onepc.qb.import.log", required=True, ondelete="cascade", index=True
    )
    row = fields.Integer(string="CSV line")
    label = fields.Char(string="Record")
    status = fields.Selection(ROW_STATUSES, required=True)
    message = fields.Char()
    res_model = fields.Char()
    res_id = fields.Integer()

