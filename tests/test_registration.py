"""学号自助注册与审核流程的测试。"""
from __future__ import annotations

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from competition.models import AppSetting, Profile
from competition.services import registration
from tests.utils import Workspace

PWD = "DIPcompetition2026"


class RegistrationTests(TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.ws = Workspace()

    @classmethod
    def tearDownClass(cls):
        cls.ws.cleanup()
        super().tearDownClass()

    def setUp(self):
        self.setting = AppSetting.get()
        self.setting.registration_open = True
        self.setting.save()

    def _post(self, **overrides):
        data = {
            "student_id": "20260001",
            "real_name": "张三",
            "password1": PWD,
            "password2": PWD,
        }
        data.update(overrides)
        return self.client.post(reverse("register"), data)

    def test_closed_registration_blocks_get_and_service(self):
        self.setting.registration_open = False
        self.setting.save()
        resp = self.client.get(reverse("register"))
        self.assertEqual(resp.status_code, 403)
        self.assertContains(resp, "注册暂未开放", status_code=403)
        with self.assertRaises(registration.RegistrationError):
            registration.register_student("20260002", "李四", PWD, setting=self.setting)

    def test_register_creates_active_account_and_logs_in(self):
        resp = self._post()
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp.url, reverse("home"))

        profile = Profile.objects.get(student_id="20260001")
        self.assertEqual(profile.status, Profile.STATUS_ACTIVE)
        self.assertEqual(profile.contest_code, "S001")
        self.assertFalse(profile.must_change_password)
        self.assertEqual(profile.user.username, "20260001")
        # 注册后已自动登录，可以进入首页
        self.assertEqual(self.client.get(reverse("home")).status_code, 200)

    def test_contest_code_continues_from_existing(self):
        user = User.objects.create_user(username="old", password=PWD)
        Profile.objects.create(
            user=user, student_id="old", real_name="旧生", contest_code="S007"
        )
        self._post()
        self.assertEqual(Profile.objects.get(student_id="20260001").contest_code, "S008")

    def test_invite_code_required(self):
        self.setting.registration_code = "DIP2026"
        self.setting.save()

        resp = self._post()
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "注册码")
        self.assertFalse(Profile.objects.filter(student_id="20260001").exists())

        resp = self._post(registration_code="wrong")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "注册码不正确")

        resp = self._post(registration_code="DIP2026")
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(Profile.objects.filter(student_id="20260001").exists())

    def test_prefix_and_duplicate_validation(self):
        self.setting.registration_prefix = "52"
        self.setting.save()
        resp = self._post(student_id="20260001")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "需要以 52 开头")

        resp = self._post(student_id="52285500019")
        self.assertEqual(resp.status_code, 302)
        # 同一学号再注册一次应被拒绝
        self.client.logout()
        resp = self._post(student_id="52285500019")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "该学号已注册")

    def test_approval_flow_blocks_login_until_staff_approves(self):
        self.setting.registration_require_approval = True
        self.setting.save()

        resp = self._post()
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp.url, reverse("login"))

        profile = Profile.objects.get(student_id="20260001")
        self.assertEqual(profile.status, Profile.STATUS_PENDING)

        resp = self.client.post(
            reverse("login"), {"username": "20260001", "password": PWD}
        )
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "等待教师审核")

        staff = User.objects.create_user(username="teacher", password=PWD, is_staff=True)
        self.client.force_login(staff)
        resp = self.client.post(
            reverse("staff_registration_action"),
            {"user_id": profile.user_id, "action": "approve"},
        )
        self.assertEqual(resp.status_code, 302)
        profile.refresh_from_db()
        self.assertEqual(profile.status, Profile.STATUS_ACTIVE)

        self.client.logout()
        resp = self.client.post(
            reverse("login"), {"username": "20260001", "password": PWD}
        )
        self.assertEqual(resp.status_code, 302)

    def test_force_password_change_redirects_after_register(self):
        self.setting.registration_force_password_change = True
        self.setting.save()
        resp = self._post()
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp.url, reverse("password_change"))
        self.assertTrue(Profile.objects.get(student_id="20260001").must_change_password)

    def test_rejected_account_cannot_stay_logged_in(self):
        self._post()
        profile = Profile.objects.get(student_id="20260001")
        profile.status = Profile.STATUS_REJECTED
        profile.save(update_fields=["status"])

        resp = self.client.get(reverse("home"))
        self.assertEqual(resp.status_code, 302)
        self.assertIn(reverse("login"), resp.url)
