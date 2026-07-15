from django.test import TestCase
from django.urls import reverse

from .models import User
from channel_manager.models import AuditEvent


class RoleRoutingTests(TestCase):
    def setUp(self):
        self.master = User.objects.create_user(username="master", password="StrongPass123!", role=User.Role.MASTER, display_name="总账号")
        self.sub = User.objects.create_user(username="sub", password="StrongPass123!", role=User.Role.SUB, parent=self.master, display_name="子账号")

    def test_master_login_redirects_to_master_dashboard(self):
        response = self.client.post(reverse("login"), {"username": "master", "password": "StrongPass123!"})
        self.assertRedirects(response, reverse("role-home"), fetch_redirect_response=False)
        response = self.client.get(reverse("role-home"))
        self.assertRedirects(response, reverse("master-dashboard"), fetch_redirect_response=False)

    def test_subaccount_cannot_open_master_pages(self):
        self.client.force_login(self.sub)
        response = self.client.get(reverse("master-dashboard"))
        self.assertRedirects(response, reverse("user-contents"), fetch_redirect_response=False)

    def test_master_cannot_open_subaccount_frontend(self):
        self.client.force_login(self.master)
        response = self.client.get(reverse("user-contents"))
        self.assertRedirects(response, reverse("master-dashboard"), fetch_redirect_response=False)

    def test_forced_password_change_blocks_business_pages(self):
        self.sub.force_password_change = True
        self.sub.save(update_fields=("force_password_change",))
        self.client.force_login(self.sub)
        response = self.client.get(reverse("user-contents"))
        self.assertRedirects(response, reverse("password-change"), fetch_redirect_response=False)

        response = self.client.post(
            reverse("password-change"),
            {
                "old_password": "StrongPass123!",
                "new_password1": "NewStrongPass456!",
                "new_password2": "NewStrongPass456!",
            },
        )
        self.assertRedirects(response, reverse("role-home"), fetch_redirect_response=False)
        self.sub.refresh_from_db()
        self.assertFalse(self.sub.force_password_change)

    def test_master_can_create_subaccount_from_web(self):
        self.client.force_login(self.master)
        response = self.client.post(
            reverse("master-subaccount-create"),
            {
                "username": "sub_001",
                "display_name": "子账号001",
                "password1": "123456",
                "password2": "123456",
            },
        )
        created = User.objects.get(username="sub_001")
        self.assertEqual(created.parent, self.master)
        self.assertEqual(created.role, User.Role.SUB)
        self.assertTrue(created.check_password("123456"))
        self.assertRedirects(
            response,
            reverse("master-subaccount-detail", args=[created.pk]),
            fetch_redirect_response=False,
        )

    def test_login_is_rate_limited_after_repeated_failures(self):
        for _ in range(10):
            AuditEvent.objects.create(
                action="auth.login",
                object_type="user",
                object_id="master",
                outcome=AuditEvent.Outcome.FAILED,
                ip="127.0.0.1",
            )
        response = self.client.post(
            reverse("login"),
            {"username": "master", "password": "StrongPass123!"},
            REMOTE_ADDR="127.0.0.1",
        )
        self.assertEqual(response.status_code, 429)
        self.assertFalse(response.wsgi_request.user.is_authenticated)
