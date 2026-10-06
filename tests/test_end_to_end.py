"""Real generator -> workbook -> PDF verification for the reimbursement flow."""

from __future__ import annotations

import hashlib
from pathlib import Path
import sys
import tempfile
import unittest

from PIL import Image, ImageDraw
from openpyxl import load_workbook

APP_DIR = Path(__file__).resolve().parents[1]
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

from generator import generate_workbook
from office import BUNDLED_SOFFICE, OfficeError, export_pdf, find_soffice


TEMPLATE = APP_DIR / "resources" / "差旅报销单模板.xlsx"


def complete_payload() -> dict:
    """Return a payload that exercises public transit, self-driving, and blanks."""
    return {
        "date": "2026-09-04",
        "department": "技术部",
        "traveler": "张三",
        "reason": "客户拜访",
        "days": 2,
        "allowance": 77.5,
        "rows": [
            {
                "date": "2026-09-01",
                "origin": "上海",
                "destination": "杭州",
                "transport": "高铁",
                "public_amount": 12.5,
                "receipts": 2,
            },
            {
                "date": "2026-09-02",
                "origin": "杭州",
                "destination": "宁波",
                "transport": "自驾",
                "mileage": 180,
                "toll": 20,
                "lodging": 200,
                "receipts": 1,
            },
            {},
        ],
    }


def make_png(path: Path, label: str, color: tuple[int, int, int]) -> None:
    image = Image.new("RGB", (320, 180), color)
    draw = ImageDraw.Draw(image)
    draw.text((20, 70), label, fill="white")
    image.save(path, format="PNG")


