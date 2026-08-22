# Copyright (C) 2019, Patrick Wilson
# License AGPL-3.0 or later (http://www.gnu.org/licenses/agpl).

from odoo import api, fields, models


class Lead(models.Model):
    _inherit = "crm.lead"

    fsm_order_ids = fields.One2many(
        "fsm.order", "opportunity_id", string="Service Orders"
    )
    fsm_location_id = fields.Many2one(
        "fsm.location",
        string="FSM Location",
        domain="[('partner_id', '=', partner_id)]",
    )
    fsm_order_count = fields.Integer(
        compute="_compute_fsm_order_count", string="# FSM Orders"
    )
    district_id = fields.Many2one("res.district", string="District")
    region_id = fields.Many2one(
        "res.region",
        string="Region",
        compute="_compute_region_id",
        store=True,
        readonly=False,
    )
    state_id = fields.Many2one(
        "res.country.state",
        string="State",
        compute="_compute_state_id",
        store=True,
        readonly=False,
    )
    country_id = fields.Many2one(
        "res.country",
        string="Country",
        compute="_compute_country_id",
        store=True,
        readonly=False,
    )

    @api.depends("district_id")
    def _compute_region_id(self):
        for rec in self:
            rec.region_id = rec.district_id.region_id if rec.district_id else False

    @api.depends("region_id")
    def _compute_state_id(self):
        for rec in self:
            rec.state_id = rec.region_id.state_id if rec.region_id else False

    @api.depends("state_id")
    def _compute_country_id(self):
        for rec in self:
            rec.country_id = rec.state_id.country_id if rec.state_id else False

    @api.onchange("region_id")
    def _onchange_region_id(self):
        return {
            "domain": {
                "district_id": (
                    [("region_id", "=", self.region_id.id)] if self.region_id else []
                )
            }
        }

    @api.onchange("state_id")
    def _onchange_state_id(self):
        return {
            "domain": {
                "region_id": (
                    [("state_id", "=", self.state_id.id)] if self.state_id else []
                )
            }
        }

    @api.onchange("country_id")
    def _onchange_country_id(self):
        return {
            "domain": {
                "state_id": (
                    [("country_id", "=", self.country_id.id)] if self.country_id else []
                )
            }
        }

    partner_latitude = fields.Float(string="Latitude", digits=(8, 6))
    partner_longitude = fields.Float(string="Longitude", digits=(8, 6))

    def _compute_fsm_order_count(self):
        for rec in self:
            rec.fsm_order_count = len(rec.fsm_order_ids)

    def write(self, vals):
        res = super().write(vals)
        if {
            "partner_latitude",
            "partner_longitude",
            "district_id",
            "fsm_location_id",
        } & set(vals):
            for rec in self:
                if rec.fsm_location_id and rec.fsm_location_id.partner_id:
                    loc_vals = {}
                    if "partner_latitude" in vals and rec.partner_latitude:
                        loc_vals["partner_latitude"] = rec.partner_latitude
                    if "partner_longitude" in vals and rec.partner_longitude:
                        loc_vals["partner_longitude"] = rec.partner_longitude
                    if "district_id" in vals and rec.district_id:
                        loc_vals["district_id"] = rec.district_id.id
                    if loc_vals:
                        rec.fsm_location_id.partner_id.write(loc_vals)
        return res

    def action_create_fsm_order(self):
        self.ensure_one()
        action = self.env["ir.actions.actions"]._for_xml_id(
            "fieldservice.action_fsm_operation_order"
        )
        action["context"] = {
            "default_opportunity_id": self.id,
            "default_location_id": self.fsm_location_id.id,
            "default_description": self.description,
            "default_priority": self.priority,
        }
        view = self.env.ref("fieldservice.fsm_order_form", raise_if_not_found=False)
        action["views"] = [(view and view.id or False, "form")]
        return action
