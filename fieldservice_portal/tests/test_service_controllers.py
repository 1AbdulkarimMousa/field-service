from contextlib import contextmanager
from datetime import timedelta
from unittest.mock import patch

from odoo import Command, fields
from odoo.tests.common import tagged
from odoo.tools import mute_logger

from odoo.addons.base.tests.common import TransactionCaseWithUserPortal
from odoo.addons.fieldservice_portal.controllers import (
    _booking,
    fsm_order_portal,
    installation_portal,
    requests_portal,
)
from odoo.addons.http_routing.tests.common import MockRequest


class _ControllerRequest:
    def __init__(self, env):
        self.env = env

    def render(self, template, values):
        return {"response": "render", "template": template, "values": values}

    def redirect(self, location):
        return {"response": "redirect", "location": location}


class _ExplodingEnv:
    def __getattr__(self, name):
        raise RuntimeError(f"Unable to access {name}")


class _MutatingEnv:
    def __init__(self, env, model_name, occurrence, callback):
        self._env = env
        self._model_name = model_name
        self._occurrence = occurrence
        self._callback = callback
        self._access_count = 0

    def __getattr__(self, name):
        return getattr(self._env, name)

    def __getitem__(self, model_name):
        if model_name == self._model_name:
            self._access_count += 1
            if self._access_count == self._occurrence:
                self._callback()
        return self._env[model_name]


