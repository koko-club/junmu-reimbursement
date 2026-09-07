from pathlib import Path
import os
import stat
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
import unittest
from unittest.mock import patch

APP_DIR = Path(__file__).resolve().parents[1]
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

import office
from office import OfficeError, export_pdf, find_soffice


BUNDLED_SOFFICE = Path(
    "/Users/koko/.cache/codex-runtimes/codex-primary-runtime/"
    "dependencies/bin/override/soffice"
)


def make_executable(path: Path, body: str = "#!/bin/sh\nexit 0\n") -> Path:
    path.write_text(body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


class OfficeTest(unittest.TestCase):
    def test_portable_fontconfig_is_relative_to_libreoffice_contents(self):
        self.assertEqual(
            office.PORTABLE_FONTCONFIG_DIR,
            office.PORTABLE_SOFFICE.parent.parent / "Resources" / "fontconfig",
        )

    def test_amount_to_upper_matches_template_style(self):
        self.assertEqual(office._amount_to_upper(550), "伍佰伍拾元整")
        self.assertEqual(office._amount_to_upper("567.50"), "伍佰陆拾柒元伍角")
        self.assertEqual(office._amount_to_upper(0), "零元整")

    def test_find_soffice_accepts_configured_executable(self):
        with tempfile.TemporaryDirectory() as tmp:
            configured = make_executable(Path(tmp) / "soffice")
            self.assertEqual(find_soffice(str(configured)), configured)

    def test_find_soffice_accepts_bundled_executable(self):
        with tempfile.TemporaryDirectory() as tmp:
            bundled = make_executable(Path(tmp) / "bundled-soffice")
            with patch("office.BUNDLED_SOFFICE", bundled), patch("office.shutil.which", return_value=None):
                self.assertEqual(find_soffice(""), bundled)

    def test_find_soffice_rejects_invalid_configured_path(self):
        with self.assertRaisesRegex(OfficeError, "not executable|does not exist"):
            find_soffice("/definitely/missing/soffice")

    def test_export_pdf_moves_generated_pdf_and_cleans_temp_dirs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workbook = root / "claim.xlsx"
            workbook.write_bytes(b"xlsx")
            fake = make_executable(
                root / "soffice",
                "#!/bin/sh\n"
                "outdir=''\n"
                "while [ $# -gt 0 ]; do\n"
                "  if [ \"$1\" = --outdir ]; then shift; outdir=$1; fi\n"
                "  shift\n"
                "done\n"
                "printf 'pdf' > \"$outdir/claim.pdf\"\n",
            )
            pdf = export_pdf(workbook, root, fake)
            self.assertEqual(pdf, root / "claim.pdf")
            self.assertEqual(pdf.read_bytes(), b"pdf")
            self.assertEqual(list(root.glob(".soffice-profile-*")), [])
            self.assertEqual(list(root.glob(".pdf-convert-*")), [])

    def test_bundled_conversion_loads_system_fontconfig(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workbook = root / "claim.xlsx"
            workbook.write_bytes(b"xlsx")
            observed = {}

            def fake_run(command, **kwargs):
                observed["env"] = kwargs["env"]
                observed["cache_exists_during_run"] = Path(kwargs["env"]["XDG_CACHE_HOME"]).is_dir()
                outdir = Path(command[command.index("--outdir") + 1])
                (outdir / "claim.pdf").write_bytes(b"pdf")
                return type("Completed", (), {"returncode": 0, "stderr": ""})()

            with patch("office.subprocess.run", side_effect=fake_run):
                export_pdf(workbook, root, BUNDLED_SOFFICE)

            self.assertTrue(observed["env"]["FONTCONFIG_FILE"].endswith("/Resources/fontconfig/fonts.conf"))
            self.assertTrue(observed["env"]["FONTCONFIG_PATH"].endswith("/Resources/fontconfig"))
            self.assertTrue(observed["cache_exists_during_run"])

    def test_fontconfig_cache_failure_is_reported_as_office_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workbook = root / "claim.xlsx"
            workbook.write_bytes(b"xlsx")
            with patch("office._conversion_environment", side_effect=OSError("cache is not writable")):
                with self.assertRaisesRegex(OfficeError, "could not prepare PDF conversion: cache is not writable"):
                    export_pdf(workbook, root, BUNDLED_SOFFICE)
            self.assertEqual(list(root.glob(".soffice-profile-*")), [])
            self.assertEqual(list(root.glob(".pdf-convert-*")), [])

    def test_export_pdf_reports_stderr_and_leaves_no_partial_pdf(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workbook = root / "claim.xlsx"
            workbook.write_bytes(b"xlsx")
            fake = make_executable(root / "soffice", "#!/bin/sh\necho 'conversion failed' >&2\nexit 7\n")
            with self.assertRaisesRegex(OfficeError, "conversion failed"):
                export_pdf(workbook, root, fake)
            self.assertFalse((root / "claim.pdf").exists())
            self.assertTrue(workbook.exists())

    def test_export_pdf_avoids_overwriting_existing_pdf(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workbook = root / "claim.xlsx"
            workbook.write_bytes(b"xlsx")
            existing = root / "claim.pdf"
            existing.write_bytes(b"original")
            fake = make_executable(
                root / "soffice",
                "#!/bin/sh\n"
                "outdir=''\n"
                "while [ $# -gt 0 ]; do\n"
                "  if [ \"$1\" = --outdir ]; then shift; outdir=$1; fi\n"
                "  shift\n"
                "done\n"
                "printf 'new' > \"$outdir/claim.pdf\"\n",
            )
            pdf = export_pdf(workbook, root, fake)
            self.assertEqual(pdf, root / "claim-2.pdf")
            self.assertEqual(existing.read_bytes(), b"original")
            self.assertEqual(pdf.read_bytes(), b"new")
            next_pdf = export_pdf(workbook, root, fake)
            self.assertEqual(next_pdf, root / "claim-3.pdf")
            self.assertEqual(pdf.read_bytes(), b"new")

    def test_concurrent_exports_reserve_distinct_pdf_names(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workbook = root / "claim.xlsx"
            workbook.write_bytes(b"xlsx")
            fake = make_executable(
                root / "soffice",
                "#!/bin/sh\n"
                "outdir=''\n"
                "while [ $# -gt 0 ]; do\n"
                "  if [ \"$1\" = --outdir ]; then shift; outdir=$1; fi\n"
                "  shift\n"
                "done\n"
                "printf '%s' \"$outdir\" > \"$outdir/claim.pdf\"\n",
            )
            with ThreadPoolExecutor(max_workers=2) as executor:
                paths = list(executor.map(lambda _: export_pdf(workbook, root, fake), range(2)))
            self.assertEqual({path.name for path in paths}, {"claim.pdf", "claim-2.pdf"})
            contents = {(root / "claim.pdf").read_text(), (root / "claim-2.pdf").read_text()}
            self.assertEqual(len(contents), 2)
            self.assertTrue(all(contents))

    def test_pdf_reservation_wraps_unexpected_oserror(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch("office.os.open", side_effect=OSError("permission denied")):
                with self.assertRaisesRegex(OfficeError, "could not reserve PDF destination"):
                    office._reserve_pdf_destination(Path(tmp), "claim")

    def test_pdf_reservation_removes_placeholder_when_close_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch("office.os.close", side_effect=OSError("close failed")):
                with self.assertRaisesRegex(OfficeError, "could not reserve PDF destination"):
                    office._reserve_pdf_destination(root, "claim")
            self.assertFalse((root / "claim.pdf").exists())

    def test_export_pdf_converts_timeout_to_office_error_and_cleans(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workbook = root / "claim.xlsx"
            workbook.write_bytes(b"xlsx")
            fake = make_executable(root / "soffice", "#!/bin/sh\nsleep 30\n")
            with patch("office.EXPORT_TIMEOUT_SECONDS", 0.01):
                with self.assertRaisesRegex(OfficeError, "timed out"):
                    export_pdf(workbook, root, fake)
            self.assertFalse((root / "claim.pdf").exists())
            self.assertEqual(list(root.glob(".soffice-profile-*")), [])
            self.assertEqual(list(root.glob(".pdf-convert-*")), [])

    def test_cleanup_errors_do_not_mask_office_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workbook = root / "claim.xlsx"
            workbook.write_bytes(b"xlsx")
            fake = make_executable(root / "soffice", "#!/bin/sh\necho boom >&2\nexit 1\n")
            with patch("office.shutil.rmtree", side_effect=OSError("cleanup failed")):
                with self.assertRaisesRegex(OfficeError, "boom"):
                    export_pdf(workbook, root, fake)


if __name__ == "__main__":
    unittest.main()
