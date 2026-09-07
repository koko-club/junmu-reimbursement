from pathlib import Path
import sys
import unittest

APP_DIR = Path(__file__).resolve().parents[1]
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

from validation import ValidationError, safe_output_stem, validate_image_filename, validate_payload


def payload(**overrides):
    value = {
        "date": "2026-09-04",
        "department": "技术部",
        "traveler": "张三",
        "reason": "客户拜访",
        "days": "2",
        "allowance": "50.5",
        "rows": [
            {
                "date": "2026-09-01",
                "origin": "上海",
                "destination": "杭州",
                "public_amount": "12.50",
                "mileage": "10",
                "toll": "20.25",
                "lodging": "180",
                "receipts": "2",
            }
        ],
    }
    value.update(overrides)
    return value


class ValidationTest(unittest.TestCase):
    def test_formula_leading_text_is_rejected(self):
        for field in ("department", "traveler", "reason"):
            with self.subTest(field=field), self.assertRaises(ValidationError):
                validate_payload(payload(**{field: "=1+1"}))
        for field in ("origin", "destination", "transport"):
            row = {
                "date": "2026-09-01",
                "origin": "上海",
                "destination": "杭州",
                field: "@SUM(1,1)",
            }
            with self.subTest(field=field), self.assertRaises(ValidationError):
                validate_payload(payload(rows=[row]))

    def test_valid_payload_is_normalized_and_padded(self):
        result = validate_payload(payload())

        self.assertEqual(result["days"], 2)
        self.assertEqual(result["allowance"], 50.5)
        self.assertEqual(len(result["rows"]), 11)
        self.assertEqual(result["rows"][0]["public_amount"], 12.5)
        self.assertEqual(result["receipt_count"], 2)
        self.assertEqual(result["rows"][1], {})

    def test_trusted_profile_overrides_untrusted_client_values_before_validation(self):
        value = payload(department="=CLIENT", traveler=None, date="")

        result = validate_payload(
            value,
            traveler=" 王安全 ",
            department=" 财务部 ",
        )

        self.assertEqual(result["traveler"], "王安全")
        self.assertEqual(result["department"], "财务部")
        self.assertEqual(result["date"], "")
        self.assertEqual(len(result["rows"]), 11)

    def test_trusted_profile_values_are_strictly_validated(self):
        for field, value in (("traveler", "@SUM(1,1)"), ("department", "")):
            overrides = {"traveler": "王安全", "department": "财务部", field: value}
            with self.subTest(field=field), self.assertRaises(ValidationError):
                validate_payload(payload(), **overrides)

    def test_missing_traveler_is_rejected(self):
        with self.assertRaises(ValidationError):
            validate_payload(payload(traveler=""))

    def test_negative_amount_is_rejected(self):
        with self.assertRaises(ValidationError):
            validate_payload(payload(rows=[{"public_amount": "-1"}]))

    def test_fractional_receipts_is_rejected(self):
        with self.assertRaises(ValidationError):
            validate_payload(payload(rows=[{"receipts": "1.5"}]))

    def test_incomplete_populated_row_is_rejected(self):
        with self.assertRaisesRegex(ValidationError, r"requires origin and destination together"):
            validate_payload(payload(rows=[{"date": "2026-09-01", "origin": "上海"}]))

    def test_receipts_only_row_is_rejected_and_not_counted(self):
        with self.assertRaises(ValidationError):
            validate_payload(payload(rows=[{"receipts": "1"}]))

    def test_optional_blank_date_is_preserved(self):
        result = validate_payload(payload(date=""))
        self.assertEqual(result["date"], "")

    def test_complete_row_may_omit_date(self):
        row = {"origin": "上海", "destination": "杭州", "public_amount": "12"}
        result = validate_payload(payload(rows=[row]))
        self.assertEqual(result["rows"][0]["origin"], "上海")
        self.assertEqual(result["rows"][0].get("date", ""), "")

    def test_date_text_is_not_format_validated(self):
        for value in ("2026-9-7", "2026/9/7"):
            with self.subTest(value=value):
                self.assertEqual(validate_payload(payload(date=value))["date"], value)

    def test_image_extension_is_case_insensitive_but_restricted(self):
        self.assertEqual(validate_image_filename("receipt.JPEG"), "receipt.JPEG")
        with self.assertRaises(ValidationError):
            validate_image_filename("receipt.gif")

    def test_unsafe_traveler_characters_are_removed_from_stem(self):
        stem = safe_output_stem("2026-09-04", "../张三\n")
        self.assertEqual(stem, "2026-09-04-张三-差旅报销单")
        self.assertNotIn("/", stem)
        self.assertNotIn("\\", stem)

    def test_legitimate_repeated_dots_are_preserved_in_stem(self):
        self.assertEqual(
            safe_output_stem("2026-09-04", "A..B"),
            "2026-09-04-A..B-差旅报销单",
        )

    def test_duplicate_inputs_produce_same_deterministic_stem(self):
        self.assertEqual(
            safe_output_stem("2026-09-04", "张三"),
            safe_output_stem("2026-09-04", "张三"),
        )


if __name__ == "__main__":
    unittest.main()