@tagged("post_install", "-at_install")
class TestServiceControllers(TransactionCaseWithUserPortal):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env = cls.env(
            context={
                **cls.env.context,
                "mail_create_nolog": True,
                "mail_notrack": True,
                "tracking_disable": True,
            }
        )
        cls.portal_env = cls.env(user=cls.user_portal)
        cls.company = cls.env.company

        cls.maintenance_type = cls.env["fsm.order.type"].create(
            {"name": "Controller Maintenance", "service_type": "maintenance"}
        )
        cls.installation_type = cls.env["fsm.order.type"].create(
            {"name": "Controller Installation", "service_type": "installation"}
        )
        cls.maintenance_product = cls.env["product.product"].create(
            {
                "name": "Controller Maintenance Service",
                "type": "service",
                "field_service_tracking": "sale",
                "list_price": 100.0,
            }
        )
        cls.maintenance_template = cls.env["sale.order.template"].create(
            {
                "name": "Controller Maintenance Template",
                "service_type": "maintenance",
                "company_id": cls.company.id,
            }
        )
        cls.env["sale.order.template.line"].create(
            {
                "sale_order_template_id": cls.maintenance_template.id,
                "product_id": cls.maintenance_product.id,
                "product_uom_id": cls.maintenance_product.uom_id.id,
                "product_uom_qty": 1,
            }
        )
        cls.installation_template = cls.env["sale.order.template"].create(
            {
                "name": "Controller Installation Template",
                "service_type": "installation",
                "company_id": cls.company.id,
            }
        )
        cls.company.write(
            {
                "requests_sale_order_template_id": cls.maintenance_template.id,
                "installation_release_policy": "approval",
            }
        )

        cls.team = cls.env["fsm.team"].create(
            {"name": "Controller Portal Team", "company_id": cls.company.id}
        )
        person_partner = cls.env["res.partner"].create(
            {"name": "Controller Technician", "tz": "UTC"}
        )
        cls.person = cls.env["fsm.person"].create(
            {"partner_id": person_partner.id, "team_id": cls.team.id}
        )
        route_days = cls.env["fsm.route.day"].search([])
        cls.maintenance_route = cls.env["fsm.route"].create(
            {
                "name": "Controller Maintenance Route",
                "route_type": "maintenance",
                "fsm_person_id": cls.person.id,
                "max_order": 10,
                "day_ids": [Command.set(route_days.ids)],
            }
        )
        cls.installation_route = cls.env["fsm.route"].create(
            {
                "name": "Controller Installation Route",
                "route_type": "installation",
                "fsm_person_id": cls.person.id,
                "max_order": 10,
                "day_ids": [Command.set(route_days.ids)],
            }
        )
        cls.route_date = fields.Date.today() + timedelta(days=1)
        cls.maintenance_dayroute = cls.env["fsm.route.dayroute"].create(
            {
                "route_id": cls.maintenance_route.id,
                "team_id": cls.team.id,
                "date": cls.route_date,
            }
        )
        cls.installation_dayroute = cls.env["fsm.route.dayroute"].create(
            {
                "route_id": cls.installation_route.id,
                "team_id": cls.team.id,
                "date": cls.route_date,
            }
        )

    @contextmanager
    def _request_for(self, module, env=None, request_env=None):
        env = env or self.portal_env
        controller_request = _ControllerRequest(request_env or env)
        with (
            MockRequest(env),
            patch.object(module, "request", controller_request),
            patch.object(_booking, "request", controller_request),
        ):
            yield controller_request

    def _create_location(self, name, owner=None, route=None):
        owner = owner or self.partner_portal.commercial_partner_id
        location_partner = self.env["res.partner"].create(
            {"name": f"{name} Address", "parent_id": owner.id}
        )
        values = {
            "partner_id": location_partner.id,
            "owner_id": owner.id,
            "contact_id": self.partner_portal.id,
        }
        if route:
            values["fsm_route_id"] = route.id
        return self.env["fsm.location"].create(values)

    def _install_equipment(self, location, name, company=None):
        return self.env["fsm.equipment"].create(
            {
                "name": name,
                "location_id": location.id,
                "company_id": (company or self.company).id,
            }
        )

    def _create_installation_sale(self, location, partner=None, **values):
        return self.env["sale.order"].create(
            {
                "partner_id": (partner or self.partner_portal).id,
                "sale_order_template_id": self.installation_template.id,
                "fsm_location_id": location.id,
                "company_id": self.company.id,
                "state": "sale",
                **values,
            }
        )

    def _create_dayroute(self, route, days=2, capacity=None):
        if capacity is not None:
            route.max_order = capacity
        return self.env["fsm.route.dayroute"].create(
            {
                "route_id": route.id,
                "team_id": self.team.id,
                "date": fields.Date.today() + timedelta(days=days),
            }
        )

    def _create_fsm_order(self, sale, dayroute=None, **values):
        order_values = {
            "location_id": sale.fsm_location_id.id,
            "sale_id": sale.id,
            "company_id": sale.company_id.id,
            "team_id": self.team.id,
            **values,
        }
        if dayroute:
            order_values.update(
                {
                    "dayroute_id": dayroute.id,
                    "fsm_route_id": dayroute.route_id.id,
                }
            )
        return self.env["fsm.order"].create(order_values)

    def _create_order_stage(self, name, portal_visible):
        return self.env["fsm.stage"].create(
            {
                "name": name,
                "sequence": 70 if portal_visible else 71,
                "stage_type": "order",
                "portal_visible": portal_visible,
                "company_id": self.company.id,
            }
        )

    def _call_http(self, endpoint, controller, *args, **kwargs):
        return endpoint.original_endpoint(controller, *args, **kwargs)

    def test_fsm_order_page_includes_optional_status_messages(self):
        stage = self._create_order_stage(
            "Controller Detail Portal Visible", portal_visible=True
        )
        location = self._create_location("Controller Detail Site")
        order = self.env["fsm.order"].create(
            {
                "name": "Controller Detail Work Order",
                "location_id": location.id,
                "stage_id": stage.id,
                "team_id": self.team.id,
                "company_id": self.company.id,
            }
        )
        controller = fsm_order_portal.CustomerPortal()

        with self._request_for(fsm_order_portal):
            response = self._call_http(
                fsm_order_portal.CustomerPortal.portal_my_fsm_order,
                controller,
                order.id,
                error="Controller error",
                warning="Controller warning",
                success="Controller success",
            )
            plain_response = self._call_http(
                fsm_order_portal.CustomerPortal.portal_my_fsm_order,
                controller,
                order.id,
            )

        self.assertEqual(
            response["template"],
            "fieldservice_portal.portal_fieldservice_order_page",
        )
        values = response["values"]
        self.assertEqual(values["page_name"], "fsm_order")
        self.assertEqual(values["fsm_order"], order)
        self.assertEqual(values["error"], "Controller error")
        self.assertEqual(values["warning"], "Controller warning")
        self.assertEqual(values["success"], "Controller success")
        self.assertNotIn("error", plain_response["values"])
        self.assertNotIn("warning", plain_response["values"])
        self.assertNotIn("success", plain_response["values"])

    def test_fsm_order_list_builds_name_and_all_search_domains(self):
        stage = self._create_order_stage(
            "Controller Search Portal Visible", portal_visible=True
        )
        location = self._create_location("Controller Search Site")
        matching_location = self._create_location("ALL-NEEDLE Controller Site")
        common_values = {
            "stage_id": stage.id,
            "team_id": self.team.id,
            "company_id": self.company.id,
        }
        name_match = self.env["fsm.order"].create(
            {
                **common_values,
                "name": "NAME-NEEDLE Controller Order",
                "description": "No all-search match",
                "location_id": location.id,
            }
        )
        name_decoy = self.env["fsm.order"].create(
            {
                **common_values,
                "name": "Controller Name Decoy",
                "description": "NAME-NEEDLE only in description",
                "location_id": location.id,
            }
        )
        all_name_match = self.env["fsm.order"].create(
            {
                **common_values,
                "name": "ALL-NEEDLE Controller Order",
                "description": "Name field match",
                "location_id": location.id,
            }
        )
        all_description_match = self.env["fsm.order"].create(
            {
                **common_values,
                "name": "Controller Description Match",
                "description": "ALL-NEEDLE in the description",
                "location_id": location.id,
            }
        )
        all_location_match = self.env["fsm.order"].create(
            {
                **common_values,
                "name": "Controller Location Match",
                "description": "Location field match",
                "location_id": matching_location.id,
            }
        )
        controller = fsm_order_portal.CustomerPortal()

        with self._request_for(fsm_order_portal):
            name_response = self._call_http(
                fsm_order_portal.CustomerPortal.portal_my_fsm_orders,
                controller,
                search="NAME-NEEDLE",
                search_in="name",
                groupby="none",
                filterby="all",
                sortby="name",
            )
            all_response = self._call_http(
                fsm_order_portal.CustomerPortal.portal_my_fsm_orders,
                controller,
                search="ALL-NEEDLE",
                search_in="all",
                groupby="none",
                filterby="all",
                sortby="name",
            )

        name_orders = name_response["values"]["grouped_orders"][0]
        all_orders = all_response["values"]["grouped_orders"][0]
        self.assertEqual(name_orders, name_match)
        self.assertNotIn(name_decoy, name_orders)
        self.assertEqual(
            set(all_orders.ids),
            {all_name_match.id, all_description_match.id, all_location_match.id},
        )
        self.assertEqual(name_response["values"]["search_in"], "name")
        self.assertEqual(all_response["values"]["search_in"], "all")

    def test_requests_access_is_recursive_and_equipment_is_company_scoped(self):
        commercial_partner = self.partner_portal.commercial_partner_id
        child = self.env["res.partner"].create(
            {"name": "Controller Child Owner", "parent_id": commercial_partner.id}
        )
        grandchild = self.env["res.partner"].create(
            {"name": "Controller Grandchild Owner", "parent_id": child.id}
        )
        recursive_location = self._create_location(
            "Recursive Controller Site", owner=grandchild
        )
        self._install_equipment(recursive_location, "Recursive Controller Equipment")
        other_company = self.env["res.company"].create(
            {"name": "Controller Other Equipment Company"}
        )
        foreign_equipment_location = self._create_location(
            "Foreign Equipment Controller Site"
        )
        self._install_equipment(
            foreign_equipment_location,
            "Foreign Controller Equipment",
            company=other_company,
        )

        controller = requests_portal.RequestsPortal()
        with self._request_for(requests_portal):
            locations = (
                self.env["fsm.location"]
                .sudo()
                .search(controller._requests_location_domain())
            )
            checked_recursive = controller._requests_check_location(
                recursive_location.id
            )
            checked_foreign = controller._requests_check_location(
                foreign_equipment_location.id
            )

        self.assertIn(recursive_location, locations)
        self.assertEqual(checked_recursive, recursive_location)
        self.assertNotIn(foreign_equipment_location, locations)
        self.assertIsNone(checked_foreign)

    def test_requests_submit_requires_capacity_for_each_generated_order(self):
        line_product = self.env["product.product"].create(
            {
                "name": "Controller Per-Line Maintenance",
                "type": "service",
                "field_service_tracking": "line",
            }
        )
        multi_order_template = self.env["sale.order.template"].create(
            {
                "name": "Controller Multi-Order Maintenance Template",
                "service_type": "maintenance",
                "company_id": self.company.id,
            }
        )
        line_values = {
            "product_id": line_product.id,
            "product_uom_id": line_product.uom_id.id,
            "product_uom_qty": 1,
        }
        self.env["sale.order.template.line"].create(
            [
                {"sale_order_template_id": multi_order_template.id, **line_values},
                {"sale_order_template_id": multi_order_template.id, **line_values},
            ]
        )
        self.company.requests_sale_order_template_id = multi_order_template
        self.maintenance_route.max_order = 1
        self.maintenance_dayroute._compute_order_count()
        location = self._create_location(
            "Capacity Controller Site", route=self.maintenance_route
        )
        self._install_equipment(location, "Capacity Controller Equipment")

        controller = requests_portal.RequestsPortal()
        with self._request_for(requests_portal):
            result = controller.requests_submit(
                location_id=location.id,
                route_id=self.maintenance_dayroute.id,
                description="Needs two work orders",
            )

        self.assertFalse(result["success"])
        self.assertIn("capacity", result["error"].lower())
        self.assertFalse(
            self.env["sale.order"].search(
                [("portal_service_description", "=", "Needs two work orders")]
            )
        )

    def test_installation_submit_uses_the_selected_route(self):
        location = self._create_location(
            "Installation Route Controller Site", route=self.installation_route
        )
        sale = self._create_installation_sale(location)

        controller = installation_portal.InstallationPortal()
        with self._request_for(installation_portal):
            result = controller.installation_submit(
                sale_order_id=sale.id,
                route_id=self.installation_dayroute.id,
            )

        self.assertTrue(result["success"], result)
        order = self.env["fsm.order"].browse(result["order_id"])
        self.assertEqual(order.sale_id, sale)
        self.assertEqual(order.dayroute_id, self.installation_dayroute)
        self.assertEqual(order.fsm_route_id, self.installation_route)
        self.assertEqual(location.fsm_route_id, self.installation_route)

    def test_requests_submit_rejects_mismatched_location_route_without_mutation(self):
        route_days = self.env["fsm.route.day"].search([])
        selected_route = self.env["fsm.route"].create(
            {
                "name": "Controller Mismatched Maintenance Route",
                "route_type": "maintenance",
                "fsm_person_id": self.person.id,
                "max_order": 10,
                "day_ids": [Command.set(route_days.ids)],
            }
        )
        selected_dayroute = self._create_dayroute(selected_route, days=2)
        location = self._create_location(
            "Mismatched Maintenance Controller Site", route=self.maintenance_route
        )
        self._install_equipment(location, "Mismatched Maintenance Controller Equipment")
        historical_order = self.env["fsm.order"].create(
            {
                "name": "Historical Maintenance Controller Order",
                "location_id": location.id,
                "dayroute_id": self.maintenance_dayroute.id,
                "fsm_route_id": self.maintenance_route.id,
                "scheduled_date_start": self.maintenance_dayroute.date_start_planned,
                "team_id": self.team.id,
                "company_id": self.company.id,
            }
        )
        original_dayroute = historical_order.dayroute_id
        description = "Reject mismatched maintenance route"
        controller = requests_portal.RequestsPortal()

        with self._request_for(requests_portal):
            result = controller.requests_submit(
                location_id=location.id,
                route_id=selected_dayroute.id,
                description=description,
            )

        location.invalidate_recordset(["fsm_route_id"])
        historical_order.invalidate_recordset(["fsm_route_id", "dayroute_id"])
        created_quotes = self.env["sale.order"].search_count(
            [("portal_service_description", "=", description)]
        )
        self.assertEqual(
            (
                result["success"],
                location.fsm_route_id,
                historical_order.fsm_route_id,
                historical_order.dayroute_id,
                created_quotes,
            ),
            (
                False,
                self.maintenance_route,
                self.maintenance_route,
                original_dayroute,
                0,
            ),
        )
        self.assertIn("does not match this service location", result["error"])

    def test_installation_submit_rejects_mismatched_route_without_mutation(self):
        route_days = self.env["fsm.route.day"].search([])
        selected_route = self.env["fsm.route"].create(
            {
                "name": "Controller Mismatched Installation Route",
                "route_type": "installation",
                "fsm_person_id": self.person.id,
                "max_order": 10,
                "day_ids": [Command.set(route_days.ids)],
            }
        )
        selected_dayroute = self._create_dayroute(selected_route, days=2)
        location = self._create_location(
            "Mismatched Installation Controller Site", route=self.installation_route
        )
        historical_order = self.env["fsm.order"].create(
            {
                "name": "Historical Installation Controller Order",
                "location_id": location.id,
                "dayroute_id": self.installation_dayroute.id,
                "fsm_route_id": self.installation_route.id,
                "scheduled_date_start": self.installation_dayroute.date_start_planned,
                "team_id": self.team.id,
                "company_id": self.company.id,
            }
        )
        original_dayroute = historical_order.dayroute_id
        sale = self._create_installation_sale(location)
        controller = installation_portal.InstallationPortal()

        with self._request_for(installation_portal):
            result = controller.installation_submit(
                sale_order_id=sale.id,
                route_id=selected_dayroute.id,
            )

        location.invalidate_recordset(["fsm_route_id"])
        historical_order.invalidate_recordset(["fsm_route_id", "dayroute_id"])
        sale.invalidate_recordset(["portal_dayroute_id", "fsm_order_ids"])
        self.assertEqual(
            (
                result["success"],
                location.fsm_route_id,
                historical_order.fsm_route_id,
                historical_order.dayroute_id,
                sale.portal_dayroute_id,
                bool(sale.fsm_order_ids),
            ),
            (
                False,
                self.installation_route,
                self.installation_route,
                original_dayroute,
                self.env["fsm.route.dayroute"],
                False,
            ),
        )
        self.assertIn("does not match this service location", result["error"])

    def test_controller_routes_are_authenticated_portal_contracts(self):
        route_contracts = (
            (
                requests_portal.RequestsPortal.portal_requests,
                "/my/requests",
                "http",
            ),
            (
                requests_portal.RequestsPortal.portal_requests_location,
                "/my/requests/<int:location_id>",
                "http",
            ),
            (
                requests_portal.RequestsPortal.portal_requests_new,
                "/my/requests/<int:location_id>/new",
                "http",
            ),
            (
                requests_portal.RequestsPortal.requests_get_routes,
                "/my/requests/routes",
                "jsonrpc",
            ),
            (
                requests_portal.RequestsPortal.requests_submit,
                "/my/requests/submit",
                "jsonrpc",
            ),
            (
                installation_portal.InstallationPortal.portal_installation,
                "/my/installation/<int:sale_order_id>",
                "http",
            ),
            (
                installation_portal.InstallationPortal.installation_get_routes,
                "/my/installation/routes",
                "jsonrpc",
            ),
            (
                installation_portal.InstallationPortal.installation_submit,
                "/my/installation/submit",
                "jsonrpc",
            ),
        )

        for endpoint, route, request_type in route_contracts:
            routing = endpoint.original_routing
            self.assertEqual(routing["routes"], [route])
            self.assertEqual(routing["type"], request_type)
            self.assertEqual(routing["auth"], "user")
            self.assertTrue(routing["website"])

    def test_requests_home_counter_is_lazy_and_access_scoped(self):
        first = self._create_location("Counter First Controller Site")
        second = self._create_location("Counter Second Controller Site")
        self._install_equipment(first, "Counter First Controller Equipment")
        self._install_equipment(second, "Counter Second Controller Equipment")
        controller = requests_portal.RequestsPortal()

        with self._request_for(requests_portal):
            skipped = controller._prepare_home_portal_values([])
            counted = controller._prepare_home_portal_values(["fsm_location_count"])

        self.assertNotIn("fsm_location_count", skipped)
        self.assertEqual(counted["fsm_location_count"], 2)

    def test_requests_location_check_rejects_missing_unequipped_and_unowned(self):
        unequipped = self._create_location("Unequipped Controller Site")
        outsider = self.env["res.partner"].create({"name": "Controller Outside Owner"})
        unowned = self._create_location("Unowned Controller Site", owner=outsider)
        self._install_equipment(unowned, "Unowned Controller Equipment")
        controller = requests_portal.RequestsPortal()

        with self._request_for(requests_portal):
            missing_result = controller._requests_check_location(0)
            unequipped_result = controller._requests_check_location(unequipped.id)
            unowned_result = controller._requests_check_location(unowned.id)

        self.assertIsNone(missing_result)
        self.assertIsNone(unequipped_result)
        self.assertIsNone(unowned_result)

    def test_requests_page_renders_empty_state(self):
        controller = requests_portal.RequestsPortal()

        with self._request_for(requests_portal):
            response = self._call_http(
                requests_portal.RequestsPortal.portal_requests, controller
            )

        self.assertEqual(response["response"], "render")
        self.assertEqual(
            response["template"],
            "fieldservice_portal.portal_requests_no_location",
        )
        self.assertEqual(response["values"]["page_name"], "requests")

    def test_requests_page_redirects_when_there_is_one_location(self):
        location = self._create_location("Single Controller Site")
        self._install_equipment(location, "Single Controller Equipment")
        controller = requests_portal.RequestsPortal()

        with self._request_for(requests_portal):
            response = self._call_http(
                requests_portal.RequestsPortal.portal_requests, controller
            )

        self.assertEqual(response["response"], "redirect")
        self.assertEqual(response["location"], f"/my/requests/{location.id}")

    def test_requests_page_renders_multiple_locations_in_name_order(self):
        second = self._create_location("Zulu Controller Site")
        first = self._create_location("Alpha Controller Site")
        self._install_equipment(second, "Zulu Controller Equipment")
        self._install_equipment(first, "Alpha Controller Equipment")
        controller = requests_portal.RequestsPortal()

        with self._request_for(requests_portal):
            response = self._call_http(
                requests_portal.RequestsPortal.portal_requests, controller
            )

        self.assertEqual(
            response["template"],
            "fieldservice_portal.portal_requests_location_picker",
        )
        self.assertEqual(response["values"]["locations"], first | second)
        self.assertEqual(response["values"]["page_name"], "requests")

    def test_requests_location_page_filters_visibility_orders_and_limit(self):
        location = self._create_location("Order List Controller Site")
        self._install_equipment(location, "Order List Controller Equipment")
        visible_stage = self._create_order_stage(
            "Controller Portal Visible", portal_visible=True
        )
        hidden_stage = self._create_order_stage(
            "Controller Portal Hidden", portal_visible=False
        )
        now = fields.Datetime.now()
        visible_orders = self.env["fsm.order"]
        for index in range(21):
            visible_orders |= self.env["fsm.order"].create(
                {
                    "name": f"Visible Controller Order {index:02d}",
                    "location_id": location.id,
                    "stage_id": visible_stage.id,
                    "request_early": now + timedelta(hours=index),
                }
            )
        hidden_order = self.env["fsm.order"].create(
            {
                "name": "Hidden Controller Order",
                "location_id": location.id,
                "stage_id": hidden_stage.id,
                "request_early": now + timedelta(days=2),
            }
        )
        other_company = self.env["res.company"].create(
            {"name": "Order List Controller Other Company"}
        )
        other_company_team = (
            self.env["fsm.team"]
            .sudo()
            .with_company(other_company)
            .create(
                {
                    "name": "Order List Controller Other Team",
                    "company_id": other_company.id,
                }
            )
        )
        other_company_stage = (
            self.env["fsm.stage"]
            .sudo()
            .with_company(other_company)
            .create(
                {
                    "name": "Other Company Portal Visible",
                    "stage_type": "order",
                    "portal_visible": True,
                    "company_id": other_company.id,
                }
            )
        )
        other_company_order = (
            self.env["fsm.order"]
            .sudo()
            .with_company(other_company)
            .create(
                {
                    "name": "Other Company Controller Order",
                    "location_id": location.id,
                    "stage_id": other_company_stage.id,
                    "team_id": other_company_team.id,
                    "company_id": other_company.id,
                    "request_early": now + timedelta(days=3),
                }
            )
        )
        controller = requests_portal.RequestsPortal()

        with self._request_for(requests_portal):
            response = self._call_http(
                requests_portal.RequestsPortal.portal_requests_location,
                controller,
                location.id,
            )
            denied = self._call_http(
                requests_portal.RequestsPortal.portal_requests_location,
                controller,
                0,
            )

        orders = response["values"]["orders"]
        self.assertEqual(
            response["template"], "fieldservice_portal.portal_requests_location"
        )
        self.assertEqual(response["values"]["location"], location)
        self.assertEqual(len(orders), 20)
        self.assertEqual(orders[0], visible_orders[-1])
        self.assertNotIn(visible_orders[0], orders)
        self.assertNotIn(hidden_order, orders)
        self.assertNotIn(other_company_order, orders)
        self.assertEqual(denied["location"], "/my/requests")

    def test_requests_new_page_renders_or_redirects_by_access(self):
        location = self._create_location("New Request Controller Site")
        self._install_equipment(location, "New Request Controller Equipment")
        controller = requests_portal.RequestsPortal()

        with self._request_for(requests_portal):
            response = self._call_http(
                requests_portal.RequestsPortal.portal_requests_new,
                controller,
                location.id,
            )
            denied = self._call_http(
                requests_portal.RequestsPortal.portal_requests_new,
                controller,
                0,
            )

        self.assertEqual(
            response["template"], "fieldservice_portal.portal_requests_new"
        )
        self.assertEqual(response["values"]["location"], location)
        self.assertEqual(response["values"]["page_name"], "requests")
        self.assertEqual(denied["location"], "/my/requests")

    def test_requests_routes_return_available_slots_and_empty_result(self):
        location = self._create_location(
            "Route List Controller Site", route=self.maintenance_route
        )
        self._install_equipment(location, "Route List Controller Equipment")
        routeless_location = self._create_location("Routeless Controller Site")
        self._install_equipment(
            routeless_location, "Routeless Controller Equipment"
        )
        other_route = self.env["fsm.route"].create(
            {
                "name": "Other Controller Maintenance Route",
                "route_type": "maintenance",
                "fsm_person_id": self.person.id,
                "max_order": 10,
                "day_ids": [Command.set(self.env["fsm.route.day"].search([]).ids)],
            }
        )
        self._create_dayroute(other_route)
        controller = requests_portal.RequestsPortal()

        with self._request_for(requests_portal):
            available = controller.requests_get_routes(location_id=location.id)
            denied = controller.requests_get_routes(location_id=0)
            malformed = controller.requests_get_routes(location_id="not-an-id")
            overflow = controller.requests_get_routes(location_id=float("inf"))
            routeless = controller.requests_get_routes(
                location_id=routeless_location.id
            )
            self.maintenance_route.max_order = 0
            self.maintenance_dayroute._compute_order_count()
            empty = controller.requests_get_routes(location_id=location.id)

        self.assertTrue(available["success"])
        self.assertEqual(
            available["routes"],
            [
                {
                    "id": self.maintenance_dayroute.id,
                    "date": str(self.maintenance_dayroute.date),
                    "route_name": self.maintenance_route.name,
                    "person": self.person.name,
                    "remaining": 10,
                }
            ],
        )
        self.assertEqual(empty, {"success": True, "routes": []})
        self.assertFalse(denied["success"])
        self.assertIn("Access denied", denied["error"])
        self.assertFalse(malformed["success"])
        self.assertIn("Access denied", malformed["error"])
        self.assertFalse(overflow["success"])
        self.assertIn("Access denied", overflow["error"])
        self.assertEqual(routeless, {"success": True, "routes": []})

    @mute_logger(requests_portal._logger.name)
    def test_requests_routes_handle_generic_failure(self):
        controller = requests_portal.RequestsPortal()

        with self._request_for(requests_portal, request_env=_ExplodingEnv()):
            result = controller.requests_get_routes()

        self.assertFalse(result["success"])
        self.assertIn("temporarily unavailable", result["error"])

    def test_requests_submit_rejects_missing_malformed_and_denied_locations(self):
        denied_location = self._create_location("Denied Submit Controller Site")
        controller = requests_portal.RequestsPortal()

        with self._request_for(requests_portal):
            missing = controller.requests_submit()
            malformed = controller.requests_submit(location_id="not-an-id")
            denied = controller.requests_submit(location_id=denied_location.id)

        self.assertFalse(missing["success"])
        self.assertIn("No location", missing["error"])
        self.assertFalse(malformed["success"])
        self.assertIn("invalid", malformed["error"])
        self.assertFalse(denied["success"])
        self.assertIn("Access denied", denied["error"])

    def test_requests_submit_reports_configuration_and_slot_validation(self):
        location = self._create_location(
            "Validation Submit Controller Site", route=self.maintenance_route
        )
        self._install_equipment(location, "Validation Submit Controller Equipment")
        controller = requests_portal.RequestsPortal()

        with self._request_for(requests_portal):
            self.company.requests_sale_order_template_id = False
            unconfigured = controller.requests_submit(
                location_id=location.id,
                route_id=self.maintenance_dayroute.id,
            )
            self.company.requests_sale_order_template_id = self.maintenance_template
            invalid_slot = controller.requests_submit(
                location_id=location.id,
                route_id=self.installation_dayroute.id,
            )

        self.assertFalse(unconfigured["success"])
        self.assertIn("configured", unconfigured["error"])
        self.assertFalse(invalid_slot["success"])
        self.assertIn("no longer available", invalid_slot["error"])

    def test_requests_submit_reports_missing_maintenance_type(self):
        location = self._create_location(
            "Missing Type Controller Site", route=self.maintenance_route
        )
        self._install_equipment(location, "Missing Type Controller Equipment")
        self.maintenance_type.unlink()
        controller = requests_portal.RequestsPortal()

        with self._request_for(requests_portal):
            result = controller.requests_submit(
                location_id=location.id,
                route_id=self.maintenance_dayroute.id,
            )

        self.assertFalse(result["success"])
        self.assertIn("configured", result["error"])

    def test_requests_submit_creates_maintenance_quotation(self):
        location = self._create_location(
            "Success Submit Controller Site", route=self.maintenance_route
        )
        self._install_equipment(location, "Success Submit Controller Equipment")
        controller = requests_portal.RequestsPortal()

        with self._request_for(requests_portal):
            result = controller.requests_submit(
                location_id=location.id,
                route_id=self.maintenance_dayroute.id,
                description="Controller customer description",
            )

        self.assertTrue(result["success"], result)
        sale = self.env["sale.order"].browse(result["sale_order_id"])
        self.assertEqual(sale.partner_id, self.partner_portal)
        self.assertEqual(sale.sale_order_template_id, self.maintenance_template)
        self.assertEqual(sale.fsm_location_id, location)
        self.assertEqual(sale.portal_dayroute_id, self.maintenance_dayroute)
        self.assertEqual(
            sale.portal_service_description, "Controller customer description"
        )
        self.assertEqual(sale.validity_date, self.maintenance_dayroute.date)
        self.assertEqual(sale.company_id, self.company)
        self.assertEqual(len(sale.order_line), 1)
        self.assertEqual(result["redirect"], f"/my/orders/{sale.id}")

    def test_requests_capacity_counts_untracked_portal_service_order(self):
        line_product = self.env["product.product"].create(
            {
                "name": "Controller Capacity Line Service",
                "type": "service",
                "field_service_tracking": "line",
            }
        )
        plain_service = self.env["product.product"].create(
            {
                "name": "Controller Capacity Plain Service",
                "type": "service",
                "field_service_tracking": "no",
            }
        )
        template = self.env["sale.order.template"].create(
            {
                "name": "Controller Mixed Capacity Template",
                "service_type": "maintenance",
                "company_id": self.company.id,
            }
        )
        self.env["sale.order.template.line"].create(
            [
                {
                    "sale_order_template_id": template.id,
                    "product_id": line_product.id,
                    "product_uom_id": line_product.uom_id.id,
                    "product_uom_qty": 1,
                },
                {
                    "sale_order_template_id": template.id,
                    "product_id": plain_service.id,
                    "product_uom_id": plain_service.uom_id.id,
                    "product_uom_qty": 1,
                },
            ]
        )
        self.company.requests_sale_order_template_id = template
        self.maintenance_route.max_order = 1
        self.maintenance_dayroute._compute_order_count()
        location = self._create_location(
            "Mixed Capacity Controller Site", route=self.maintenance_route
        )
        self._install_equipment(location, "Mixed Capacity Controller Equipment")
        controller = requests_portal.RequestsPortal()

        with self._request_for(requests_portal):
            result = controller.requests_submit(
                location_id=location.id,
                route_id=self.maintenance_dayroute.id,
            )

        self.assertFalse(result["success"])
        self.assertIn("capacity", result["error"])

    @mute_logger(requests_portal._logger.name)
    def test_requests_submit_rolls_back_on_generic_failure(self):
        location = self._create_location(
            "Rollback Submit Controller Site", route=self.maintenance_route
        )
        self._install_equipment(location, "Rollback Submit Controller Equipment")
        description = "Controller rollback quotation"
        controller = requests_portal.RequestsPortal()

        with (
            patch.object(
                type(self.env["sale.order"]),
                "_onchange_sale_order_template_id",
                autospec=True,
                side_effect=RuntimeError("Force quotation rollback"),
            ),
            self._request_for(requests_portal),
        ):
            result = controller.requests_submit(
                location_id=location.id,
                route_id=self.maintenance_dayroute.id,
                description=description,
            )

        self.assertFalse(result["success"])
        self.assertIn("could not be created", result["error"])
        self.assertFalse(
            self.env["sale.order"].search(
                [("portal_service_description", "=", description)]
            )
        )

    def test_installation_sale_access_checks_owner_company_and_readiness(self):
        controller = installation_portal.InstallationPortal()
        location = self._create_location("Installation Access Controller Site")
        outsider = self.env["res.partner"].create(
            {"name": "Installation Controller Outsider"}
        )
        outsider_sale = self._create_installation_sale(location, partner=outsider)
        draft_sale = self._create_installation_sale(location, state="draft")
        wrong_type_sale = self.env["sale.order"].create(
            {
                "partner_id": self.partner_portal.id,
                "sale_order_template_id": self.maintenance_template.id,
                "fsm_location_id": location.id,
                "company_id": self.company.id,
                "state": "sale",
            }
        )
        missing_location_sale = self.env["sale.order"].create(
            {
                "partner_id": self.partner_portal.id,
                "sale_order_template_id": self.installation_template.id,
                "company_id": self.company.id,
                "state": "sale",
            }
        )
        other_company = self.env["res.company"].create(
            {
                "name": "Installation Controller Other Company",
                "installation_release_policy": "approval",
            }
        )
        other_template = (
            self.env["sale.order.template"]
            .sudo()
            .create(
                {
                    "name": "Other Company Installation Controller Template",
                    "service_type": "installation",
                    "company_id": other_company.id,
                }
            )
        )
        other_company_sale = (
            self.env["sale.order"]
            .sudo()
            .with_company(other_company)
            .create(
                {
                    "partner_id": self.partner_portal.id,
                    "sale_order_template_id": other_template.id,
                    "fsm_location_id": location.id,
                    "company_id": other_company.id,
                    "state": "sale",
                }
            )
        )
        unreleased_sale = self._create_installation_sale(location)
        ready_sale = self._create_installation_sale(location)

        with self._request_for(installation_portal):
            missing = controller._installation_check_sale_order(0)
            outsider_result = controller._installation_check_sale_order(
                outsider_sale.id
            )
            company_result = controller._installation_check_sale_order(
                other_company_sale.id
            )
            draft_without_requirement = controller._installation_check_sale_order(
                draft_sale.id
            )
            draft_ready = controller._installation_check_sale_order(
                draft_sale.id, require_ready=True
            )
            wrong_type = controller._installation_check_sale_order(
                wrong_type_sale.id, require_ready=True
            )
            missing_location = controller._installation_check_sale_order(
                missing_location_sale.id, require_ready=True
            )
            self.company.installation_release_policy = "payment"
            unreleased = controller._installation_check_sale_order(
                unreleased_sale.id, require_ready=True
            )
            self.company.installation_release_policy = "approval"
            ready = controller._installation_check_sale_order(
                ready_sale.id, require_ready=True
            )

        self.assertIsNone(missing)
        self.assertIsNone(outsider_result)
        self.assertIsNone(company_result)
        self.assertEqual(draft_without_requirement, draft_sale)
        self.assertIsNone(draft_ready)
        self.assertIsNone(wrong_type)
        self.assertIsNone(missing_location)
        self.assertIsNone(unreleased)
        self.assertEqual(ready, ready_sale)

    def test_installation_page_renders_ready_sale_and_redirects_denied_sale(self):
        location = self._create_location("Installation Page Controller Site")
        sale = self._create_installation_sale(location)
        controller = installation_portal.InstallationPortal()

        with self._request_for(installation_portal):
            response = self._call_http(
                installation_portal.InstallationPortal.portal_installation,
                controller,
                sale.id,
            )
            denied = self._call_http(
                installation_portal.InstallationPortal.portal_installation,
                controller,
                0,
            )

        self.assertEqual(
            response["template"],
            "fieldservice_portal.portal_installation_slot_picker",
        )
        self.assertEqual(response["values"]["so"], sale)
        self.assertEqual(response["values"]["location"], location)
        self.assertEqual(response["values"]["page_name"], "installation")
        self.assertEqual(denied["location"], "/my")

    def test_installation_routes_return_available_slots_and_empty_result(self):
        location = self._create_location(
            "Route List Installation Controller Site", route=self.installation_route
        )
        sale = self._create_installation_sale(location)
        routeless_sale = self._create_installation_sale(
            self._create_location("Routeless Installation Controller Site")
        )
        other_route = self.env["fsm.route"].create(
            {
                "name": "Other Controller Installation Route",
                "route_type": "installation",
                "fsm_person_id": self.person.id,
                "max_order": 10,
                "day_ids": [Command.set(self.env["fsm.route.day"].search([]).ids)],
            }
        )
        self._create_dayroute(other_route)
        controller = installation_portal.InstallationPortal()

        with self._request_for(installation_portal):
            available = controller.installation_get_routes(sale_order_id=sale.id)
            denied = controller.installation_get_routes(sale_order_id=0)
            malformed = controller.installation_get_routes(sale_order_id="not-an-id")
            overflow = controller.installation_get_routes(sale_order_id=float("inf"))
            routeless = controller.installation_get_routes(
                sale_order_id=routeless_sale.id
            )
            self.installation_route.max_order = 0
            self.installation_dayroute._compute_order_count()
            empty = controller.installation_get_routes(sale_order_id=sale.id)

        self.assertTrue(available["success"])
        self.assertEqual(
            available["routes"],
            [
                {
                    "id": self.installation_dayroute.id,
                    "date": str(self.installation_dayroute.date),
                    "route_name": self.installation_route.name,
                    "person": self.person.name,
                    "remaining": 10,
                }
            ],
        )
        self.assertEqual(empty, {"success": True, "routes": []})
        self.assertFalse(denied["success"])
        self.assertIn("not ready", denied["error"])
        self.assertFalse(malformed["success"])
        self.assertIn("not ready", malformed["error"])
        self.assertFalse(overflow["success"])
        self.assertIn("not ready", overflow["error"])
        self.assertEqual(routeless, {"success": True, "routes": []})

    @mute_logger(installation_portal._logger.name)
    def test_installation_routes_handle_generic_failure(self):
        controller = installation_portal.InstallationPortal()

        with self._request_for(installation_portal, request_env=_ExplodingEnv()):
            result = controller.installation_get_routes()

        self.assertFalse(result["success"])
        self.assertIn("temporarily unavailable", result["error"])

    def test_installation_submit_rejects_missing_malformed_and_unready_sale(self):
        location = self._create_location(
            "Unready Installation Controller Site", route=self.installation_route
        )
        draft_sale = self._create_installation_sale(location, state="draft")
        controller = installation_portal.InstallationPortal()

        with self._request_for(installation_portal):
            missing = controller.installation_submit()
            malformed = controller.installation_submit(sale_order_id="not-an-id")
            unready = controller.installation_submit(sale_order_id=draft_sale.id)

        self.assertFalse(missing["success"])
        self.assertIn("No sale order", missing["error"])
        self.assertFalse(malformed["success"])
        self.assertIn("invalid", malformed["error"])
        self.assertFalse(unready["success"])
        self.assertIn("not ready", unready["error"])

    def test_installation_submit_rechecks_readiness_after_lock(self):
        location = self._create_location(
            "Relock Installation Controller Site", route=self.installation_route
        )
        sale = self._create_installation_sale(location)
        controller = installation_portal.InstallationPortal()

        def cancel_sale():
            sale.state = "cancel"

        request_env = _MutatingEnv(
            self.portal_env,
            "sale.order",
            occurrence=2,
            callback=cancel_sale,
        )
        with self._request_for(
            installation_portal,
            request_env=request_env,
        ):
            result = controller.installation_submit(
                sale_order_id=sale.id,
                route_id=self.installation_dayroute.id,
            )

        self.assertFalse(result["success"])
        self.assertIn("not ready", result["error"])
        self.assertEqual(sale.state, "cancel")

    def test_installation_submit_invalidates_sale_cache_after_lock(self):
        location = self._create_location(
            "Refresh Installation Controller Site", route=self.installation_route
        )
        sale = self._create_installation_sale(location)
        controller = installation_portal.InstallationPortal()
        events = []
        sale_model = type(sale)
        original_lock = sale_model.lock_for_update
        original_invalidate = sale_model.invalidate_recordset

        def tracked_lock(records, **kwargs):
            if records == sale:
                events.append("lock")
            return original_lock(records, **kwargs)

        def tracked_invalidate(records, fnames=None, flush=True):
            if records == sale and fnames is None:
                events.append("invalidate")
            return original_invalidate(records, fnames, flush=flush)

        with (
            patch.object(
                sale_model,
                "lock_for_update",
                autospec=True,
                side_effect=tracked_lock,
            ),
            patch.object(
                sale_model,
                "invalidate_recordset",
                autospec=True,
                side_effect=tracked_invalidate,
            ),
            self._request_for(installation_portal),
        ):
            result = controller.installation_submit(
                sale_order_id=sale.id,
                route_id=self.installation_dayroute.id,
            )

        self.assertTrue(result["success"], result)
        self.assertIn("invalidate", events)
        self.assertLess(events.index("lock"), events.index("invalidate"))

    def test_installation_submit_reports_missing_type(self):
        location = self._create_location(
            "Validation Installation Controller Site", route=self.installation_route
        )
        sale_without_type = self._create_installation_sale(location)
        self.installation_type.unlink()
        controller = installation_portal.InstallationPortal()

        with self._request_for(installation_portal):
            missing_type = controller.installation_submit(
                sale_order_id=sale_without_type.id,
                route_id=self.installation_dayroute.id,
            )

        self.assertFalse(missing_type["success"])
        self.assertIn("configured", missing_type["error"])

    def test_installation_submit_rejects_invalid_slot_and_insufficient_capacity(self):
        invalid_location = self._create_location(
            "Invalid Slot Installation Controller Site", route=self.installation_route
        )
        invalid_sale = self._create_installation_sale(invalid_location)
        capacity_location = self._create_location(
            "Capacity Installation Controller Site", route=self.installation_route
        )
        capacity_sale = self._create_installation_sale(capacity_location)
        capacity_orders = self._create_fsm_order(
            capacity_sale
        ) | self._create_fsm_order(capacity_sale)
        self.installation_route.max_order = 1
        self.installation_dayroute._compute_order_count()
        controller = installation_portal.InstallationPortal()

        with self._request_for(installation_portal):
            invalid_slot = controller.installation_submit(
                sale_order_id=invalid_sale.id,
                route_id=self.maintenance_dayroute.id,
            )
            insufficient = controller.installation_submit(
                sale_order_id=capacity_sale.id,
                route_id=self.installation_dayroute.id,
            )

        self.assertFalse(invalid_slot["success"])
        self.assertIn("no longer available", invalid_slot["error"])
        self.assertFalse(insufficient["success"])
        self.assertIn("capacity", insufficient["error"])
        self.assertFalse(capacity_orders.mapped("dayroute_id"))

    def test_installation_submit_rejects_partly_scheduled_orders(self):
        location = self._create_location(
            "Partial Installation Controller Site", route=self.installation_route
        )
        sale = self._create_installation_sale(location)
        scheduled = self._create_fsm_order(sale, self.installation_dayroute)
        unscheduled = self._create_fsm_order(sale)
        controller = installation_portal.InstallationPortal()

        with self._request_for(installation_portal):
            result = controller.installation_submit(
                sale_order_id=sale.id,
                route_id=self.installation_dayroute.id,
            )

        self.assertFalse(result["success"])
        self.assertIn("partly scheduled", result["error"])
        self.assertEqual(scheduled.dayroute_id, self.installation_dayroute)
        self.assertFalse(unscheduled.dayroute_id)

    def test_installation_submit_rejects_conflicting_appointments(self):
        location = self._create_location(
            "Conflict Installation Controller Site", route=self.installation_route
        )
        sale = self._create_installation_sale(location)
        other_dayroute = self._create_dayroute(self.installation_route, days=3)
        first = self._create_fsm_order(sale, self.installation_dayroute)
        second = self._create_fsm_order(sale, other_dayroute)
        controller = installation_portal.InstallationPortal()

        with self._request_for(installation_portal):
            result = controller.installation_submit(
                sale_order_id=sale.id,
                route_id=self.installation_dayroute.id,
            )

        self.assertFalse(result["success"])
        self.assertIn("conflicting", result["error"])
        self.assertNotEqual(first.dayroute_id, second.dayroute_id)

    def test_installation_submit_is_idempotent_for_scheduled_orders(self):
        location = self._create_location(
            "Idempotent Installation Controller Site", route=self.installation_route
        )
        sale = self._create_installation_sale(location)
        orders = self._create_fsm_order(
            sale, self.installation_dayroute
        ) | self._create_fsm_order(sale, self.installation_dayroute)
        controller = installation_portal.InstallationPortal()

        with self._request_for(installation_portal):
            result = controller.installation_submit(
                sale_order_id=sale.id,
                route_id=0,
            )

        self.assertTrue(result["success"])
        self.assertEqual(result["order_id"], orders.sorted("id")[0].id)
        self.assertEqual(orders.mapped("dayroute_id"), self.installation_dayroute)
        self.assertEqual(result["redirect"], f"/my/orders/{sale.id}")

    def test_installation_submit_updates_all_existing_orders(self):
        location = self._create_location(
            "Update Installation Controller Site", route=self.installation_route
        )
        opportunity = self.env["crm.lead"].create(
            {
                "name": "Controller Installation Opportunity",
                "partner_id": self.partner_portal.id,
                "type": "opportunity",
            }
        )
        sale = self._create_installation_sale(location, opportunity_id=opportunity.id)
        orders = self._create_fsm_order(sale) | self._create_fsm_order(sale)
        controller = installation_portal.InstallationPortal()

        with self._request_for(installation_portal):
            result = controller.installation_submit(
                sale_order_id=sale.id,
                route_id=self.installation_dayroute.id,
            )

        self.assertTrue(result["success"], result)
        self.assertEqual(result["order_id"], orders.sorted("id")[0].id)
        self.assertEqual(orders.mapped("dayroute_id"), self.installation_dayroute)
        self.assertEqual(orders.mapped("person_id"), self.person)
        self.assertEqual(orders.mapped("team_id"), self.team)
        self.assertEqual(orders.mapped("type"), self.installation_type)
        self.assertEqual(orders.mapped("opportunity_id"), opportunity)
        self.assertTrue(
            all(
                order.request_early == self.installation_dayroute.date_start_planned
                for order in orders
            )
        )
        self.assertTrue(
            all(
                order.scheduled_date_start
                == self.installation_dayroute.date_start_planned
                for order in orders
            )
        )
        self.assertEqual(sale.portal_dayroute_id, self.installation_dayroute)

    @mute_logger(installation_portal._logger.name)
    def test_installation_submit_rolls_back_created_order_on_generic_failure(self):
        location = self._create_location(
            "Rollback Installation Controller Site", route=self.installation_route
        )
        sale = self._create_installation_sale(location)
        controller = installation_portal.InstallationPortal()

        with (
            patch.object(
                type(self.env["fsm.order"]),
                "create",
                autospec=True,
                side_effect=RuntimeError("Force installation rollback"),
            ),
            self._request_for(installation_portal),
        ):
            result = controller.installation_submit(
                sale_order_id=sale.id,
                route_id=self.installation_dayroute.id,
            )

        sale.invalidate_recordset(["portal_dayroute_id", "fsm_order_ids"])
        self.assertFalse(result["success"])
        self.assertIn("could not be saved", result["error"])
        self.assertFalse(sale.portal_dayroute_id)
        self.assertEqual(location.fsm_route_id, self.installation_route)
        self.assertFalse(self.env["fsm.order"].search([("sale_id", "=", sale.id)]))
