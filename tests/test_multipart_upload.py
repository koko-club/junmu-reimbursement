"""Streaming multipart invoice uploads without a file-count or size cap."""

from pathlib import Path
import tempfile
import unittest

from multipart_upload import parse_multipart_upload


def multipart_body(parts: list[tuple[str, bytes, str | None]], boundary: str) -> bytes:
    body = bytearray()
    for name, value, filename in parts:
        body.extend(f"--{boundary}\r\n".encode())
        disposition = f'Content-Disposition: form-data; name="{name}"'
        if filename is not None:
            disposition += f'; filename="{filename}"'
        body.extend(disposition.encode() + b"\r\n\r\n")
        body.extend(value + b"\r\n")
    body.extend(f"--{boundary}--\r\n".encode())
    return bytes(body)


class MultipartUploadTest(unittest.TestCase):
    def test_streams_large_pdf_and_preserves_unlimited_file_order(self):
        boundary = "test-boundary-123"
        pdf = b"%PDF-1.4\n" + b"x" * (11 * 1024 * 1024) + b"\r\n--test-boundary-123Xstill-pdf"
        body = multipart_body([
            ("payload", b'{"reason":"test"}', None),
            ("invoices", pdf, "first.pdf"),
            ("invoices", b"%PDF-1.4 second", "second.pdf"),
            ("invoices", b"%PDF-1.4 third", "third.pdf"),
        ], boundary)
        chunks = (body[index:index + 8191] for index in range(0, len(body), 8191))
        with tempfile.TemporaryDirectory() as directory:
            parsed = parse_multipart_upload(chunks, boundary.encode(), Path(directory))
            self.assertEqual(parsed.payload_raw, b'{"reason":"test"}')
            self.assertEqual([path.name for path in parsed.invoices],
                             ["0000-first.pdf", "0001-second.pdf", "0002-third.pdf"])
            self.assertEqual(parsed.invoices[0].read_bytes(), pdf)
            self.assertEqual(parsed.invoices[2].read_bytes(), b"%PDF-1.4 third")

    def test_rejects_truncated_body_and_unsafe_filename(self):
        boundary = b"test-boundary"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaises(ValueError):
                parse_multipart_upload([b"--test-boundary\r\nContent-Disposition: form-data; name=\"invoices\"; filename=\"x.pdf\"\r\n\r\n%PDF"], boundary, root)
            body = multipart_body([("invoices", b"%PDF", "../bad.pdf")], boundary.decode())
            with self.assertRaises(ValueError):
                parse_multipart_upload([body], boundary, root)

    def test_reads_invoice_layout_selection(self):
        boundary = "layout-boundary"
        body = multipart_body([
            ("payload", b"{}", None),
            ("invoice_layout", b"4", None),
        ], boundary)
        with tempfile.TemporaryDirectory() as directory:
            parsed = parse_multipart_upload([body], boundary.encode(), Path(directory))
        self.assertEqual(parsed.layout_raw, b"4")


if __name__ == "__main__":
    unittest.main()
