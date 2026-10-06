import unittest

from tests.http_helpers import RunningApp


class RegistrationRulesTest(unittest.TestCase):
    def setUp(self):
        self.running = RunningApp().__enter__()
        self.client = self.running.client
        setup = self.client.post_json("/api/setup", {
            "username": "admin", "password": "admin-password",
            "real_name": "管理员", "department": "财务部",
        })
        self.assertEqual(setup.status, 201)

    def tearDown(self):
        self.running.__exit__(None, None, None)

    def register(self, **changes):
        payload = {
            "username": "newuser", "password": "123456",
            "real_name": "张三", "department": "技术部",
        }
        payload.update(changes)
        return self.client.post_json("/api/register", payload)

    def test_six_character_password_can_register_and_log_in_after_approval(self):
        response = self.register()
        self.assertEqual(response.status, 201)
        admin = self.running.users.authenticate("admin", "admin-password")
        pending = self.running.users.list_pending(admin.id)[0]
        self.running.users.approve(admin.id, pending.id)
        login = self.client.post_json("/api/login", {
            "username": "newuser", "password": "123456",
        })
        self.assertEqual(login.status, 200)

    def test_password_below_six_characters_identifies_password_field(self):
        response = self.register(password="12345")
        self.assertEqual(response.status, 400)
        self.assertEqual(response.json(), {
            "error": "密码至少需要6位", "field": "password",
        })

    def test_non_chinese_name_identifies_name_field(self):
        response = self.register(real_name="张San")
        self.assertEqual(response.status, 400)
        self.assertEqual(response.json(), {
            "error": "姓名必须使用中文汉字", "field": "real_name",
        })

    def test_non_chinese_department_identifies_department_field(self):
        response = self.register(department="技术Dept")
        self.assertEqual(response.status, 400)
        self.assertEqual(response.json(), {
            "error": "部门必须使用中文汉字", "field": "department",
        })

    def test_username_length_identifies_username_field(self):
        response = self.register(username="ab")
        self.assertEqual(response.status, 400)
        self.assertEqual(response.json(), {
            "error": "账号需为3到50个字符", "field": "username",
        })


if __name__ == "__main__":
    unittest.main()