class EndToEndTest(unittest.TestCase):
    def test_complete_generation_and_optional_pdf_conversion(self):
        template_hash_before = hashlib.sha256(TEMPLATE.read_bytes()).hexdigest()
        template_mtime_before = TEMPLATE.stat().st_mtime_ns

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first_image = root / "route-01.png"
            second_image = root / "route-02.png"
            make_png(first_image, "route 1", (34, 105, 170))
            make_png(second_image, "route 2", (42, 135, 80))
            payload = complete_payload()
            long_traveler = "张" * 100
            payload["traveler"] = long_traveler

            first = generate_workbook(
                TEMPLATE,
                root,
                payload,
                image_paths=[first_image, second_image],
            )
            self.assertTrue(first.path.is_file())
            self.assertEqual(first.screenshot_count, 2)
            self.assertEqual(first.receipt_count, 3)

            source_ws = load_workbook(TEMPLATE, data_only=False, read_only=False)["1月"]
            source_signatures = [source_ws.cell(22, column).value for column in range(1, 13)]
            source_ws.parent.close()

            workbook = load_workbook(first.path, data_only=False, read_only=False)
            try:
                self.assertEqual(workbook.sheetnames, ["差旅报销单", "里程截图01", "里程截图02"])
                ws = workbook["差旅报销单"]
                self.assertEqual(ws.max_row, 23)
                self.assertEqual(ws["A3"].value, "报销日期：2026/09/04")
                self.assertEqual(ws["B4"].value, "技术部")
                self.assertEqual(ws["B5"].value, long_traveler)
                self.assertEqual(ws["E5"].value, "客户拜访")
                self.assertEqual(ws["J7"].value, 2)
                self.assertEqual(ws["K7"].value, "=J7*77.5")
                self.assertEqual(ws["L5"].value, "附\n单\n据\n\n张")
                self.assertEqual(ws.print_area, "'差旅报销单'!$A$1:$L$23")

                self.assertEqual(ws["G9"].value, '=IF(F9*1=0,"",F9*1)')
                self.assertEqual(ws["G10"].value, '=IF(F10*1=0,"",F10*1)')
                self.assertEqual(
                    ws["J9"].value,
                    '=IF(F9*1 + E9 + H9 + I9 = 0, "", F9*1 + E9 + H9 + I9)',
                )
                self.assertEqual(
                    ws["J10"].value,
                    '=IF(F10*1 + E10 + H10 + I10 = 0, "", F10*1 + E10 + H10 + I10)',
                )
                # Receipt count is driven by detail receipts, not screenshot count.
                self.assertEqual(ws["K9"].value, 2)
                self.assertEqual(ws["K10"].value, 1)

                # The third detail row remains empty instead of receiving numeric zeroes.
                for column in ("A", "B", "C", "D", "E", "F", "H", "I", "K"):
                    self.assertIsNone(ws[f"{column}11"].value, f"{column}11 should stay blank")
                self.assertEqual(ws["A22"].value, source_signatures[0])
                self.assertEqual(ws["J22"].value, source_signatures[9])
                self.assertEqual(
                    [ws.cell(22, column).value for column in range(1, 13)],
                    source_signatures,
                )
                self.assertEqual(len(workbook["里程截图01"]._images), 1)
                self.assertEqual(len(workbook["里程截图02"]._images), 1)
            finally:
                workbook.close()

            soffice = None
            try:
                soffice = find_soffice(str(BUNDLED_SOFFICE))
            except OfficeError:
                # Generator assertions remain mandatory when PDF conversion is unavailable.
                pass

            first_pdf = None
            if soffice is not None:
                first_pdf = export_pdf(first.path, root, soffice)
                self.assertTrue(first_pdf.is_file())
                self.assertGreater(first_pdf.stat().st_size, 0)
                try:
                    from pypdf import PdfReader
                except ImportError:  # pragma: no cover - bundled runtime currently includes pypdf
                    PdfReader = None
                if PdfReader is not None:
                    reader = PdfReader(str(first_pdf))
                    self.assertGreaterEqual(len(reader.pages), 3)
                    first_page = reader.pages[0]
                    self.assertAlmostEqual(float(first_page.mediabox.width), 595.28, delta=1)
                    self.assertAlmostEqual(float(first_page.mediabox.height), 841.89, delta=1)
                    text_positions = {}

                    def collect_text(text, cm, tm, _font, _size):
                        if text.strip() in {"差旅费用报销单", "审核人"}:
                            text_positions[text.strip()] = cm[5] + tm[5]

                    first_page.extract_text(visitor_text=collect_text)
                    page_height = float(first_page.mediabox.height)
                    form_top = page_height - text_positions["差旅费用报销单"]
                    form_bottom = page_height - text_positions["审核人"]
                    self.assertAlmostEqual((form_top + form_bottom) / 2, page_height / 4, delta=12)
                    from pypdf.generic import ContentStream

                    operations = ContentStream(first_page.get_contents(), reader).operations
                    midpoint = page_height / 2
                    guide_points = [
                        (op, float(args[0]), float(args[1]))
                        for args, op in operations
                        if op in (b"m", b"l") and len(args) == 2
                        and abs(float(args[1]) - midpoint) < 1
                    ]
                    self.assertEqual([point[0] for point in guide_points], [b"m", b"l"])
                    self.assertAlmostEqual(guide_points[1][1] - guide_points[0][1], 72 * 2 / 25.4, delta=0.5)
                    self.assertAlmostEqual(guide_points[1][1], float(first_page.mediabox.width) - 18, delta=1)
                    strokes = []
                    stroke_width = None
                    start = end = None
                    for args, operator in operations:
                        if operator == b"w":
                            stroke_width = float(args[0])
                        elif operator == b"m":
                            start = tuple(map(float, args))
                        elif operator == b"l":
                            end = tuple(map(float, args))
                        elif operator == b"S" and start and end:
                            strokes.append((*start, *end, stroke_width))
                            start = end = None
                    right_edge = [
                        line for line in strokes
                        if 560 < line[0] < 563 and abs(line[0] - line[2]) < 0.1
                        and abs(line[1] - line[3]) > 10
                    ]
                    self.assertTrue(right_edge)
                    self.assertGreater(max(abs(line[1] - line[3]) for line in right_edge), 200)
                    self.assertTrue(all(line[4] >= 1.5 for line in right_edge), "right outer border is thin")
                    full_width_lines = [
                        line for line in strokes
                        if line[0] < 30 and line[2] > 550 and abs(line[1] - line[3]) < 0.1
                    ]
                    bottom_edge = min(full_width_lines, key=lambda line: line[1])
                    self.assertGreaterEqual(bottom_edge[4], 1.5, "bottom outer border is thin")
                    first_text = reader.pages[0].extract_text() or ""
                    self.assertIn("差旅费用报销单", first_text)
                    self.assertIn("2026/09/04", first_text)
                    self.assertIn(long_traveler, first_text)
                    self.assertNotIn("Err:502", first_text)
                    self.assertIn("伍佰陆拾柒元伍角", first_text)
                    self.assertNotIn("里程粘贴区", first_text)
                    self.assertNotIn("详见后附里程截图", first_text)
                    appendix_one_text = reader.pages[1].extract_text() or ""
                    appendix_two_text = reader.pages[2].extract_text() or ""
                    self.assertIn("1/2", appendix_one_text)
                    self.assertIn("2/2", appendix_two_text)
                    self.assertTrue(any(image.data for image in reader.pages[1].images))

            # Keep a byte-for-byte snapshot so the collision generation below
            # proves that the first workbook was not overwritten.
            first_bytes = first.path.read_bytes()
            second = generate_workbook(
                TEMPLATE,
                root,
                payload,
                image_paths=[first_image, second_image],
            )
            self.assertTrue(second.path.is_file())
            self.assertEqual(second.path.name, f"{first.path.stem}-2.xlsx")
            self.assertEqual(first.path.read_bytes(), first_bytes)
            if soffice is not None:
                second_pdf = export_pdf(second.path, root, soffice)
                self.assertEqual(second_pdf.name, f"{first_pdf.stem}-2.pdf")
                self.assertTrue(second_pdf.is_file())
                self.assertGreater(second_pdf.stat().st_size, 0)

        self.assertEqual(hashlib.sha256(TEMPLATE.read_bytes()).hexdigest(), template_hash_before)
        self.assertEqual(TEMPLATE.stat().st_mtime_ns, template_mtime_before)


if __name__ == "__main__":
    unittest.main()
