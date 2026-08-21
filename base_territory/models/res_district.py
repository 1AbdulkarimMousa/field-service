# Copyright (C) 2020 Gray Matter Logic
# License AGPL-3.0 or later (http://www.gnu.org/licenses/agpl).

from odoo import api, fields, models


class ResDistrict(models.Model):
    _name = "res.district"
    _description = "District"

    name = fields.Char(required=True)
    region_id = fields.Many2one("res.region", string="Region")
    partner_id = fields.Many2one("res.partner", string="District Manager")
    description = fields.Char()
    polygon_ids = fields.One2many(
        "res.district.polygon.point", "district_id", string="Polygon Points"
    )

    @api.model
    def find_by_coordinates(self, lat, lng):
        if lat is None or lng is None:
            return self.env["res.district"]
        districts = self.search([("polygon_ids", "!=", False)])
        for district in districts:
            points = district.polygon_ids.sorted("sequence")
            if len(points) >= 3 and self._point_in_polygon(
                float(lat), float(lng), [(point.lat, point.lng) for point in points]
            ):
                return district
        return self.env["res.district"]

    @api.model
    def _point_in_polygon(self, lat, lng, polygon):
        inside = False
        previous = len(polygon) - 1
        for current, (current_lat, current_lng) in enumerate(polygon):
            previous_lat, previous_lng = polygon[previous]
            if ((current_lat > lat) != (previous_lat > lat)) and (
                lng
                < (previous_lng - current_lng)
                * (lat - current_lat)
                / (previous_lat - current_lat)
                + current_lng
            ):
                inside = not inside
            previous = current
        return inside
