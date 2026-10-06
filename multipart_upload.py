"""Stream reimbursement multipart fields to private files with bounded memory."""

from __future__ import annotations

from dataclasses import dataclass
from email import policy
from email.parser import BytesParser
from io import BytesIO
from pathlib import Path
from typing import Iterable

from validation import validate_image_filename, validate_invoice_upload_filename


_MAX_HEADER_BYTES = 16 * 1024
_MAX_PAYLOAD_BYTES = 1024 * 1024


@dataclass(frozen=True)
class UploadParts:
    payload_raw: bytes | None
    screenshots: list[Path]
    invoices: list[Path]
    layout_raw: bytes | None = None


class _PayloadSink(BytesIO):
    def write(self, data: bytes) -> int:
        if self.tell() + len(data) > _MAX_PAYLOAD_BYTES:
            raise ValueError("报销表单数据过大")
        return super().write(data)


class _DiscardSink:
    def write(self, data: bytes) -> int:
        return len(data)


class _ChunkReader:
    def __init__(self, chunks: Iterable[bytes]):
        self._chunks = iter(chunks)
        self.buffer = bytearray()

    def _fill(self) -> None:
        for chunk in self._chunks:
            if chunk:
                self.buffer.extend(chunk)
                return
        raise ValueError("请求体不完整")

    def take(self, count: int) -> bytes:
        while len(self.buffer) < count:
            self._fill()
        value = bytes(self.buffer[:count])
        del self.buffer[:count]
        return value

    def headers(self) -> bytes:
        delimiter = b"\r\n\r\n"
        while True:
            position = self.buffer.find(delimiter)
            if position >= 0:
                if position > _MAX_HEADER_BYTES:
                    raise ValueError("上传字段头过长")
                value = bytes(self.buffer[:position])
                del self.buffer[:position + len(delimiter)]
                return value
            if len(self.buffer) > _MAX_HEADER_BYTES + len(delimiter):
                raise ValueError("上传字段头过长")
            self._fill()

    def copy_part(self, marker: bytes, sink) -> bytes:
        """Copy a part until a complete next/final boundary; return its suffix."""
        while True:
            position = self.buffer.find(marker)
            if position >= 0:
                while len(self.buffer) < position + len(marker) + 2:
                    self._fill()
                suffix = bytes(self.buffer[position + len(marker):position + len(marker) + 2])
                if suffix in (b"\r\n", b"--"):
                    sink.write(bytes(self.buffer[:position]))
                    del self.buffer[:position + len(marker) + 2]
                    return suffix
                # The bytes resemble a delimiter but belong to the file.
                sink.write(bytes(self.buffer[:position + len(marker)]))
                del self.buffer[:position + len(marker)]
                continue
            safe_count = len(self.buffer) - len(marker) - 1
            if safe_count > 0:
                sink.write(bytes(self.buffer[:safe_count]))
                del self.buffer[:safe_count]
            self._fill()

    def finish(self) -> None:
        trailing = bytes(self.buffer)
        for chunk in self._chunks:
            trailing += chunk
            if len(trailing) > 2:
                raise ValueError("multipart 结束符后有多余数据")
        if trailing not in (b"", b"\r\n"):
            raise ValueError("multipart 结束符无效")


def parse_multipart_upload(
    chunks: Iterable[bytes], boundary: bytes, directory: Path
) -> UploadParts:
    """Parse browser multipart data while writing each attachment incrementally."""
    if not boundary or len(boundary) > 70 or any(value < 33 or value > 126 for value in boundary):
        raise ValueError("multipart boundary 无效")
    reader = _ChunkReader(chunks)
    if reader.take(len(boundary) + 2) != b"--" + boundary:
        raise ValueError("multipart 起始边界无效")
    opening = reader.take(2)
    if opening == b"--":
        reader.finish()
        return UploadParts(None, [], [])
    if opening != b"\r\n":
        raise ValueError("multipart 起始边界无效")

    payload_raw: bytes | None = None
    layout_raw: bytes | None = None
    screenshots: list[Path] = []
    invoices: list[Path] = []
    marker = b"\r\n--" + boundary
    while True:
        header_bytes = reader.headers()
        part = BytesParser(policy=policy.default).parsebytes(header_bytes + b"\r\n\r\n")
        if part.get_content_disposition() != "form-data":
            raise ValueError("上传字段格式无效")
        name = part.get_param("name", header="content-disposition")
        filename = part.get_filename()
        if name == "payload" and payload_raw is None:
            sink = _PayloadSink()
            suffix = reader.copy_part(marker, sink)
            payload_raw = sink.getvalue()
        elif name == "invoice_layout" and layout_raw is None:
            sink = _PayloadSink()
            suffix = reader.copy_part(marker, sink)
            layout_raw = sink.getvalue()
        elif name == "screenshots" and filename:
            safe_name = validate_image_filename(filename)
            path = Path(directory) / f"{len(screenshots):04d}-{safe_name}"
            with path.open("xb") as output:
                suffix = reader.copy_part(marker, output)
            screenshots.append(path)
        elif name == "invoices" and filename:
            safe_name = validate_invoice_upload_filename(filename)
            path = Path(directory) / f"{len(invoices):04d}-{safe_name}"
            with path.open("xb") as output:
                suffix = reader.copy_part(marker, output)
            invoices.append(path)
        else:
            suffix = reader.copy_part(marker, _DiscardSink())
        if suffix == b"--":
            reader.finish()
            return UploadParts(payload_raw, screenshots, invoices, layout_raw)
