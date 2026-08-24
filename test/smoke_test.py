# Copyright (c) 2026 Jifeng Wu
# Licensed under the MIT License. See LICENSE file in the project root
# for full license information.
"""Self-contained smoke test for resumable_file_server.

Run from the repository root with either Python 2 or Python 3:

    python -m test.smoke_test
"""
from __future__ import print_function

import io
import os
import re
import shutil
import tempfile

from typing import List, Text, Tuple

from parse_multipart_form_data import (
    PartBegin,
    PartData,
    PartEnd,
    parse_multipart_form_data,
)
from resumable_file_server import (
    format_current_date_time,
    html_escape,
    http_request_uri_path_to_internal_path,
    iter_body_chunks,
    parse_range_header,
    serve_directory,
    serve_file,
    upload_filename_to_internal_child_name,
    write_error,
)


HTTP_DATE_RE = re.compile(
    r"^[A-Z][a-z]{2}, \d{2} [A-Z][a-z]{2} \d{4} \d{2}:\d{2}:\d{2} GMT$"
)


def make_fixture():
    # type: () -> Text
    """Create a temporary directory tree and return its filesystem path."""
    base = tempfile.mkdtemp()

    with open(os.path.join(base, "hello.txt"), "w") as fh:
        fh.write("hello world")

    with open(os.path.join(base, "data.bin"), "wb") as fh:
        fh.write(b"\x00\x01\x02\x03binary")

    os.mkdir(os.path.join(base, "sub"))
    with open(os.path.join(base, "sub", "nested.txt"), "w") as fh:
        fh.write("nested")

    return base


def check_format_date(base):
    # type: (Text) -> None
    value = format_current_date_time()
    assert HTTP_DATE_RE.match(value), "unexpected HTTP date: %r" % (value,)


def check_html_escape(base):
    # type: (Text) -> None
    assert html_escape(u'<a href="x">&\'') == u'&lt;a href=&quot;x&quot;&gt;&amp;&#39;'


def check_write_error(base):
    # type: (Text) -> None
    buf = io.BytesIO()
    write_error(buf, 404)
    data = buf.getvalue()
    assert data.startswith(b"HTTP/1.1 404 Not found\r\n")
    assert b"404 Not found" in data


def check_write_error_head(base):
    # type: (Text) -> None
    buf = io.BytesIO()
    write_error(buf, 404, u"HEAD")
    data = buf.getvalue()
    assert data.startswith(b"HTTP/1.1 404 Not found\r\n")
    assert b"content-length:" in data
    assert data.endswith(b"\r\n\r\n")


def check_directory_listing(base):
    # type: (Text) -> None
    buf = io.BytesIO()
    serve_directory(buf, u"GET", base, base, u"127.0.0.1", 12345)
    data = buf.getvalue()
    assert data.startswith(b"HTTP/1.1 200 OK\r\n")
    assert b"Directory listing" in data
    assert b"hello.txt" in data
    assert b"data.bin" in data
    assert b"sub/" in data
    assert b"enctype='multipart/form-data'" in data


def check_directory_listing_head(base):
    # type: (Text) -> None
    buf = io.BytesIO()
    serve_directory(buf, u"HEAD", base, base, u"127.0.0.1", 12345)
    data = buf.getvalue()
    assert data.startswith(b"HTTP/1.1 200 OK\r\n")
    assert b"content-length:" in data
    assert data.endswith(b"\r\n\r\n")


def check_full_download(base):
    # type: (Text) -> None
    path = os.path.join(base, "hello.txt")
    buf = io.BytesIO()
    serve_file(buf, u"GET", path, None, u"127.0.0.1", 12345)
    data = buf.getvalue()
    assert data.startswith(b"HTTP/1.1 200 OK\r\n")
    assert b"accept-ranges: bytes" in data
    assert b"content-length: 11" in data
    assert data.endswith(b"hello world")


def check_range_download(base):
    # type: (Text) -> None
    path = os.path.join(base, "hello.txt")
    buf = io.BytesIO()
    serve_file(buf, u"GET", path, u"bytes=2-6", u"127.0.0.1", 12345)
    data = buf.getvalue()
    assert data.startswith(b"HTTP/1.1 206 Partial Content\r\n")
    assert b"content-range: bytes 2-6/11" in data
    assert b"content-length: 5" in data
    assert data.endswith(b"llo w")


def check_range_not_satisfiable(base):
    # type: (Text) -> None
    path = os.path.join(base, "hello.txt")
    buf = io.BytesIO()
    serve_file(buf, u"GET", path, u"bytes=99-100", u"127.0.0.1", 12345)
    data = buf.getvalue()
    assert data.startswith(b"HTTP/1.1 416 Requested Range Not Satisfiable\r\n")
    assert b"content-range: bytes */11" in data


