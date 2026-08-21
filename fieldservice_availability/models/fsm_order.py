# Copyright 2025 APSL-Nagarro Antoni Marroig
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
from odoo import api, models
from odoo.exceptions import ValidationError
from odoo.tools.misc import format_date


class FSMRoute(models.Model):
    _inherit = "fsm.order"

    def _reassign_dayroute_for_person_timezone(self):
        """Re-evaluate the day route after the assigned route changes."""
        self.ensure_one()
        if not self.scheduled_date_start:
            return
        route = self.fsm_route_id
        person = route.fsm_person_id if route else self.person_id
        if not person:
            if self.dayroute_id:
                self.write({"dayroute_id": False})
            return
        old_dayroute = self.dayroute_id
        managed = self._manage_fsm_route(
            {
                "fsm_route_id": route.id if route else False,
                "person_id": person.id,
                "scheduled_date_start": self.scheduled_date_start,
            }
        )
        new_dayroute_id = managed.get("dayroute_id")
        updates = {"person_id": person.id}
        if new_dayroute_id != (old_dayroute.id if old_dayroute else False):
            updates["dayroute_id"] = new_dayroute_id
        self.write(updates)

    @api.constrains("scheduled_date_start", "location_id")
    def check_black_out_days(self):
        for order in self:
            if not (order.fsm_route_id and order.scheduled_date_start):
                continue
            order_date, order_zip = order.scheduled_date_start.date(), order.zip
            blackout_days = (
                order.fsm_route_id.fsm_blackout_group_ids.fsm_blackout_day_ids
            )
            match = blackout_days.filtered(
                lambda x, d=order_date, z=order_zip: x.date == d
                and (not x.zip or x.zip == z)
            )
            if match:
                raise ValidationError(
                    self.env._(
                        "The date %(date)s is a blackout day for field"
                        " service operations on this route.",
                        date=format_date(order.env, order.scheduled_date_start),
                    )
                )
