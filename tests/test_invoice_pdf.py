"""Invoice PDF validation and two-up reimbursement layout."""

from pathlib import Path
import tempfile
import unittest

from pypdf import PdfReader, PdfWriter
from pypdf.generic import DecodedStreamObject

from invoice_pdf import append_invoice_pages, validate_invoice_pdf


def make_pdf(path: Path, labels: list[str], width: float = 595.28, height: float = 841.89) -> None:
    writer = PdfWriter()
    for label in labels:
        page = writer.add_blank_page(width=width, height=height)
        stream = DecodedStreamObject()
        stream.set_data(f"BT ({label}) Tj ET\n0 0 m 10 10 l S\n".encode("ascii"))
        page.replace_contents(stream)
    with path.open("wb") as output:
        writer.write(output)


class InvoicePdfTest(unittest.TestCase):
    def test_four_up_keeps_report_above_two_invoices_then_places_four_per_page(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            report = root / "report.pdf"
            invoice = root / "invoices.pdf"
            make_pdf(report, ["REPORT"])
            make_pdf(invoice, [f"INVOICE{index}" for index in range(1, 8)], width=420, height=297)

            append_invoice_pages(report, [invoice], layout=4)

            pages = PdfReader(report).pages
            self.assertEqual(len(pages), 3)
            for page, expected in zip(pages, ((1, 2), (3, 4, 5, 6), (7,))):
                content = page.get_contents().get_data()
                for index in range(1, 8):
                    self.assertEqual(f"INVOICE{index}".encode() in content, index in expected)
            self.assertIn(b"REPORT", pages[0].get_contents().get_data())
            self.assertNotIn(b"REPORT", pages[1].get_contents().get_data())

    def test_unknown_invoice_layout_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            report = root / "report.pdf"
            invoice = root / "invoice.pdf"
            make_pdf(report, ["REPORT"])
            make_pdf(invoice, ["INVOICE"])
            with self.assertRaises(ValueError):
                append_invoice_pages(report, [invoice], layout=3)

    def test_first_invoice_uses_lower_half_and_following_invoices_are_two_per_page(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            report = root / "report.pdf"
            first = root / "first.pdf"
            rest = root / "rest.pdf"
            make_pdf(report, ["REPORT"])
            make_pdf(first, ["INVOICEONE"], width=420, height=297)
            make_pdf(rest, ["INVOICETWO", "INVOICETHREE", "INVOICEFOUR"], width=420, height=297)

            append_invoice_pages(report, [first, rest])

            pages = PdfReader(report).pages
            self.assertEqual(len(pages), 3)
            for page in pages:
                self.assertAlmostEqual(float(page.mediabox.width), 595.28, places=1)
                self.assertAlmostEqual(float(page.mediabox.height), 841.89, places=1)
            contents = [page.get_contents().get_data() for page in pages]
            self.assertIn(b"REPORT", contents[0])
            self.assertIn(b"INVOICEONE", contents[0])
            self.assertNotIn(b"INVOICETWO", contents[0])
            self.assertIn(b"INVOICETWO", contents[1])
            self.assertIn(b"INVOICETHREE", contents[1])
            self.assertIn(b"INVOICEFOUR", contents[2])

    def test_invalid_and_encrypted_documents_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            invalid = root / "invalid.pdf"
            invalid.write_bytes(b"not a pdf")
            with self.assertRaises(ValueError):
                validate_invoice_pdf(invalid)
            encrypted = root / "encrypted.pdf"
            writer = PdfWriter()
            writer.add_blank_page(width=420, height=297)
            writer.encrypt("secret")
            with encrypted.open("wb") as output:
                writer.write(output)
            with self.assertRaises(ValueError):
                validate_invoice_pdf(encrypted)

    def test_more_than_one_hundred_invoice_pages_are_accepted(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            report = root / "report.pdf"
            first = root / "first.pdf"
            second = root / "second.pdf"
            make_pdf(report, ["REPORT"])
            make_pdf(first, ["A"] * 51)
            make_pdf(second, ["B"] * 50)
            append_invoice_pages(report, [first, second])
            self.assertEqual(len(PdfReader(report).pages), 51)


if __name__ == "__main__":
    unittest.main()
