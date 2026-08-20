# Copyright (C) 2026 Mr Abdulkarim M. Mousa
# @ Valutoria L.T.D. <abdulkarim@valutoria.com>
# License AGPL-3.0 or later (http://www.gnu.org/licenses/agpl).

from odoo import fields, models


class ResDistrictPolygonPoint(models.Model):
    _name = "res.district.polygon.point"
    _description = "District Polygon Point"
    _order = "sequence, id"

    district_id = fields.Many2one(
        "res.district", string="District", required=True, ondelete="cascade"
    )
    sequence = fields.Integer(default=10)
    lat = fields.Float(string="Latitude", digits=(10, 7), required=True)
    lng = fields.Float(string="Longitude", digits=(10, 7), required=True)
