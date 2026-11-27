"""学号自助注册的测试（只保留「开放/关闭」一个开关）。"""
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

    def test_register_creates_usable_account_and_logs_in(self):
        resp = self._post()
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp.url, reverse("home"))

        profile = Profile.objects.get(student_id="20260001")
        self.assertEqual(profile.contest_code, "S001")
        self.assertFalse(profile.must_change_password)
        self.assertEqual(profile.user.username, "20260001")
        # 注册即可用：自动登录后能进首页
        self.assertEqual(self.client.get(reverse("home")).status_code, 200)

        # 退出后用刚设置的密码可以直接登录，没有审核等待
        self.client.logout()
        resp = self.client.post(
            reverse("login"), {"username": "20260001", "password": PWD}
        )
        self.assertEqual(resp.status_code, 302)

    def test_contest_code_continues_from_existing(self):
        user = User.objects.create_user(username="old", password=PWD)
        Profile.objects.create(
            user=user, student_id="old", real_name="旧生", contest_code="S007"
        )
        self._post()
        self.assertEqual(Profile.objects.get(student_id="20260001").contest_code, "S008")

    def test_duplicate_and_invalid_student_id_rejected(self):
        self._post()
        self.client.logout()

        resp = self._post()
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "该学号已注册")

        resp = self._post(student_id="abc123")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "学号应为")

        self.assertEqual(Profile.objects.filter(student_id="20260001").count(), 1)

    def test_password_confirmation_and_strength(self):
        resp = self._post(password2="another-pass-123")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "两次输入的密码不一致")
        self.assertFalse(Profile.objects.filter(student_id="20260001").exists())

        resp = self._post(password1="12345678", password2="12345678")
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(Profile.objects.filter(student_id="20260001").exists())

    def test_staff_can_toggle_registration_only(self):
        staff = User.objects.create_user(username="teacher", password=PWD, is_staff=True)
        self.client.force_login(staff)

        resp = self.client.post(
            reverse("staff_settings"), {"section": "registration"}
        )
        self.assertEqual(resp.status_code, 302)
        self.setting.refresh_from_db()
        self.assertFalse(self.setting.registration_open)

        resp = self.client.post(
            reverse("staff_settings"),
            {"section": "registration", "registration_open": "on"},
        )
        self.assertEqual(resp.status_code, 302)
        self.setting.refresh_from_db()
        self.assertTrue(self.setting.registration_open)

        # 教师面板只显示一个开关，不再有邀请码 / 前缀 / 审核 / 强制改密
        resp = self.client.get(reverse("staff_dashboard"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "开放自助注册")
        for gone in ("注册邀请码", "学号前缀", "注册后需教师审核", "注册后强制改密"):
            self.assertNotContains(resp, gone)

    def test_register_page_has_no_extra_fields(self):
        resp = self.client.get(reverse("register"))
        self.assertEqual(resp.status_code, 200)
        for gone in ("注册码", "邀请码"):
            self.assertNotContains(resp, gone)
