"""Regression tests for routing notifications when a ticket is created."""

from unittest.mock import patch

from django.test import TestCase
from django.urls import reverse

from helpdesk.cbv.ticket_type import TicketsCreateFormView
from helpdesk.forms import TicketForm
from helpdesk.models import DepartmentManager, Ticket, TicketType
from horilla.testkit import make_company, make_employee, make_user
from horilla.testkit.factories import make_job_position


class TicketFormWithAssignees(TicketForm):
    class Meta(TicketForm.Meta):
        fields = [*TicketForm.Meta.fields, "assigned_to"]


class TicketCreateForwardingTests(TestCase):
    assignment_verb = "You have been assigned to a new Ticket"
    forwarding_verb = "A new Ticket has been forwarded to you"

    @classmethod
    def setUpTestData(cls):
        company = make_company()
        cls.owner_user = make_user("ticket-owner")
        cls.owner = make_employee(
            company=company, email=cls.owner_user.email, user=cls.owner_user
        )
        recipient_user = make_user("ticket-recipient")
        cls.recipient = make_employee(
            company=company, email=recipient_user.email, user=recipient_user
        )
        cls.assignee = make_employee(company=company, email="assignee@test.horilla")
        cls.ticket_type = TicketType.objects.create(
            title="IT Support", type="service_request", prefix="ITS"
        )
        cls.position = make_job_position()
        cls.department = cls.position.department_id
        DepartmentManager.objects.create(
            department=cls.department, manager=cls.recipient
        )

    def setUp(self):
        self.client.force_login(self.owner_user)

    def create_ticket(self, assigning_type, destination, **fields):
        with (
            patch("helpdesk.cbv.ticket_type.TicketSendThread"),
            patch("helpdesk.cbv.ticket_type.notify.send") as notify,
        ):
            response = self.client.post(
                reverse("ticket-create"),
                {
                    "title": "Laptop will not boot",
                    "description": "Dead on power-on.",
                    "employee_id": self.owner.pk,
                    "ticket_type": self.ticket_type.pk,
                    "priority": "medium",
                    "status": "new",
                    "assigning_type": assigning_type,
                    "raised_on": destination.pk,
                    **fields,
                },
                HTTP_HX_REQUEST="true",
            )
        self.assertLess(response.status_code, 400)
        ticket = Ticket.objects.get(title="Laptop will not boot")
        self.assertEqual(ticket.raised_on, str(destination.pk))
        deliveries = []
        for call in notify.call_args_list:
            self.assertEqual(
                call.kwargs["redirect"],
                reverse("ticket-detail", kwargs={"ticket_id": ticket.pk}),
            )
            deliveries.extend(
                (user, call.kwargs["verb"]) for user in call.kwargs["recipient"]
            )
        return ticket, deliveries

    def test_individual_forwarding_leaves_ticket_unassigned(self):
        ticket, deliveries = self.create_ticket("individual", self.recipient)

        self.assertFalse(ticket.assigned_to.exists())
        self.assertCountEqual(
            deliveries,
            [
                (self.recipient.employee_user_id, self.forwarding_verb),
                (self.owner_user, self.assignment_verb),
            ],
        )
        self.client.force_login(self.recipient.employee_user_id)
        response = self.client.get(
            reverse("ticket-detail", kwargs={"ticket_id": ticket.pk})
        )
        self.assertContains(response, ticket.title)

    def test_form_assignees_are_preserved_when_forwarding_to_an_assignee(self):
        with patch.object(TicketsCreateFormView, "form_class", TicketFormWithAssignees):
            ticket, deliveries = self.create_ticket(
                "individual",
                self.recipient,
                assigned_to=[self.recipient.pk, self.assignee.pk],
            )

        self.assertCountEqual(ticket.assigned_to.all(), [self.recipient, self.assignee])
        self.assertCountEqual(
            deliveries,
            [
                (self.recipient.employee_user_id, self.forwarding_verb),
                (self.assignee.employee_user_id, self.assignment_verb),
                (self.owner_user, self.assignment_verb),
            ],
        )

    def test_forwarding_to_owner_only_sends_forwarding_notification(self):
        ticket, deliveries = self.create_ticket("individual", self.owner)

        self.assertFalse(ticket.assigned_to.exists())
        self.assertEqual(deliveries, [(self.owner_user, self.forwarding_verb)])

    def test_department_forwarding_notifies_owner_and_department_manager(self):
        ticket, deliveries = self.create_ticket("department", self.department)

        self.assertFalse(ticket.assigned_to.exists())
        self.assertCountEqual(
            deliveries,
            [
                (self.owner_user, self.assignment_verb),
                (self.recipient.employee_user_id, self.assignment_verb),
            ],
        )

    def test_job_position_forwarding_notifies_owner(self):
        ticket, deliveries = self.create_ticket("job_position", self.position)

        self.assertFalse(ticket.assigned_to.exists())
        self.assertEqual(deliveries, [(self.owner_user, self.assignment_verb)])
