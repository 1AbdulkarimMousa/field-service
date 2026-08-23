from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

from odoo import Command, fields
from odoo.tests.common import tagged
from odoo.tools import mute_logger

from odoo.addons.base.tests.common import TransactionCaseWithUserPortal
from odoo.addons.fieldservice_portal.controllers import _booking, visit_portal
from odoo.addons.http_routing.tests.common import MockRequest


@tagged("post_install", "-at_install")
class TestVisitController(TransactionCaseWithUserPortal):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls.env.company
        cls.country = cls.company.country_id or cls.env.ref("base.us")
        cls.company.write({"country_id": cls.country.id, "city": "Portal City"})
        cls.partner_portal.write(
            {
                "phone": "+1 555 0100",
                "street": "Original customer street",
            }
        )

        cls.state = cls.env["res.country.state"].create(
            {
                "name": "Portal Test State",
                "code": "PTS",
                "country_id": cls.country.id,
            }
        )
        cls.region = cls.env["res.region"].create(
            {"name": "Portal Test Region", "state_id": cls.state.id}
        )
        cls.district = cls.env["res.district"].create(
            {
                "name": "Portal Test District",
                "region_id": cls.region.id,
                "company_id": cls.company.id,
            }
        )

        cls.visit_team = cls.env["crm.team"].create(
            {
                "name": "Portal Visit Team",
                "company_id": cls.company.id,
                "user_id": cls.env.user.id,
            }
        )
        cls.fsm_team = cls.env["fsm.team"].create(
            {"name": "Portal Route Team", "company_id": cls.company.id}
        )
        cls.route = cls.env["fsm.route"].create(
            {
                "name": "Portal Visit Route",
                "route_type": "visit",
                "max_order": 5,
                "day_ids": [(6, 0, cls.env["fsm.route.day"].search([]).ids)],
            }
        )
        cls.dayroute = cls.env["fsm.route.dayroute"].create(
            {
                "route_id": cls.route.id,
                "team_id": cls.fsm_team.id,
                "date": fields.Date.today() + timedelta(days=1),
            }
        )

        cls.survey_type = cls.env["fsm.order.type"].search(
            [("service_type", "=", "survey")], limit=1
        ) or cls.env["fsm.order.type"].create(
            {"name": "Portal Survey", "service_type": "survey"}
        )
        cls.service_product = cls.env["product.product"].create(
            {
                "name": "Portal Survey Service",
                "type": "service",
                "field_service_tracking": "sale",
                "list_price": 125.0,
            }
        )
        cls.survey_template = cls.env["sale.order.template"].create(
            {
                "name": "Portal Survey Template",
                "service_type": "survey",
                "company_id": cls.company.id,
            }
        )
        cls.env["sale.order.template.line"].create(
            {
                "sale_order_template_id": cls.survey_template.id,
                "product_id": cls.service_product.id,
                "product_uom_id": cls.service_product.uom_id.id,
                "product_uom_qty": 1,
            }
        )
        cls.company.write(
            {
                "visit_crm_team_id": cls.visit_team.id,
                "visit_sale_order_template_id": cls.survey_template.id,
            }
        )

    def setUp(self):
        super().setUp()
        self.company.invalidate_recordset(
            [
                "country_id",
                "visit_crm_team_id",
                "visit_sale_order_template_id",
            ]
        )
        self.company.write(
            {
                "country_id": self.country.id,
                "visit_crm_team_id": self.visit_team.id,
                "visit_sale_order_template_id": self.survey_template.id,
            }
        )
        self.route.write({"route_type": "visit", "max_order": 5})
        self.dayroute.write(
            {
                "route_id": self.route.id,
                "team_id": self.fsm_team.id,
                "date": fields.Date.context_today(self.env.user) + timedelta(days=1),
            }
        )
        self.dayroute._compute_order_count()
        portal_env = self.env(user=self.user_portal)
        website = self.env["website"].search([], limit=1)
        self.request = SimpleNamespace(env=portal_env, website=website)
        self.mock_request = MockRequest(portal_env, website=website)
        self.mock_request.__enter__()
        self.addCleanup(self.mock_request.__exit__, None, None, None)
        self.booking_request_patch = patch.object(_booking, "request", self.request)
        self.visit_request_patch = patch.object(visit_portal, "request", self.request)
        self.booking_request_patch.start()
        self.visit_request_patch.start()
        self.addCleanup(self.booking_request_patch.stop)
        self.addCleanup(self.visit_request_patch.stop)
        self.controller = visit_portal.VisitPortal()

    def _mapped_visit_values(self, **values):
        return {
            "route_id": str(self.dayroute.id),
            "country_code": self.country.code.lower(),
            "latitude": "30.123456",
            "longitude": "31.234567",
            "street": "10 Portal Street",
            "unit": "Building 4",
            "city": "Portal City",
            "zip": "12345",
            "state_id": str(self.state.id),
            "region_id": str(self.region.id),
            "district_id": str(self.district.id),
            **values,
        }

    def _create_multiline_survey_template(self):
        line_product = self.env["product.product"].create(
            {
                "name": "Portal Per-Line Survey",
                "type": "service",
                "field_service_tracking": "line",
            }
        )
        line_values = {
            "product_id": line_product.id,
            "product_uom_id": line_product.uom_id.id,
            "product_uom_qty": 1,
        }
        return self.env["sale.order.template"].create(
            {
                "name": "Portal Two-Order Survey",
                "service_type": "survey",
                "company_id": self.company.id,
                "sale_order_template_line_ids": [
                    Command.create(line_values),
                    Command.create(line_values),
                ],
            }
        )

    def _create_route_dayroute(
        self,
        route_type="visit",
        *,
        date_offset=2,
        max_order=5,
        company=None,
        person=None,
    ):
        company = company or self.company
        team = (
            self.fsm_team
            if company == self.company
            else self.env["fsm.team"].create(
                {
                    "name": f"{company.name} Portal Route Team",
                    "company_id": company.id,
                }
            )
        )
        route = self.env["fsm.route"].create(
            {
                "name": f"Portal {route_type.title()} Route {date_offset}",
                "route_type": route_type,
                "fsm_person_id": person.id if person else False,
                "max_order": max_order,
                "day_ids": [Command.set(self.env["fsm.route.day"].search([]).ids)],
            }
        )
        dayroute = self.env["fsm.route.dayroute"].create(
            {
                "route_id": route.id,
                "team_id": team.id,
                "date": fields.Date.context_today(self.env.user)
                + timedelta(days=date_offset),
            }
        )
        return route, dayroute

    def _reserve_dayroute(self, dayroute, *, validity_date=None):
        values = {
            "partner_id": self.partner_portal.id,
            "portal_dayroute_id": dayroute.id,
            "order_line": [
                Command.create(
                    {
                        "product_id": self.service_product.id,
                        "product_uom_qty": 1,
                    }
                )
            ],
        }
        if validity_date is not None:
            values["validity_date"] = validity_date
        return self.env["sale.order"].create(values)

    def _assert_submit_error(self, expected, **overrides):
        result = self.controller.submit_visit_request(
            **self._mapped_visit_values(**overrides)
        )
        self.assertFalse(result["success"], result)
        self.assertIn(expected.lower(), result["error"].lower())
        return result

    def test_booking_domains_filter_date_route_company_and_capacity(self):
        end_date = fields.Date.context_today(self.env.user) + timedelta(days=7)
        self.assertEqual(
            _booking.dayroute_domain("visit", end_date=end_date),
            [
                ("date", ">=", fields.Date.context_today(self.request.env.user)),
                ("order_remaining", ">", 0),
                ("team_id.company_id", "=", self.company.id),
                ("route_id.route_type", "=", "visit"),
                ("date", "<=", end_date),
            ],
        )
        self.assertNotIn(
            ("route_id.route_type", "=", "visit"), _booking.dayroute_domain()
        )

        _maintenance_route, maintenance = self._create_route_dayroute(
            "maintenance", date_offset=2
        )
        _late_route, late = self._create_route_dayroute("visit", date_offset=30)
        other_company = self.env["res.company"].create(
            {"name": "Portal Domain Other Company"}
        )
        _foreign_route, foreign = self._create_route_dayroute(
            "visit", date_offset=3, company=other_company
        )
        _full_route, full = self._create_route_dayroute(
            "visit", date_offset=4, max_order=1
        )
        reservation = self._reserve_dayroute(full)

        available = _booking.dayroutes_with_available_capacity(
            "visit", end_date=end_date
        )
        available_dayroutes = [item[0] for item in available]

        self.assertIn(self.dayroute, available_dayroutes)
        self.assertNotIn(maintenance, available_dayroutes)
        self.assertNotIn(late, available_dayroutes)
        self.assertNotIn(foreign, available_dayroutes)
        self.assertNotIn(full, available_dayroutes)
        self.assertEqual(_booking.dayroute_available_capacity(full), 0)

        reservation.action_cancel()
        self.assertEqual(_booking.dayroute_available_capacity(full), 1)
        expired = self._reserve_dayroute(
            full,
            validity_date=fields.Date.context_today(self.env.user) - timedelta(days=1),
        )
        self.assertEqual(_booking.dayroute_available_capacity(full), 1)
        self.assertEqual(expired.state, "draft")

    def test_get_dayroute_validates_identity_access_lock_and_capacity(self):
        for value in (None, "not-an-id"):
            with (
                self.subTest(value=value),
                self.assertRaisesRegex(
                    _booking.PortalBookingError, "valid appointment"
                ),
            ):
                _booking.get_dayroute(value, "visit")

        with self.assertRaisesRegex(_booking.PortalBookingError, "no longer available"):
            _booking.get_dayroute(999999999, "visit")
        with self.assertRaisesRegex(_booking.PortalBookingError, "no longer available"):
            _booking.get_dayroute(self.dayroute.id, "maintenance")
        with self.assertRaisesRegex(_booking.PortalBookingError, "no longer available"):
            _booking.get_dayroute(self.dayroute.id, None)

        _past_route, past = self._create_route_dayroute("visit", date_offset=-1)
        with self.assertRaisesRegex(_booking.PortalBookingError, "no longer available"):
            _booking.get_dayroute(past.id, "visit")

        other_company = self.env["res.company"].create(
            {"name": "Portal Access Other Company"}
        )
        _foreign_route, foreign = self._create_route_dayroute(
            "visit", date_offset=5, company=other_company
        )
        with self.assertRaisesRegex(_booking.PortalBookingError, "no longer available"):
            _booking.get_dayroute(foreign.id, "visit")

        self.assertEqual(
            _booking.get_dayroute(self.dayroute.id, "visit", lock=True),
            self.dayroute,
        )
        self.route.max_order = 1
        self.dayroute._compute_order_count()
        self._reserve_dayroute(self.dayroute)
        with self.assertRaisesRegex(_booking.PortalBookingError, "remaining capacity"):
            _booking.get_dayroute(
                self.dayroute.id, "visit", needed_capacity=1, lock=True
            )

    def test_config_service_and_template_helpers_use_company_records(self):
        self.assertEqual(
            _booking.get_configured_record(
                "sale.order.template", "visit_sale_order_template_id"
            ),
            self.survey_template,
        )
        with self.assertRaisesRegex(_booking.PortalBookingError, "not configured"):
            _booking.get_configured_record("crm.team", "visit_sale_order_template_id")

        self.assertEqual(_booking.get_service_type("survey"), self.survey_type)
        with self.assertRaisesRegex(_booking.PortalBookingError, "not configured"):
            _booking.get_service_type("missing-service")

        self.assertEqual(
            _booking.get_sale_template("visit_sale_order_template_id", "survey"),
            self.survey_template,
        )
        with self.assertRaisesRegex(
            _booking.PortalBookingError, "quotation is not configured"
        ):
            _booking.get_sale_template("visit_sale_order_template_id", "maintenance")

        self.company.visit_sale_order_template_id = False
        self.assertFalse(
            _booking.get_configured_record(
                "sale.order.template", "visit_sale_order_template_id"
            )
        )
        with self.assertRaisesRegex(
            _booking.PortalBookingError, "quotation is not configured"
        ):
            _booking.get_sale_template("visit_sale_order_template_id", "survey")

        empty_template = self.env["sale.order.template"].create(
            {
                "name": "Portal Empty Survey Template",
                "service_type": "survey",
                "company_id": self.company.id,
            }
        )
        self.company.visit_sale_order_template_id = empty_template
        with self.assertRaisesRegex(
            _booking.PortalBookingError, "quotation is not configured"
        ):
            _booking.get_sale_template("visit_sale_order_template_id", "survey")

        untracked_product = self.env["product.product"].create(
            {
                "name": "Portal Untracked Service",
                "type": "service",
                "field_service_tracking": "no",
            }
        )
        self.env["sale.order.template.line"].create(
            {
                "sale_order_template_id": empty_template.id,
                "product_id": untracked_product.id,
                "product_uom_id": untracked_product.uom_id.id,
                "product_uom_qty": 1,
            }
        )
        with self.assertRaisesRegex(
            _booking.PortalBookingError, "quotation is not configured"
        ):
            _booking.get_sale_template("visit_sale_order_template_id", "survey")

    def test_crm_team_and_user_helpers_cover_direct_fallback_and_missing_users(self):
        self.assertEqual(_booking.get_crm_team(), self.visit_team)
        self.assertEqual(_booking.get_team_user(self.visit_team), self.env.user)

        fallback_team = self.env["crm.team"].create(
            {"name": "Portal Fallback Team", "company_id": self.company.id}
        )
        fallback_user = (
            self.env["res.users"]
            .with_context(no_reset_password=True)
            .create(
                {
                    "name": "Portal Fallback User",
                    "login": "portal-fallback-user@example.test",
                    "email": "portal-fallback-user@example.test",
                    "company_id": self.company.id,
                    "company_ids": [Command.set([self.company.id])],
                    "group_ids": [Command.set([self.env.ref("base.group_user").id])],
                }
            )
        )
        self.env["crm.team.member"].create(
            {"crm_team_id": fallback_team.id, "user_id": fallback_user.id}
        )
        fallback_user.invalidate_recordset(["sale_team_id"])
        self.assertEqual(_booking.get_team_user(fallback_team), fallback_user)

        missing_team = self.env["crm.team"].create(
            {"name": "Portal Team Without User", "company_id": self.company.id}
        )
        with self.assertRaisesRegex(_booking.PortalBookingError, "no internal user"):
            _booking.get_team_user(missing_team)

        portal_led_team = self.env["crm.team"].create(
            {
                "name": "Portal User Led Team",
                "company_id": self.company.id,
                "user_id": self.user_portal.id,
            }
        )
        with self.assertRaisesRegex(_booking.PortalBookingError, "no internal user"):
            _booking.get_team_user(portal_led_team)

        self.company.visit_crm_team_id = False
        with self.assertRaisesRegex(
            _booking.PortalBookingError, "team is not configured"
        ):
            _booking.get_crm_team()
        self.assertEqual(
            _booking.district_domain(),
            [("company_id", "in", [self.company.id, False])],
        )

    def test_visit_page_renders_map_fallbacks_and_company_location_catalog(self):
        rendered = []

        def render(template, values):
            response = {"template": template, "values": values}
            rendered.append(response)
            return response

        self.request.render = render
        self.request.website.google_maps_api_key = False
        self.company.write({"country_id": False, "city": False})
        self.company.partner_id.write(
            {"partner_latitude": 0.0, "partner_longitude": 0.0}
        )

        page_endpoint = self.controller.portal_request_visit.original_endpoint
        fallback = page_endpoint(self.controller)

        self.assertEqual(fallback["template"], "fieldservice_portal.portal_visit_form")
        fallback_values = fallback["values"]
        self.assertEqual(fallback_values["partner"], self.partner_portal)
        self.assertEqual(fallback_values["page_name"], "visit")
        self.assertEqual(fallback_values["map_country_code"], "EG")
        self.assertEqual(fallback_values["map_city"], "Cairo")
        self.assertEqual(fallback_values["map_latitude"], 30.0444)
        self.assertEqual(fallback_values["map_longitude"], 31.2357)
        self.assertTrue(fallback_values["manual_location"])

        self.company.write(
            {"country_id": self.country.id, "city": "Configured Portal City"}
        )
        self.company.partner_id.write(
            {"partner_latitude": 29.987654, "partner_longitude": 31.012345}
        )
        self.request.website.google_maps_api_key = "portal-maps-key"

        configured = page_endpoint(self.controller)

        configured_values = configured["values"]
        self.assertEqual(configured_values["map_country_code"], self.country.code)
        self.assertEqual(configured_values["map_city"], "Configured Portal City")
        self.assertEqual(configured_values["map_latitude"], 29.987654)
        self.assertEqual(configured_values["map_longitude"], 31.012345)
        self.assertEqual(configured_values["map_language"], "en")
        self.assertIn(self.state, configured_values["states"])
        self.assertIn(self.region, configured_values["regions"])
        self.assertIn(self.district, configured_values["districts"])
        self.assertFalse(configured_values["manual_location"])
        self.assertEqual(len(rendered), 2)

    def test_route_endpoints_serialize_capacity_and_apply_visit_window(self):
        technician = self.env["fsm.person"].create(
            {"name": "Portal Route Technician", "team_id": self.fsm_team.id}
        )
        self.route.fsm_person_id = technician
        self.dayroute._compute_person_id()
        _maintenance_route, maintenance = self._create_route_dayroute(
            "maintenance", date_offset=2, person=technician
        )
        _far_route, far_visit = self._create_route_dayroute(
            "visit", date_offset=29, person=technician
        )

        general = self.controller.get_available_dayroutes()
        visits = self.controller.get_available_routes()

        self.assertEqual(general["status"], "success")
        self.assertEqual(general["count"], len(general["dayroutes"]))
        general_by_id = {item["id"]: item for item in general["dayroutes"]}
        payload = general_by_id[self.dayroute.id]
        self.assertEqual(payload["name"], self.dayroute.name)
        self.assertEqual(payload["date"], self.dayroute.date.isoformat())
        self.assertEqual(payload["person_id"], technician.id)
        self.assertEqual(payload["person_name"], technician.name)
        self.assertEqual(payload["route_id"], self.route.id)
        self.assertEqual(payload["route_name"], self.route.name)
        self.assertEqual(payload["order_count"], self.dayroute.order_count)
        self.assertEqual(payload["max_order"], self.dayroute.max_order)
        self.assertEqual(payload["order_remaining"], 5)
        self.assertEqual(
            payload["stage_id"],
            self.dayroute.stage_id.id if self.dayroute.stage_id else None,
        )
        self.assertEqual(
            payload["stage_name"],
            self.dayroute.stage_id.name if self.dayroute.stage_id else None,
        )
        self.assertIn(maintenance.id, general_by_id)
        self.assertIn(far_visit.id, general_by_id)

        self.assertTrue(visits["success"])
        visit_by_id = {item["id"]: item for item in visits["routes"]}
        visit_payload = visit_by_id[self.dayroute.id]
        self.assertEqual(visit_payload["name"], self.dayroute.name)
        self.assertEqual(visit_payload["date"], str(self.dayroute.date))
        self.assertEqual(visit_payload["route_name"], self.route.name)
        self.assertEqual(visit_payload["person"], technician.name)
        self.assertEqual(visit_payload["remaining"], 5)
        self.assertEqual(visit_payload["max_order"], 5)
        self.assertNotIn(maintenance.id, visit_by_id)
        self.assertNotIn(far_visit.id, visit_by_id)

    def test_route_and_district_endpoints_return_safe_generic_errors(self):
        invalid_request = SimpleNamespace(env=None)
        with (
            patch.object(visit_portal, "request", invalid_request),
            patch.object(_booking, "request", invalid_request),
            mute_logger("odoo.addons.fieldservice_portal.controllers.visit_portal"),
        ):
            general = self.controller.get_available_dayroutes()
            visits = self.controller.get_available_routes()
            districts = self.controller.get_district_polygons()

        self.assertEqual(general["status"], "error")
        self.assertIn("temporarily unavailable", general["message"])
        self.assertFalse(visits["success"])
        self.assertIn("temporarily unavailable", visits["error"])
        self.assertEqual(districts["status"], "error")
        self.assertIn("temporarily unavailable", districts["message"])

    def test_district_endpoint_returns_current_and_shared_polygons_only(self):
        self.env["res.district.polygon.point"].create(
            [
                {
                    "district_id": self.district.id,
                    "sequence": 10,
                    "lat": 30.1,
                    "lng": 31.1,
                },
                {
                    "district_id": self.district.id,
                    "sequence": 20,
                    "lat": 30.2,
                    "lng": 31.2,
                },
            ]
        )
        shared = self.env["res.district"].create(
            {
                "name": "Shared Portal District",
                "region_id": self.region.id,
                "company_id": False,
            }
        )
        other_company = self.env["res.company"].create(
            {"name": "Portal District Other Company"}
        )
        foreign = self.env["res.district"].create(
            {
                "name": "Foreign Portal District",
                "region_id": self.region.id,
                "company_id": other_company.id,
            }
        )

        result = self.controller.get_district_polygons()

        self.assertEqual(result["status"], "success")
        by_id = {item["id"]: item for item in result["districts"]}
        self.assertEqual(
            by_id[self.district.id]["polygon"],
            [{"lat": 30.1, "lng": 31.1}, {"lat": 30.2, "lng": 31.2}],
        )
        self.assertEqual(by_id[shared.id]["polygon"], [])
        self.assertNotIn(foreign.id, by_id)

    def test_submit_reports_configuration_user_and_appointment_errors(self):
        self.company.visit_crm_team_id = False
        self._assert_submit_error("team is not configured")
        self.company.visit_crm_team_id = self.visit_team

        self.company.visit_sale_order_template_id = False
        self._assert_submit_error("quotation is not configured")
        self.company.visit_sale_order_template_id = self.survey_template

        no_user_team = self.env["crm.team"].create(
            {"name": "Portal Submit Team Without User", "company_id": self.company.id}
        )
        self.company.visit_crm_team_id = no_user_team
        self._assert_submit_error("no internal user")
        self.company.visit_crm_team_id = self.visit_team

        self._assert_submit_error("appointment", route_id="not-a-route")

    def test_submit_validates_country_coordinates_and_territory_hierarchy(self):
        other_country = self.env["res.country"].search(
            [("id", "!=", self.country.id)], limit=1
        )
        other_state = self.env["res.country.state"].create(
            {
                "name": "Portal Foreign State",
                "code": "PFS",
                "country_id": other_country.id,
            }
        )
        other_region = self.env["res.region"].create(
            {"name": "Portal Other Region", "state_id": other_state.id}
        )
        same_state_region = self.env["res.region"].create(
            {"name": "Portal Alternate Region", "state_id": self.state.id}
        )
        wrong_region_district = self.env["res.district"].create(
            {
                "name": "Portal Wrong Region District",
                "region_id": same_state_region.id,
                "company_id": self.company.id,
            }
        )
        other_company = self.env["res.company"].create(
            {"name": "Portal Submit Other Company"}
        )
        foreign_district = self.env["res.district"].create(
            {
                "name": "Portal Foreign Company District",
                "region_id": self.region.id,
                "company_id": other_company.id,
            }
        )
        cases = (
            ("country", {"country_code": other_country.code}),
            ("location", {"latitude": "north"}),
            ("location", {"latitude": "91"}),
            ("state", {"state_id": "not-a-state"}),
            ("state", {"state_id": str(other_state.id)}),
            ("region", {"region_id": "not-a-region"}),
            (
                "region",
                {"state_id": str(self.state.id), "region_id": str(other_region.id)},
            ),
            ("building number", {"street": ""}),
            ("building number", {"unit": ""}),
            ("service district", {"district_id": "not-a-district"}),
            ("service district", {"district_id": str(foreign_district.id)}),
            (
                "service district",
                {"district_id": str(wrong_region_district.id)},
            ),
            (
                "state, region, city",
                {
                    "manual_location": True,
                    "state_id": False,
                    "region_id": False,
                    "district_id": False,
                },
            ),
            (
                "service district",
                {"manual_location": "true", "district_id": False},
            ),
        )

        before = self.env["crm.lead"].search_count(
            [("partner_id", "=", self.partner_portal.id)]
        )
        for expected, overrides in cases:
            with self.subTest(expected=expected, overrides=overrides):
                self._assert_submit_error(expected, **overrides)
        self.assertEqual(
            self.env["crm.lead"].search_count(
                [("partner_id", "=", self.partner_portal.id)]
            ),
            before,
        )

    def test_submit_requires_a_company_country(self):
        self.company.country_id = False

        self._assert_submit_error("company country is not configured")

    def test_submit_manual_visit_creates_address_without_coordinates(self):
        self.assertNotIn("mobile", self.partner_portal._fields)
        self.partner_portal.phone = False

        result = self.controller.submit_visit_request(
            **self._mapped_visit_values(
                manual_location="true", latitude=False, longitude=False
            )
        )

        self.assertTrue(result["success"], result.get("error"))
        lead = self.env["crm.lead"].browse(result["lead_id"])
        location_partner = lead.fsm_location_id.partner_id
        self.assertEqual(lead.phone, "")
        self.assertEqual(lead.district_id, self.district)
        self.assertEqual(lead.state_id, self.state)
        self.assertEqual(lead.region_id, self.region)
        self.assertEqual(lead.partner_latitude, 0.0)
        self.assertEqual(lead.partner_longitude, 0.0)
        self.assertEqual(location_partner.partner_latitude, 0.0)
        self.assertEqual(location_partner.partner_longitude, 0.0)
        self.assertEqual(location_partner.city, "Portal City")
        self.assertEqual(location_partner.zip, "12345")

    def test_submit_mapped_location_outside_coverage_flags_staff_notice(self):
        result = self.controller.submit_visit_request(
            **self._mapped_visit_values(district_id=False)
        )

        self.assertTrue(result["success"], result.get("error"))
        lead = self.env["crm.lead"].browse(result["lead_id"])
        sale = self.env["sale.order"].search([("opportunity_id", "=", lead.id)])
        self.assertFalse(lead.district_id)
        self.assertIn("outside automatic coverage", lead.description)
        self.assertEqual(lead.state_id, self.state)
        self.assertEqual(lead.region_id, self.region)
        self.assertFalse(lead.fsm_location_id.partner_id.district_id)
        self.assertEqual(
            result["redirect"],
            f"/my/orders/{sale.id}",
        )

    def test_submit_minimal_mapped_visit_omits_optional_address_fields(self):
        result = self.controller.submit_visit_request(
            **self._mapped_visit_values(
                city=False,
                zip=False,
                state_id=False,
                region_id=False,
                district_id=False,
            )
        )

        self.assertTrue(result["success"], result.get("error"))
        lead = self.env["crm.lead"].browse(result["lead_id"])
        location_partner = lead.fsm_location_id.partner_id
        self.assertEqual(lead.partner_latitude, 30.123456)
        self.assertEqual(lead.partner_longitude, 31.234567)
        self.assertEqual(lead.street, "10 Portal Street")
        self.assertEqual(lead.street2, "Building 4")
        self.assertFalse(lead.city)
        self.assertFalse(lead.zip)
        self.assertFalse(lead.state_id)
        self.assertFalse(lead.region_id)
        self.assertFalse(lead.district_id)
        self.assertEqual(location_partner.country_id, self.country)
        self.assertFalse(location_partner.city)
        self.assertFalse(location_partner.zip)
        self.assertFalse(location_partner.state_id)
        self.assertFalse(location_partner.region_id)
        self.assertFalse(location_partner.district_id)

    def test_submit_manual_region_accepts_a_covered_district(self):
        result = self.controller.submit_visit_request(
            **self._mapped_visit_values(
                manual_location=1,
                latitude=False,
                longitude=False,
                region_id=False,
                manual_region="Customer-entered region",
            )
        )

        self.assertTrue(result["success"], result.get("error"))
        lead = self.env["crm.lead"].browse(result["lead_id"])
        self.assertEqual(lead.district_id, self.district)
        self.assertEqual(lead.region_id, self.region)

    def test_submit_rolls_back_malformed_orm_values_and_handles_generic_errors(self):
        domain = [("partner_id", "=", self.partner_portal.id)]
        before_leads = self.env["crm.lead"].search_count(domain)
        before_sales = self.env["sale.order"].search_count(domain)
        before_children = self.env["res.partner"].search_count(
            [("parent_id", "=", self.partner_portal.id)]
        )

        malformed = self.controller.submit_visit_request(
            **self._mapped_visit_values(zip=b"\xff")
        )

        self.assertFalse(malformed["success"])
        self.assertIn("details are invalid", malformed["error"])
        self.assertEqual(self.env["crm.lead"].search_count(domain), before_leads)
        self.assertEqual(self.env["sale.order"].search_count(domain), before_sales)
        self.assertEqual(
            self.env["res.partner"].search_count(
                [("parent_id", "=", self.partner_portal.id)]
            ),
            before_children,
        )

        with mute_logger("odoo.addons.fieldservice_portal.controllers.visit_portal"):
            generic = self.controller.submit_visit_request(
                **self._mapped_visit_values(country_code=7)
            )
        self.assertFalse(generic["success"])
        self.assertIn("could not be created", generic["error"])

    def test_submit_mapped_visit_creates_real_booking_without_optional_stock(self):
        self.assertNotIn("shipping_address_id", self.env["fsm.location"]._fields)

        result = self.controller.submit_visit_request(**self._mapped_visit_values())

        self.assertTrue(result["success"], result.get("error"))
        lead = self.env["crm.lead"].browse(result["lead_id"]).exists()
        sale = self.env["sale.order"].search([("opportunity_id", "=", lead.id)])
        self.assertTrue(lead)
        self.assertEqual(len(sale), 1)
        self.assertEqual(sale.portal_dayroute_id, self.dayroute)
        self.assertEqual(sale.sale_order_template_id, self.survey_template)
        self.assertEqual(sale.validity_date, self.dayroute.date)
        self.assertEqual(lead.fsm_location_id, sale.fsm_location_id)
        self.assertEqual(lead.fsm_location_id.fsm_route_id, self.route)
        self.assertEqual(lead.district_id, self.district)
        self.assertEqual(lead.partner_latitude, 30.123456)
        self.assertEqual(lead.partner_longitude, 31.234567)
        location_partner = lead.fsm_location_id.partner_id
        self.assertEqual(location_partner.parent_id, self.partner_portal)
        self.assertEqual(location_partner.street, "10 Portal Street")
        self.assertEqual(location_partner.street2, "Building 4")
        self.assertEqual(location_partner.district_id, self.district)
        self.assertEqual(self.partner_portal.street, "Original customer street")

    def test_submit_multiline_template_rejects_route_without_enough_fsm_slots(self):
        template = self._create_multiline_survey_template()
        self.company.visit_sale_order_template_id = template
        self.route.max_order = 1
        self.dayroute._compute_order_count()

        result = self.controller.submit_visit_request(**self._mapped_visit_values())

        self.assertFalse(result["success"])
        self.assertIn("capacity", result["error"].lower())
        self.assertFalse(
            self.env["sale.order"].search(
                [
                    ("partner_id", "=", self.partner_portal.id),
                    ("sale_order_template_id", "=", template.id),
                ]
            )
        )

    def test_submit_mixed_tracking_template_reserves_fallback_sale_order(self):
        line_product = self.env["product.product"].create(
            {
                "name": "Portal Per-Line Survey With Fallback",
                "type": "service",
                "field_service_tracking": "line",
            }
        )
        untracked_service = self.env["product.product"].create(
            {
                "name": "Portal Untracked Survey Fallback",
                "type": "service",
                "field_service_tracking": "no",
            }
        )
        template = self.env["sale.order.template"].create(
            {
                "name": "Portal Mixed Tracking Survey",
                "service_type": "survey",
                "company_id": self.company.id,
                "sale_order_template_line_ids": [
                    Command.create(
                        {
                            "product_id": line_product.id,
                            "product_uom_id": line_product.uom_id.id,
                            "product_uom_qty": 1,
                        }
                    ),
                    Command.create(
                        {
                            "product_id": untracked_service.id,
                            "product_uom_id": untracked_service.uom_id.id,
                            "product_uom_qty": 1,
                        }
                    ),
                ],
            }
        )
        self.company.visit_sale_order_template_id = template
        self.route.max_order = 1
        self.dayroute._compute_order_count()

        result = self.controller.submit_visit_request(**self._mapped_visit_values())

        self.assertFalse(result["success"])
        self.assertIn("capacity", result["error"].lower())
        self.assertFalse(
            self.env["sale.order"].search(
                [
                    ("partner_id", "=", self.partner_portal.id),
                    ("sale_order_template_id", "=", template.id),
                ]
            )
        )

    def test_multiline_draft_reserves_every_future_fsm_order(self):
        template = self._create_multiline_survey_template()
        self.company.visit_sale_order_template_id = template
        self.route.max_order = 3
        self.dayroute._compute_order_count()

        first = self.controller.submit_visit_request(**self._mapped_visit_values())
        available = dict(_booking.dayroutes_with_available_capacity("visit"))[
            self.dayroute
        ]
        second = self.controller.submit_visit_request(**self._mapped_visit_values())

        self.assertTrue(first["success"], first.get("error"))
        self.assertEqual(available, 1)
        self.assertFalse(second["success"])
        self.assertIn("capacity", second["error"].lower())
        self.assertEqual(
            self.env["sale.order"].search_count(
                [
                    ("partner_id", "=", self.partner_portal.id),
                    ("sale_order_template_id", "=", template.id),
                ]
            ),
            1,
        )
