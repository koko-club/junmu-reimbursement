"""Place uploaded PDF invoice pages in two-up or four-up A4 layouts."""

from __future__ import annotations

import os
from pathlib import Path
import tempfile

from pypdf import PdfReader, PdfWriter, Transformation
from pypdf.generic import DecodedStreamObject
from PIL import Image, ImageOps, UnidentifiedImageError


INVOICE_HORIZONTAL_MARGIN_PT = 24
INVOICE_VERTICAL_MARGIN_PT = 30
FOUR_UP_HORIZONTAL_MARGIN_PT = 14
FOUR_UP_VERTICAL_MARGIN_PT = 10
CUT_GUIDE_MARGIN_PT = 18
CUT_GUIDE_LENGTH_PT = 72 * 2 / 25.4


def validate_invoice_pdf(path: Path) -> int:
    """Return the page count of a readable, unencrypted invoice PDF."""
    path = Path(path)
    with path.open("rb") as source:
        header = source.read(5)
    if header != b"%PDF-":
        raise ValueError("发票必须是有效的 PDF 文件")
    try:
        reader = PdfReader(path, strict=False)
        if reader.is_encrypted:
            raise ValueError("不支持加密的发票 PDF")
        count = len(reader.pages)
        if count < 1:
            raise ValueError("发票 PDF 页数无效")
        for page in reader.pages:
            box = page.cropbox
            if float(box.width) <= 0 or float(box.height) <= 0:
                raise ValueError("发票 PDF 页面尺寸无效")
        return count
    except ValueError:
        raise
    except Exception as error:
        raise ValueError("发票必须是有效的 PDF 文件") from error


def prepare_invoice_uploads(paths: list[Path]) -> tuple[list[Path], list[int]]:
    """Normalize source images to PDF and count invoice pages used by the layout."""
    prepared = []
    counts = []
    for path in paths:
        if path.suffix.lower() == ".pdf":
            counts.append(validate_invoice_pdf(path))
            prepared.append(path)
            continue
        if path.suffix.lower() not in {".jpg", ".jpeg", ".png"}:
            raise ValueError("仅支持 PDF、JPG 或 PNG 发票文件")
        destination = path.with_suffix(".pdf")
        try:
            with Image.open(path) as image:
                if image.format not in {"JPEG", "PNG"} or image.width * image.height > 30_000_000:
                    raise ValueError("发票图片无效或尺寸过大")
                image.load()
                normalized = ImageOps.exif_transpose(image).convert("RGB")
                normalized.save(destination, format="PDF")
        except (OSError, UnidentifiedImageError, Image.DecompressionBombError) as error:
            raise ValueError("发票图片无效") from error
        prepared.append(destination)
        counts.append(1)
    return prepared, counts


def _place_invoice(target, invoice, slot: int, width: float, height: float) -> None:
    """Fit one PDF page inside a paper half, centered with printable margins."""
    if invoice.get("/Rotate", 0):
        invoice.transfer_rotation_to_content()
    box = invoice.cropbox
    source_width, source_height = float(box.width), float(box.height)
    half_height = height / 2
    usable_width = width - 2 * INVOICE_HORIZONTAL_MARGIN_PT
    usable_height = half_height - 2 * INVOICE_VERTICAL_MARGIN_PT
    if min(source_width, source_height, usable_width, usable_height) <= 0:
        raise ValueError("发票 PDF 页面尺寸无效")
    scale = min(usable_width / source_width, usable_height / source_height)
    left = (width - source_width * scale) / 2
    bottom = (half_height - source_height * scale) / 2
    if slot == 0:
        bottom += half_height
    transform = (
        Transformation()
        .translate(-float(box.left), -float(box.bottom))
        .scale(scale)
        .translate(left, bottom)
    )
    target.merge_transformed_page(invoice, transform, expand=False)


def _place_invoice_four(target, invoice, slot: int, width: float, height: float) -> None:
    """Fit an invoice in a centered quarter, ordered across then down."""
    if invoice.get("/Rotate", 0):
        invoice.transfer_rotation_to_content()
    box = invoice.cropbox
    source_width, source_height = float(box.width), float(box.height)
    cell_width, cell_height = width / 2, height / 2
    usable_width = cell_width - 2 * FOUR_UP_HORIZONTAL_MARGIN_PT
    usable_height = cell_height - 2 * FOUR_UP_VERTICAL_MARGIN_PT
    if min(source_width, source_height, usable_width, usable_height) <= 0:
        raise ValueError("发票 PDF 页面尺寸无效")
    scale = min(usable_width / source_width, usable_height / source_height)
    column, row = slot % 2, slot // 2
    left = column * cell_width + (cell_width - source_width * scale) / 2
    bottom = (1 - row) * cell_height + (cell_height - source_height * scale) / 2
    transform = (
        Transformation()
        .translate(-float(box.left), -float(box.bottom))
        .scale(scale)
        .translate(left, bottom)
    )
    target.merge_transformed_page(invoice, transform, expand=False)


def _add_cut_guide(page, width: float, height: float) -> None:
    guide = PdfWriter().add_blank_page(width=width, height=height)
    stream = DecodedStreamObject()
    midpoint = height / 2
    stream.set_data((
        f"q\n0.75 G\n0.5 w\n[6 3] 0 d\n"
        f"{width - CUT_GUIDE_MARGIN_PT - CUT_GUIDE_LENGTH_PT:.3f} {midpoint:.3f} m\n"
        f"{width - CUT_GUIDE_MARGIN_PT:.3f} {midpoint:.3f} l\nS\nQ\n"
    ).encode("ascii"))
    guide.replace_contents(stream)
    page.merge_page(guide)


def append_invoice_pages(report_path: Path, invoice_paths: list[Path], *, layout: int = 2) -> None:
    """Write one report plus ordered invoices back to the report path atomically."""
    if layout not in (2, 4):
        raise ValueError("发票排版方式无效")
    if not invoice_paths:
        return
    for path in invoice_paths:
        validate_invoice_pdf(path)
    report_path = Path(report_path)
    writer = PdfWriter(clone_from=report_path)
    if len(writer.pages) != 1:
        raise ValueError("报销单 PDF 必须只有一页")
    first = writer.pages[0]
    width, height = float(first.mediabox.width), float(first.mediabox.height)
    if width <= 0 or height <= 0:
        raise ValueError("报销单 PDF 页面尺寸无效")
    readers = [PdfReader(path, strict=False) for path in invoice_paths]
    invoice_index = 0
    for reader in readers:
        for invoice in reader.pages:
            if layout == 2:
                if invoice_index == 0:
                    target, slot = first, 1
                else:
                    position = invoice_index - 1
                    if position % 2 == 0:
                        target = writer.add_blank_page(width=width, height=height)
                        _add_cut_guide(target, width, height)
                    slot = position % 2
                _place_invoice(target, invoice, slot, width, height)
            else:
                if invoice_index < 2:
                    target, slot = first, invoice_index + 2
                else:
                    position = invoice_index - 2
                    if position % 4 == 0:
                        target = writer.add_blank_page(width=width, height=height)
                        _add_cut_guide(target, width, height)
                    slot = position % 4
                _place_invoice_four(target, invoice, slot, width, height)
            invoice_index += 1
    descriptor, temporary = tempfile.mkstemp(prefix=".invoice-layout-", suffix=".pdf", dir=report_path.parent)
    try:
        with os.fdopen(descriptor, "wb") as output:
            writer.write(output)
        os.replace(temporary, report_path)
    finally:
        Path(temporary).unlink(missing_ok=True)
