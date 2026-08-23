from datetime import timedelta
from unittest.mock import patch

from odoo import Command, fields
from odoo.exceptions import ValidationError
from odoo.tests.common import TransactionCase, tagged
from odoo.tools import mute_logger


@tagged("post_install", "-at_install")
class TestPortalModels(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls.env.company
        cls.company.email = "notifications@example.com"
        cls.partner = cls.env["res.partner"].create(
            {
                "name": "Portal Model Customer",
                "email": "customer@example.com",
            }
        )
        cls.partner_without_email = cls.env["res.partner"].create(
            {"name": "Customer Without Email"}
        )
        location_partner = cls.env["res.partner"].create(
            {
                "name": "Portal Model Site",
                "parent_id": cls.partner.id,
            }
        )
        cls.location = cls.env["fsm.location"].create(
            {
                "partner_id": location_partner.id,
                "owner_id": cls.partner.id,
                "contact_id": cls.partner.id,
            }
        )
        cls.team = cls.env["fsm.team"].create(
            {
                "name": "Portal Model Team",
                "company_id": cls.company.id,
            }
        )
        technician_partner = cls.env["res.partner"].create(
            {"name": "Morgan Technician"}
        )
        cls.technician = cls.env["fsm.person"].create(
            {
                "partner_id": technician_partner.id,
                "team_id": cls.team.id,
            }
        )

        last_sequence = max(cls.env["fsm.stage"].search([]).mapped("sequence") or [0])
        cls.started_stage = cls.env["fsm.stage"].create(
            {
                "name": "Portal Work Started",
                "sequence": last_sequence + 101,
                "stage_type": "order",
                "notification_event": "started",
                "company_id": cls.company.id,
            }
        )
        cls.completed_stage = cls.env["fsm.stage"].create(
            {
                "name": "Portal Work Completed",
                "sequence": last_sequence + 102,
                "stage_type": "order",
                "notification_event": "completed",
                "company_id": cls.company.id,
            }
        )
        cls.neutral_stage = cls.env["fsm.stage"].create(
            {
                "name": "Portal Work Pending",
                "sequence": last_sequence + 103,
                "stage_type": "order",
                "notification_event": "none",
                "company_id": cls.company.id,
            }
        )

        cls.order_types = {}
        cls.sale_templates = {}
        for service_type in (
            "survey",
            "installation",
            "maintenance",
            "repair",
            "other",
        ):
            cls.order_types[service_type] = cls.env["fsm.order.type"].create(
                {
                    "name": f"Portal {service_type.title()}",
                    "service_type": service_type,
                }
            )
            cls.sale_templates[service_type] = cls.env["sale.order.template"].create(
                {
                    "name": f"Portal {service_type.title()} Template",
                    "service_type": service_type,
                    "company_id": cls.company.id,
                }
            )

        cls.service_product = cls.env["product.product"].create(
            {
                "name": "Portal General Service",
                "type": "service",
                "field_service_tracking": "no",
                "list_price": 100.0,
            }
        )
        cls.line_service_product = cls.env["product.product"].create(
            {
                "name": "Portal Line Service",
                "type": "service",
                "field_service_tracking": "line",
                "list_price": 25.0,
            }
        )
        cls.sale_service_product = cls.env["product.product"].create(
            {
                "name": "Portal Sale Service",
                "type": "service",
                "field_service_tracking": "sale",
                "list_price": 75.0,
            }
        )

        all_days = cls.env["fsm.route.day"].search([])
        cls.routes = {}
        cls.dayroutes = {}
        route_types = {
            "survey": "visit",
            "maintenance": "maintenance",
            "installation": "installation",
        }
        today = fields.Date.context_today(cls.env.user)
        for offset, (service_type, route_type) in enumerate(
            route_types.items(), start=1
        ):
            route = cls.env["fsm.route"].create(
                {
                    "name": f"Portal {service_type.title()} Route",
                    "route_type": route_type,
                    "fsm_person_id": cls.technician.id,
                    "max_order": 5,
                    "day_ids": [Command.set(all_days.ids)],
                }
            )
            cls.routes[service_type] = route
            cls.dayroutes[service_type] = cls.env["fsm.route.dayroute"].create(
                {
                    "route_id": route.id,
                    "team_id": cls.team.id,
                    "date": today + timedelta(days=offset),
                }
            )

        redirect_form = cls.env["ir.ui.view"].create(
            {
                "name": "Portal Model Dummy Payment Form",
                "type": "qweb",
                "arch": '<form action="dummy" method="post"/>',
            }
        )
        cls.payment_method = cls.env.ref("payment.payment_method_unknown")
        cls.payment_provider = cls.env["payment.provider"].create(
            {
                "name": "Portal Model Dummy Provider",
                "code": "none",
                "state": "test",
                "is_published": True,
                "payment_method_ids": [Command.set(cls.payment_method.ids)],
                "redirect_form_view_id": redirect_form.id,
            }
        )
        cls.payment_method.active = True
        cls.notifier = cls.env["fieldservice.notification"]

    @staticmethod
    def _external_success(_notifier, partner, message):
        return {
            "success": True,
            "partner_id": partner.id,
            "message": message,
        }

    def _create_sale(
        self,
        service_type="other",
        *,
        state="draft",
        amount=100.0,
        product=None,
        dayroute=None,
        description=False,
        partner=None,
        **values,
    ):
        target_state = state
        order_values = {
            "partner_id": (partner or self.partner).id,
            "company_id": self.company.id,
            "fsm_location_id": self.location.id,
            "sale_order_template_id": self.sale_templates[service_type].id,
            "state": "draft",
            **values,
        }
        if dayroute:
            self.location.fsm_route_id = dayroute.route_id
            order_values["portal_dayroute_id"] = dayroute.id
        if description:
            order_values["portal_service_description"] = description
        if amount is not None:
            order_values["order_line"] = [
                Command.create(
                    {
                        "product_id": (product or self.service_product).id,
                        "product_uom_qty": 1,
                        "price_unit": amount,
                    }
                )
            ]
        order = self.env["sale.order"].create(order_values)
        if target_state != "draft":
            mail_template = self.env["mail.template"]
            mail_model = self.env["mail.mail"]
            with (
                patch.object(
                    type(mail_template), "send_mail", autospec=True, return_value=101
                ),
                patch.object(
                    type(mail_model), "send", autospec=True, return_value=True
                ),
            ):
                order.write({"state": target_state})
        return order

    def _create_fsm(
        self,
        service_type="other",
        *,
        sale=None,
        person=None,
        order_type=True,
        **values,
    ):
        order_values = {
            "name": f"FSM {service_type.title()}",
            "location_id": self.location.id,
            "team_id": self.team.id,
            "stage_id": self.neutral_stage.id,
            **values,
        }
        if order_type:
            order_values["type"] = self.order_types[service_type].id
        if sale:
            order_values["sale_id"] = sale.id
        if person:
            order_values["person_id"] = person.id
        return self.env["fsm.order"].create(order_values)

    def _mark_paid(self, order):
        transaction = (
            self.env["payment.transaction"]
            .sudo()
            .create(
                {
                    "payment_method_id": self.payment_method.id,
                    "amount": order.amount_total,
                    "currency_id": order.currency_id.id,
                    "provider_id": self.payment_provider.id,
                    "reference": f"Portal model payment {order.id}",
                    "operation": "online_direct",
                    "partner_id": order.partner_id.id,
                    "state": "done",
                }
            )
        )
        order.sudo().write({"transaction_ids": [Command.link(transaction.id)]})
        order.invalidate_recordset(["amount_paid"])
        self.assertTrue(order._is_paid())
        return transaction

    def test_notification_rejects_missing_partner_and_skips_empty_channels(self):
        self.assertEqual(
            self.notifier.notify_customer(False, "Message"),
            {"success": False, "error": "No partner provided"},
        )
        self.assertEqual(
            self.notifier.notify_customer(
                self.partner_without_email,
                False,
                email_template_xmlid="fieldservice_portal.email_visit_booked",
            ),
            {},
        )
        self.assertEqual(
            self.notifier.notify_customer(self.partner, "No configured transport"),
            {},
        )

    def test_notification_records_external_success_and_failure(self):
        with patch.object(
            type(self.notifier),
            "_send_external_message",
            autospec=True,
            side_effect=self._external_success,
        ) as external_send:
            result = self.notifier.notify_customer(self.partner, "External message")

        self.assertEqual(
            result["external_message"],
            {
                "success": True,
                "partner_id": self.partner.id,
                "message": "External message",
            },
        )
        external_send.assert_called_once()

        with (
            patch.object(
                type(self.notifier),
                "_send_external_message",
                autospec=True,
                side_effect=RuntimeError("external gateway unavailable"),
            ),
            mute_logger("odoo.addons.fieldservice_portal.models.notification"),
        ):
            result = self.notifier.notify_customer(self.partner, "External failure")

        self.assertEqual(
            result["external_message"],
            {"success": False, "error": "external gateway unavailable"},
        )

    def test_notification_sends_template_with_email_context(self):
        mail_template = self.env["mail.template"]
        with patch.object(
            type(mail_template), "send_mail", autospec=True, return_value=101
        ) as template_send:
            result = self.notifier.notify_customer(
                self.partner,
                False,
                email_template_xmlid="fieldservice_portal.email_visit_booked",
                email_values={"portal_url": "https://example.test/my/orders"},
            )

        self.assertEqual(result["email"], {"success": True})
        template_send.assert_called_once()
        template_record, partner_id = template_send.call_args.args[:2]
        self.assertEqual(partner_id, self.partner.id)
        self.assertEqual(
            template_record.env.context["default_email_to"], self.partner.email
        )
        self.assertEqual(
            template_record.env.context["portal_url"],
            "https://example.test/my/orders",
        )
        self.assertTrue(template_send.call_args.kwargs["force_send"])
        self.assertEqual(
            template_send.call_args.kwargs["email_layout_xmlid"],
            "mail.mail_notification_light",
        )

    def test_notification_reports_missing_template(self):
        result = self.notifier.notify_customer(
            self.partner,
            False,
            email_template_xmlid="fieldservice_portal.missing_email_template",
        )
        self.assertEqual(
            result["email"],
            {"success": False, "error": "Template not found"},
        )

    def test_notification_creates_escaped_direct_email(self):
        mail_model = self.env["mail.mail"]
        with patch.object(
            type(mail_model), "send", autospec=True, return_value=True
        ) as mail_send:
            result = self.notifier.notify_customer(
                self.partner,
                False,
                email_subject="Direct field service update",
                email_body="Unsafe <script>alert('x')</script>",
            )

        self.assertEqual(result["email"], {"success": True})
        mail_send.assert_called_once()
        mail = mail_send.call_args.args[0]
        self.assertEqual(mail.subject, "Direct field service update")
        self.assertEqual(mail.email_to, self.partner.email_formatted)
        self.assertEqual(mail.email_from, self.company.email_formatted)
        self.assertIn("&lt;script&gt;", str(mail.body_html))
        self.assertNotIn("<script>", str(mail.body_html))

    def test_notification_isolates_template_delivery_exception(self):
        mail_template = self.env["mail.template"]
        with (
            patch.object(
                type(mail_template),
                "send_mail",
                autospec=True,
                side_effect=RuntimeError("mail transport unavailable"),
            ),
            mute_logger("odoo.addons.fieldservice_portal.models.notification"),
        ):
            result = self.notifier.notify_customer(
                self.partner,
                False,
                email_template_xmlid="fieldservice_portal.email_visit_booked",
            )

        self.assertEqual(
            result["email"],
            {"success": False, "error": "mail transport unavailable"},
        )

    def test_notification_base_url_and_available_slots_text(self):
        parameters = self.env["ir.config_parameter"].sudo()
        parameters.set_param("web.base.url", "https://field-service.example.test///")
        self.assertEqual(
            self.notifier._get_base_url(), "https://field-service.example.test"
        )

        route = self.routes["installation"]
        second_dayroute = self.env["fsm.route.dayroute"].create(
            {
                "route_id": route.id,
                "team_id": self.team.id,
                "date": self.dayroutes["installation"].date + timedelta(days=1),
            }
        )
        self.env["fsm.route.dayroute"].create(
            {
                "route_id": self.routes["survey"].id,
                "team_id": self.team.id,
                "date": self.dayroutes["installation"].date,
            }
        )
        outside_window = self.env["fsm.route.dayroute"].create(
            {
                "route_id": route.id,
                "team_id": self.team.id,
                "date": fields.Date.context_today(self.env.user) + timedelta(days=29),
            }
        )

        slots = self.notifier._get_available_slots_text("installation", limit=2)
        expected = "\n".join(
            (
                f"{self.dayroutes['installation'].date} - {route.name}",
                f"{second_dayroute.date} - {route.name}",
            )
        )
        self.assertEqual(slots, expected)
        self.assertNotIn(str(outside_window.date), slots)

        route.max_order = 0
        self.dayroutes["installation"]._compute_order_count()
        second_dayroute._compute_order_count()
        self.assertFalse(self.notifier._get_available_slots_text("installation"))

    def test_fsm_stage_write_notifies_and_failure_does_not_rollback_stage(self):
        order = self._create_fsm("survey", person=self.technician)
        with patch.object(
            type(self.notifier),
            "_send_external_message",
            autospec=True,
            side_effect=self._external_success,
        ) as external_send:
            self.assertTrue(order.write({"stage_id": self.started_stage.id}))

        self.assertEqual(order.stage_id, self.started_stage)
        message = external_send.call_args.args[2]
        self.assertIn(self.technician.name, message)
        self.assertIn(order.name, message)

        with (
            patch.object(
                type(order),
                "_on_stage_change",
                autospec=True,
                side_effect=RuntimeError("notification failure"),
            ),
            mute_logger("odoo.addons.fieldservice_portal.models.fsm_order"),
        ):
            self.assertTrue(order.write({"stage_id": self.completed_stage.id}))

        self.assertEqual(order.stage_id, self.completed_stage)
        self.assertTrue(order.write({"name": "FSM renamed without stage event"}))

    def test_fsm_stage_change_guards_and_service_type_fallback(self):
        order = self._create_fsm("other")
        with patch.object(
            type(self.notifier),
            "_send_external_message",
            autospec=True,
            side_effect=self._external_success,
        ) as external_send:
            order._on_stage_change(999999999)
            order._on_stage_change(self.neutral_stage.id)

        external_send.assert_not_called()
        self.assertEqual(order._get_service_type(), "other")

        survey_sale = self._create_sale("survey", amount=None)
        sale_backed_order = self._create_fsm("other", sale=survey_sale)
        self.assertEqual(sale_backed_order._get_service_type(), "survey")
        sale_backed_order.type = self.order_types["repair"]
        self.assertEqual(sale_backed_order._get_service_type(), "repair")

    def test_fsm_notification_partner_is_required_location_owner(self):
        order = self._create_fsm("other")

        self.assertEqual(order._get_notification_partner(), order.location_id.owner_id)

    def test_fsm_started_notification_uses_generic_technician_label(self):
        order = self._create_fsm("other")
        with patch.object(
            type(self.notifier),
            "_send_external_message",
            autospec=True,
            side_effect=self._external_success,
        ) as external_send:
            order._on_stage_change(self.started_stage.id)

        self.assertIn("Technician is on the way", external_send.call_args.args[2])

    def test_fsm_completed_notifications_cover_every_service_type(self):
        mail_template = self.env["mail.template"]
        expected = {
            "survey": (
                "site survey is complete",
                "fieldservice_portal.email_inspection_completed",
            ),
            "installation": (
                "Installation is complete",
                "fieldservice_portal.email_installation_completed",
            ),
            "maintenance": (
                "service is complete",
                "fieldservice_portal.email_maintenance_completed",
            ),
            "repair": (
                "service is complete",
                "fieldservice_portal.email_maintenance_completed",
            ),
            "other": ("work has been completed", False),
        }
        with (
            patch.object(
                type(self.notifier),
                "_send_external_message",
                autospec=True,
                side_effect=self._external_success,
            ) as external_send,
            patch.object(
                type(mail_template), "send_mail", autospec=True, return_value=101
            ) as template_send,
        ):
            for service_type in expected:
                with self.subTest(service_type=service_type):
                    self._create_fsm(service_type)._on_stage_change(
                        self.completed_stage.id
                    )

        self.assertEqual(external_send.call_count, len(expected))
        messages = [call.args[2] for call in external_send.call_args_list]
        for message, (text, _template_xmlid) in zip(
            messages, expected.values(), strict=True
        ):
            self.assertIn(text.lower(), message.lower())
        self.assertEqual(template_send.call_count, 4)
        template_xmlids = [
            template.get_external_id().get(template.id)
            for template in (call.args[0] for call in template_send.call_args_list)
        ]
        self.assertEqual(
            template_xmlids,
            [template for _text, template in expected.values() if template],
        )

    def test_sale_policy_helpers_use_real_payment_transactions(self):
        self.company.visit_confirmation_policy = "phone"
        survey_draft = self._create_sale("survey")
        self.assertFalse(survey_draft._is_visit_released())
        self.assertFalse(survey_draft._has_to_be_signed())
        self.assertFalse(survey_draft._has_to_be_paid())
        self.assertFalse(survey_draft._is_confirmation_amount_reached())

        survey_sale = self._create_sale("survey", state="sale")
        self.assertTrue(survey_sale._is_visit_released())

        self.company.visit_confirmation_policy = "payment"
        payment_survey = self._create_sale(
            "survey", require_payment=True, prepayment_percent=0.25
        )
        self.assertTrue(payment_survey._has_to_be_paid())
        self.assertEqual(
            payment_survey._get_prepayment_required_amount(),
            payment_survey.currency_id.round(payment_survey.amount_total),
        )
        self.assertFalse(payment_survey._is_confirmation_amount_reached())
        self.assertIn("fully paid", payment_survey._confirmation_error_message())

        paid_survey = self._create_sale("survey", state="sale")
        self.assertFalse(paid_survey._is_visit_released())
        self._mark_paid(paid_survey)
        self.assertTrue(paid_survey._is_visit_released())

        self.company.installation_release_policy = "payment"
        installation = self._create_sale("installation", state="sale")
        self.assertFalse(installation._is_installation_released())
        self._mark_paid(installation)
        self.assertTrue(installation._is_installation_released())
        self.assertTrue(installation._is_installation_order())

        zero_installation = self._create_sale("installation", state="sale", amount=0.0)
        self.assertFalse(zero_installation._is_installation_released())

        self.company.installation_release_policy = "approval"
        approved_installation = self._create_sale("installation", state="sale")
        self.assertTrue(approved_installation._is_installation_released())
        self.assertFalse(self._create_sale("installation")._is_installation_released())
        self.assertFalse(
            self._create_sale("other", state="sale")._is_installation_order()
        )

    def test_sale_standard_confirmation_helpers_remain_available(self):
        installation = self._create_sale(
            "installation",
            require_signature=True,
            require_payment=True,
            prepayment_percent=0.5,
        )
        self.assertTrue(installation._has_to_be_signed())
        self.assertTrue(installation._has_to_be_paid())
        self.assertEqual(
            installation._get_prepayment_required_amount(),
            installation.currency_id.round(installation.amount_total * 0.5),
        )
        self.assertFalse(installation._is_confirmation_amount_reached())
        self._mark_paid(installation)
        self.assertTrue(installation._is_confirmation_amount_reached())
        self.assertFalse(installation._has_to_be_paid())

    def test_sale_confirmation_error_paths(self):
        confirmed = self._create_sale("other", state="sale")
        self.assertIn(
            "state requiring confirmation", confirmed._confirmation_error_message()
        )

        self.company.visit_confirmation_policy = "payment"
        survey = self._create_sale("survey")
        self.assertIn("fully paid", survey._confirmation_error_message())
        self._mark_paid(survey)
        self.assertFalse(survey._confirmation_error_message())

        self.company.installation_release_policy = "payment"
        installation = self._create_sale("installation")
        self.assertIn("fully paid", installation._confirmation_error_message())

        self.company.installation_release_policy = "approval"
        self.assertFalse(installation._confirmation_error_message())
        self.assertFalse(self._create_sale("other")._confirmation_error_message())

    def test_sale_state_write_notifies_and_isolates_failure(self):
        installation = self._create_sale("installation")
        mail_template = self.env["mail.template"]
        with (
            patch.object(
                type(self.notifier),
                "_send_external_message",
                autospec=True,
                side_effect=self._external_success,
            ) as external_send,
            patch.object(
                type(mail_template), "send_mail", autospec=True, return_value=101
            ),
        ):
            self.assertTrue(installation.write({"state": "sent"}))

        self.assertEqual(installation.state, "sent")
        self.assertIn(
            "installation quotation is ready", external_send.call_args.args[2]
        )

        with (
            patch.object(
                type(installation),
                "_on_state_change",
                autospec=True,
                side_effect=RuntimeError("notification failure"),
            ),
            mute_logger("odoo.addons.fieldservice_portal.models.sale_order"),
        ):
            self.assertTrue(installation.write({"state": "cancel"}))

        self.assertEqual(installation.state, "cancel")
        self.assertTrue(installation.write({"client_order_ref": "No state change"}))

    def test_sale_state_notifications_cover_service_and_policy_branches(self):
        mail_template = self.env["mail.template"]
        mail_model = self.env["mail.mail"]
        self.company.visit_confirmation_policy = "phone"
        phone_survey = self._create_sale("survey", state="sale")
        maintenance = self._create_sale("maintenance", state="sale")
        repair = self._create_sale("repair", state="sale")
        self.company.installation_release_policy = "approval"
        approved_installation = self._create_sale("installation", state="sale")

        self.company.visit_confirmation_policy = "payment"
        paid_survey = self._create_sale("survey", state="sale")
        self._mark_paid(paid_survey)
        self.company.installation_release_policy = "payment"
        paid_installation = self._create_sale("installation", state="sale")
        self._mark_paid(paid_installation)
        unpaid_installation = self._create_sale("installation", state="sale")
        unrelated = self._create_sale("other", state="sale")

        with (
            patch.object(
                type(self.notifier),
                "_send_external_message",
                autospec=True,
                side_effect=self._external_success,
            ) as external_send,
            patch.object(
                type(mail_template), "send_mail", autospec=True, return_value=101
            ) as template_send,
            patch.object(type(mail_model), "send", autospec=True, return_value=True),
        ):
            self.company.visit_confirmation_policy = "phone"
            phone_survey._on_state_change("sale")
            maintenance._on_state_change("sale")
            repair._on_state_change("sale")
            self.company.installation_release_policy = "approval"
            approved_installation._on_state_change("sale")

            self.company.visit_confirmation_policy = "payment"
            paid_survey._on_state_change("sale")
            self.company.installation_release_policy = "payment"
            paid_installation._on_state_change("sale")
            unpaid_installation._on_state_change("sale")
            unrelated._on_state_change("sale")
            paid_installation._on_state_change("sent")

            partnerless = self.env["sale.order"].new(
                {
                    "sale_order_template_id": self.sale_templates["installation"].id,
                    "company_id": self.company.id,
                }
            )
            partnerless._on_state_change("sent")

        messages = [call.args[2] for call in external_send.call_args_list]
        self.assertEqual(len(messages), 7)
        self.assertTrue(any("phone verification" in message for message in messages))
        self.assertTrue(any("Payment received" in message for message in messages))
        self.assertEqual(
            sum("service request and payment" in message for message in messages), 2
        )
        self.assertTrue(any("approved" in message for message in messages))
        self.assertTrue(
            any("installation appointment" in message for message in messages)
        )
        self.assertTrue(
            any("installation quotation is ready" in message for message in messages)
        )
        self.assertGreaterEqual(template_send.call_count, 4)

    def test_installation_quote_lists_only_first_five_lines(self):
        products = self.env["product.product"].create(
            [
                {
                    "name": f"Installation Item {index}",
                    "type": "service",
                    "field_service_tracking": "no",
                    "list_price": index * 10.0,
                }
                for index in range(1, 7)
            ]
        )
        order = self._create_sale("installation", amount=None)
        self.env["sale.order.line"].create(
            [
                {
                    "order_id": order.id,
                    "product_id": product.id,
                    "product_uom_qty": 1,
                    "price_unit": product.list_price,
                }
                for product in products
            ]
        )
        mail_template = self.env["mail.template"]
        with (
            patch.object(
                type(self.notifier),
                "_send_external_message",
                autospec=True,
                side_effect=self._external_success,
            ) as external_send,
            patch.object(
                type(mail_template), "send_mail", autospec=True, return_value=101
            ),
        ):
            order._notify_installation_quote(self.partner, self.notifier)

        message = external_send.call_args.args[2]
        for product in products[:5]:
            self.assertIn(product.name, message)
        self.assertNotIn(products[5].name, message)
        self.assertIn(order._formatted_amount(order.amount_total), message)
        self.assertIn(f"/my/orders/{order.id}", message)

    def test_installation_release_notifications_include_available_slots(self):
        payment_order = self._create_sale("installation", state="sale")
        approval_order = self._create_sale("installation", state="sale")
        mail_template = self.env["mail.template"]
        mail_model = self.env["mail.mail"]
        with (
            patch.object(
                type(self.notifier),
                "_send_external_message",
                autospec=True,
                side_effect=self._external_success,
            ) as external_send,
            patch.object(
                type(mail_template), "send_mail", autospec=True, return_value=101
            ),
            patch.object(type(mail_model), "send", autospec=True, return_value=True),
        ):
            self.company.installation_release_policy = "payment"
            payment_order._notify_installation_released(self.partner, self.notifier)
            self.company.installation_release_policy = "approval"
            approval_order._notify_installation_released(self.partner, self.notifier)

        payment_message, approval_message = [
            call.args[2] for call in external_send.call_args_list
        ]
        self.assertIn("Payment received", payment_message)
        self.assertIn("Available appointments", payment_message)
        self.assertIn(str(self.dayroutes["installation"].date), payment_message)
        self.assertIn("approved", approval_message)
        self.assertIn("Available appointments", approval_message)
        self.assertIn(f"/my/installation/{approval_order.id}", approval_message)

        self.routes["installation"].max_order = 0
        self.dayroutes["installation"]._compute_order_count()
        with (
            patch.object(
                type(self.notifier),
                "_send_external_message",
                autospec=True,
                side_effect=self._external_success,
            ) as external_send,
            patch.object(
                type(mail_template), "send_mail", autospec=True, return_value=101
            ),
            patch.object(type(mail_model), "send", autospec=True, return_value=True),
        ):
            self.company.installation_release_policy = "payment"
            payment_order._notify_installation_released(self.partner, self.notifier)
            self.company.installation_release_policy = "approval"
            approval_order._notify_installation_released(self.partner, self.notifier)

        for call in external_send.call_args_list:
            self.assertNotIn("Available appointments", call.args[2])

    def test_survey_and_service_notification_messages(self):
        survey = self._create_sale("survey", state="sale")
        service = self._create_sale("maintenance", state="sale")
        mail_template = self.env["mail.template"]
        with (
            patch.object(
                type(self.notifier),
                "_send_external_message",
                autospec=True,
                side_effect=self._external_success,
            ) as external_send,
            patch.object(
                type(mail_template), "send_mail", autospec=True, return_value=101
            ),
        ):
            self.company.visit_confirmation_policy = "payment"
            survey._notify_survey_confirmed(self.partner, self.notifier)
            self.company.visit_confirmation_policy = "phone"
            survey._notify_survey_confirmed(self.partner, self.notifier)
            service._notify_service_paid(self.partner, self.notifier)

        payment_message, phone_message, service_message = [
            call.args[2] for call in external_send.call_args_list
        ]
        self.assertIn("Payment received", payment_message)
        self.assertIn("phone verification", phone_message)
        self.assertIn(survey._formatted_amount(survey.amount_total), payment_message)
        self.assertIn("service request and payment", service_message)

    def test_dayroute_reservation_domain_count_and_available_capacity(self):
        dayroute = self.dayroutes["survey"]
        reserved = self._create_sale("survey", dayroute=dayroute)
        candidate = self._create_sale("survey", dayroute=dayroute)

        base_domain = candidate._portal_dayroute_reservation_domain(dayroute)
        excluded_domain = candidate._portal_dayroute_reservation_domain(
            dayroute, exclude_order=candidate
        )
        self.assertNotIn(("id", "not in", candidate.ids), base_domain)
        self.assertIn(("id", "not in", candidate.ids), excluded_domain)
        self.assertEqual(
            candidate._portal_dayroute_reserved_capacity(
                dayroute, exclude_order=candidate
            ),
            1,
        )
        self.assertEqual(
            candidate._portal_dayroute_available_capacity(
                dayroute, exclude_order=candidate
            ),
            dayroute.order_remaining - 1,
        )
        self.assertEqual(reserved.state, "draft")

    def test_dayroute_reserved_capacity_sums_pending_fsm_orders(self):
        dayroute = self.dayroutes["survey"]
        multiple = self._create_sale("survey", amount=None, dayroute=dayroute)
        multiple.order_line = [
            Command.create(
                {
                    "product_id": self.line_service_product.id,
                    "product_uom_qty": 1,
                }
            ),
            Command.create(
                {
                    "product_id": self.line_service_product.id,
                    "product_uom_qty": 1,
                }
            ),
        ]
        empty = self._create_sale("survey", amount=None, dayroute=dayroute)
        candidate = self._create_sale("survey", amount=None, dayroute=dayroute)

        self.assertEqual(multiple._portal_pending_fsm_count(), 2)
        self.assertEqual(empty._portal_pending_fsm_count(), 0)
        self.assertEqual(
            candidate._portal_dayroute_reserved_capacity(
                dayroute, exclude_order=candidate
            ),
            3,
        )
        self.assertEqual(
            candidate._portal_dayroute_available_capacity(
                dayroute, exclude_order=candidate
            ),
            dayroute.order_remaining - 3,
        )

    def test_dayroute_validation_accepts_matching_routes_and_lock(self):
        for service_type in ("survey", "maintenance", "installation"):
            with self.subTest(service_type=service_type):
                order = self._create_sale(
                    service_type, dayroute=self.dayroutes[service_type]
                )
                self.assertEqual(
                    order._validate_portal_dayroute(service_type, lock=True),
                    self.dayroutes[service_type],
                )

    def test_dayroute_validation_rejects_location_route_mismatch_without_mutation(
        self,
    ):
        order = self._create_sale("survey", dayroute=self.dayroutes["survey"])
        different_route = self.routes["maintenance"]
        order.fsm_location_id.fsm_route_id = different_route

        with self.assertRaisesRegex(
            ValidationError, "does not match the service location route"
        ):
            order._validate_portal_dayroute("survey")

        self.assertEqual(order.fsm_location_id.fsm_route_id, different_route)

    def test_dayroute_validation_rejects_invalid_appointments(self):
        with self.assertRaisesRegex(ValidationError, "Select an appointment"):
            self._create_sale("survey")._validate_portal_dayroute("survey")

        with self.assertRaisesRegex(ValidationError, "does not match"):
            self._create_sale(
                "survey", dayroute=self.dayroutes["installation"]
            )._validate_portal_dayroute("survey")

        with self.assertRaisesRegex(ValidationError, "does not match"):
            self._create_sale(
                "other", dayroute=self.dayroutes["survey"]
            )._validate_portal_dayroute("other")

        past_dayroute = self.env["fsm.route.dayroute"].create(
            {
                "route_id": self.routes["survey"].id,
                "team_id": self.team.id,
                "date": fields.Date.context_today(self.env.user) - timedelta(days=1),
            }
        )
        with self.assertRaisesRegex(ValidationError, "no longer available"):
            self._create_sale(
                "survey", dayroute=past_dayroute
            )._validate_portal_dayroute("survey")

        other_company = self.env["res.company"].create(
            {"name": "Other Portal Model Company"}
        )
        other_team = self.env["fsm.team"].create(
            {"name": "Other Portal Model Team", "company_id": other_company.id}
        )
        cross_company_dayroute = self.env["fsm.route.dayroute"].create(
            {
                "route_id": self.routes["survey"].id,
                "team_id": other_team.id,
                "date": fields.Date.context_today(self.env.user) + timedelta(days=10),
            }
        )
        with self.assertRaisesRegex(ValidationError, "not available for this company"):
            self._create_sale(
                "survey", dayroute=cross_company_dayroute
            )._validate_portal_dayroute("survey")

        self.routes["survey"].max_order = 1
        self.dayroutes["survey"]._compute_order_count()
        self._create_sale("survey", dayroute=self.dayroutes["survey"])
        full_day_order = self._create_sale("survey", dayroute=self.dayroutes["survey"])
        with self.assertRaisesRegex(ValidationError, "no remaining capacity"):
            full_day_order._validate_portal_dayroute("survey")

    def test_pending_fsm_count_uses_line_and_sale_tracking_rules(self):
        linked_fsm = self._create_fsm("maintenance")
        order = self._create_sale("maintenance", amount=None)
        order.order_line = [
            Command.create({"display_type": "line_section", "name": "Section"}),
            Command.create({"display_type": "line_note", "name": "Note"}),
            Command.create(
                {
                    "product_id": self.service_product.id,
                    "product_uom_qty": 1,
                }
            ),
            Command.create(
                {
                    "product_id": self.line_service_product.id,
                    "product_uom_qty": 1,
                }
            ),
            Command.create(
                {
                    "product_id": self.line_service_product.id,
                    "product_uom_qty": 1,
                }
            ),
            Command.create(
                {
                    "product_id": self.line_service_product.id,
                    "product_uom_qty": 1,
                    "fsm_order_id": linked_fsm.id,
                }
            ),
            Command.create(
                {
                    "product_id": self.sale_service_product.id,
                    "product_uom_qty": 1,
                }
            ),
            Command.create(
                {
                    "product_id": self.sale_service_product.id,
                    "product_uom_qty": 1,
                }
            ),
        ]

        self.assertEqual(order._portal_pending_fsm_count(), 3)

    def test_pending_fsm_count_reuses_existing_sale_fsm_order(self):
        dayroute = self.dayroutes["maintenance"]
        order = self._create_sale(
            "maintenance",
            product=self.service_product,
            dayroute=dayroute,
        )
        existing_fsm = self._create_fsm(
            "maintenance",
            sale=order,
            dayroute_id=dayroute.id,
            fsm_route_id=dayroute.route_id.id,
        )
        dayroute.route_id.max_order = 1
        dayroute._compute_order_count()

        self.assertEqual(dayroute.order_remaining, 0)
        self.assertEqual(order._portal_pending_fsm_count(), 0)
        self.assertFalse(order._field_service_generation())
        self.assertEqual(order.order_line.fsm_order_id, existing_fsm)

    def test_untracked_portal_service_line_counts_and_generates_sale_fsm(self):
        self.company.visit_confirmation_policy = "phone"
        order = self._create_sale(
            "survey",
            product=self.service_product,
            dayroute=self.dayroutes["survey"],
        )

        self.assertEqual(order._portal_pending_fsm_count(), 1)

        mail_template = self.env["mail.template"]
        with (
            patch.object(
                type(self.notifier),
                "_send_external_message",
                autospec=True,
                side_effect=self._external_success,
            ),
            patch.object(
                type(mail_template), "send_mail", autospec=True, return_value=101
            ),
        ):
            self.assertTrue(order.action_confirm())

        self.assertEqual(len(order.fsm_order_ids), 1)
        self.assertEqual(order.order_line.fsm_order_id, order.fsm_order_ids)

    def test_prepare_fsm_values_adds_portal_schedule_and_description(self):
        dayroute = self.dayroutes["survey"]
        order = self._create_sale(
            "survey",
            amount=None,
            dayroute=dayroute,
            description="Customer-provided service details",
        )
        values = order._prepare_fsm_values(so_id=order.id)
        self.assertEqual(values["dayroute_id"], dayroute.id)
        self.assertEqual(values["fsm_route_id"], dayroute.route_id.id)
        self.assertEqual(values["person_id"], dayroute.person_id.id)
        self.assertEqual(values["team_id"], dayroute.team_id.id)
        self.assertEqual(values["request_early"], dayroute.date_start_planned)
        self.assertEqual(values["scheduled_date_start"], dayroute.date_start_planned)
        self.assertEqual(values["description"], "Customer-provided service details")
        self.assertEqual(
            self.env["fsm.order.type"].browse(values["type"]).service_type,
            "survey",
        )

        plain_order = self._create_sale("other", amount=None)
        plain_values = plain_order._prepare_fsm_values(so_id=plain_order.id)
        self.assertNotIn("dayroute_id", plain_values)
        self.assertNotIn("description", plain_values)

    def test_field_service_generation_rejects_unreleased_survey(self):
        self.company.visit_confirmation_policy = "phone"
        order = self._create_sale(
            "survey",
            product=self.sale_service_product,
            dayroute=self.dayroutes["survey"],
        )
        with self.assertRaisesRegex(ValidationError, "must be confirmed"):
            order._field_service_generation()
        self.assertFalse(order.fsm_order_ids)

    def test_action_confirm_generates_scheduled_fsm_order(self):
        self.company.visit_confirmation_policy = "phone"
        order = self._create_sale(
            "survey",
            product=self.sale_service_product,
            dayroute=self.dayroutes["survey"],
            description="Confirmed survey details",
        )
        mail_template = self.env["mail.template"]
        with (
            patch.object(
                type(self.notifier),
                "_send_external_message",
                autospec=True,
                side_effect=self._external_success,
            ),
            patch.object(
                type(mail_template), "send_mail", autospec=True, return_value=101
            ),
        ):
            self.assertTrue(order.action_confirm())

        self.assertEqual(order.state, "sale")
        self.assertEqual(len(order.fsm_order_ids), 1)
        generated = order.fsm_order_ids
        self.assertEqual(generated.dayroute_id, self.dayroutes["survey"])
        self.assertEqual(generated.fsm_route_id, self.routes["survey"])
        self.assertEqual(generated.person_id, self.technician)
        self.assertIn("Confirmed survey details", generated.description)

    def test_portal_return_url_uses_installation_release_policy(self):
        self.company.installation_release_policy = "approval"
        released = self._create_sale("installation", state="sale")
        self.assertEqual(
            released._get_portal_return_url(), f"/my/installation/{released.id}"
        )

        self.company.installation_release_policy = "payment"
        unreleased = self._create_sale("installation", state="sale")
        self.assertEqual(unreleased._get_portal_return_url(), "/my/orders")
        self.assertEqual(
            self._create_sale("other")._get_portal_return_url(), "/my/orders"
        )