def check_range_edge_cases(base):
    # type: (Text) -> None
    assert parse_range_header(u"bytes=-5", 11) == (6, 10)
    assert parse_range_header(u"bytes=0-99", 11) == (0, 10)
    assert parse_range_header(u"bytes=11-", 11) is None
    assert parse_range_header(u"bytes=-0", 11) is None

    for value in (u"bytes=-", u"bytes=0-1,3-4", u"bytes=one-two"):
        try:
            parse_range_header(value, 11)
        except ValueError:
            pass
        else:
            raise AssertionError("expected malformed range: %s" % (value,))

    path = os.path.join(base, "hello.txt")
    buf = io.BytesIO()
    serve_file(buf, u"GET", path, u"bytes=-5", u"127.0.0.1", 12345)
    data = buf.getvalue()
    assert b"content-range: bytes 6-10/11" in data
    assert data.endswith(b"world")

    buf = io.BytesIO()
    serve_file(buf, u"GET", path, u"bytes=0-99", u"127.0.0.1", 12345)
    data = buf.getvalue()
    assert b"content-range: bytes 0-10/11" in data
    assert data.endswith(b"hello world")


def check_head_download(base):
    # type: (Text) -> None
    path = os.path.join(base, "hello.txt")
    buf = io.BytesIO()
    serve_file(buf, u"HEAD", path, None, u"127.0.0.1", 12345)
    data = buf.getvalue()
    assert data.startswith(b"HTTP/1.1 200 OK\r\n")
    assert b"content-length: 11" in data
    assert data.endswith(b"\r\n\r\n")


def check_path_traversal_blocked(base):
    # type: (Text) -> None
    assert http_request_uri_path_to_internal_path(base, "/../etc/passwd") is None
    assert http_request_uri_path_to_internal_path(base, "/hello.txt") == os.path.join(base, "hello.txt")


def check_upload_filename_sanitization(base):
    # type: (Text) -> None
    assert upload_filename_to_internal_child_name(u"hello.txt") == "hello.txt"
    assert upload_filename_to_internal_child_name(u"../../secret.txt") == "secret.txt"
    assert upload_filename_to_internal_child_name(u"") is None


class ChunkedReader(object):
    """A reader that deliberately splits a body into small chunks."""

    def __init__(self, body, chunk_size):
        # type: (bytes, int) -> None
        self.body = body
        self.chunk_size = chunk_size
        self.offset = 0

    def read(self, size):
        # type: (int) -> bytes
        if self.offset == len(self.body):
            return b""
        end = min(self.offset + self.chunk_size, self.offset + size, len(self.body))
        chunk = self.body[self.offset:end]
        self.offset = end
        return chunk


def collect_multipart_parts(content_type, body, reader=None):
    # type: (Text, bytes, object) -> List[Tuple[Text, bytes]]
    """Collect ``(filename, content)`` pairs from a multipart body."""
    parts = []
    current_filename = None
    current_chunks = []
    if reader is None:
        reader = io.BytesIO(body)

    for event in parse_multipart_form_data(content_type, iter_body_chunks(reader)):
        if isinstance(event, PartBegin):
            current_filename = event.filename
            current_chunks = []
        elif isinstance(event, PartData):
            current_chunks.append(event.data)
        elif isinstance(event, PartEnd):
            parts.append((current_filename, b"".join(current_chunks)))
            current_filename = None

    return parts


def check_multipart_upload(base):
    # type: (Text) -> None
    boundary = "BOUNDARY"
    content_type = u"multipart/form-data; boundary=" + boundary

    body = b"".join(
        (
            b"--" + boundary.encode("ascii") + b"\r\n",
            b'Content-Disposition: form-data; name="file"; filename="hello.txt"\r\n',
            b"Content-Type: text/plain\r\n",
            b"\r\n",
            b"hello world\r\n",
            b"--" + boundary.encode("ascii") + b"--\r\n",
        )
    )

    parts = collect_multipart_parts(content_type, body)
    assert len(parts) == 1, "expected one uploaded file, got %r" % (parts,)
    filename, content = parts[0]
    assert filename == "hello.txt", "unexpected filename: %r" % (filename,)
    assert content == b"hello world", "unexpected content: %r" % (content,)


def check_large_multipart_upload(base):
    # type: (Text) -> None
    boundary = "BOUNDARY"
    content_type = u"multipart/form-data; boundary=" + boundary
    large_content = b"x" * 200000

    body = b"".join(
        (
            b"--" + boundary.encode("ascii") + b"\r\n",
            b'Content-Disposition: form-data; name="file"; filename="large.bin"\r\n',
            b"Content-Type: application/octet-stream\r\n",
            b"\r\n",
            large_content,
            b"\r\n",
            b"--" + boundary.encode("ascii") + b"--\r\n",
        )
    )

    parts = collect_multipart_parts(content_type, body)
    assert len(parts) == 1, "expected one uploaded file, got %r" % (parts,)
    filename, content = parts[0]
    assert filename == "large.bin", "unexpected filename: %r" % (filename,)
    assert len(content) == len(large_content), "unexpected byte count: %r" % (len(content),)
    assert content == large_content, "unexpected content"


def check_multipart_multiple_parts(base):
    # type: (Text) -> None
    content_type = u"multipart/form-data; boundary=BOUNDARY"

    body = b"".join(
        (
            b"--BOUNDARY\r\n",
            b'Content-Disposition: form-data; name="note"\r\n',
            b"\r\n",
            b"ignored field\r\n",
            b"--BOUNDARY\r\n",
            b'Content-Disposition: form-data; name="file"; filename="a.txt"\r\n',
            b"\r\n",
            b"AAA\r\n",
            b"--BOUNDARY\r\n",
            b'Content-Disposition: form-data; name="file"; filename="b.bin"\r\n',
            b"\r\n",
            b"BBBB\r\n",
            b"--BOUNDARY--\r\n",
        )
    )

    parts = collect_multipart_parts(content_type, body)
    assert parts == [
        ("a.txt", b"AAA"),
        ("b.bin", b"BBBB"),
    ], "unexpected parts: %r" % (parts,)


def check_multipart_empty_file(base):
    # type: (Text) -> None
    content_type = u"multipart/form-data; boundary=BOUNDARY"

    body = b"".join(
        (
            b"--BOUNDARY\r\n",
            b'Content-Disposition: form-data; name="file"; filename="empty.txt"\r\n',
            b"\r\n",
            b"\r\n",
            b"--BOUNDARY--\r\n",
        )
    )

    parts = collect_multipart_parts(content_type, body)
    assert parts == [("empty.txt", b"")], "unexpected parts: %r" % (parts,)


def check_multipart_chunked_reader(base):
    # type: (Text) -> None
    content_type = u"multipart/form-data; boundary=BOUNDARY"
    body = b"".join(
        (
            b"--BOUNDARY\r\n",
            b'Content-Disposition: form-data; name="file"; filename="split.txt"\r\n',
            b"\r\n",
            b"content split across every boundary\r\n",
            b"--BOUNDARY--\r\n",
        )
    )

    parts = collect_multipart_parts(content_type, body, ChunkedReader(body, 3))
    assert parts == [("split.txt", b"content split across every boundary")]


def check_multipart_truncated_body_rejected(base):
    # type: (Text) -> None
    content_type = u"multipart/form-data; boundary=BOUNDARY"
    body = b"".join(
        (
            b"--BOUNDARY\r\n",
            b'Content-Disposition: form-data; name="file"; filename="truncated.txt"\r\n',
            b"\r\n",
            b"partial content",
        )
    )

    try:
        collect_multipart_parts(content_type, body)
    except ValueError:
        pass
    else:
        raise AssertionError("truncated multipart body was accepted")


def main():
    # type: () -> None
    checks = (
        ("format_current_date_time", check_format_date),
        ("html_escape", check_html_escape),
        ("write_error", check_write_error),
        ("write_error HEAD", check_write_error_head),
        ("serve_directory listing", check_directory_listing),
        ("serve_directory HEAD", check_directory_listing_head),
        ("serve_file full download", check_full_download),
        ("serve_file range download", check_range_download),
        ("serve_file range not satisfiable", check_range_not_satisfiable),
        ("serve_file range edge cases", check_range_edge_cases),
        ("serve_file HEAD", check_head_download),
        ("path traversal blocked", check_path_traversal_blocked),
        ("upload filename sanitization", check_upload_filename_sanitization),
        ("multipart upload parsing", check_multipart_upload),
        ("large multipart upload streaming", check_large_multipart_upload),
        ("multipart multiple parts", check_multipart_multiple_parts),
        ("multipart empty file", check_multipart_empty_file),
        ("multipart chunked reader", check_multipart_chunked_reader),
        ("truncated multipart body rejected", check_multipart_truncated_body_rejected),
    )

    base = make_fixture()
    try:
        for name, check in checks:
            check(base)
            print("ok - %s" % (name,))
    finally:
        shutil.rmtree(base)


if __name__ == "__main__":
    main()
