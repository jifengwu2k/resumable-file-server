# Copyright (c) 2026 Jifeng Wu
# Licensed under the MIT License. See LICENSE file in the project root
# for full license information.
from __future__ import print_function

import argparse
import errno
import logging
import multiprocessing
import os
import posixpath
import socket
import sys
import time

from typing import (
    BinaryIO,
    Dict,
    Iterator,
    List,
    Optional,
    Text,
    Tuple,
)

from fspathverbs import (
    Child,
    Current,
    Parent,
    Root,
    compile_to_fspathverbs,
)
from guess_file_mime_type import guess_file_mime_type
from httppackets.http_1_1_parser import (
    BodyReader,
    Decision,
    ParserError,
    parse_http_1_1_requests,
)
from httppackets.http_1_1_serializer import serialize_http_1_1_response
from minimal_thread_pool import MinimalThreadPool
from parse_multipart_form_data import (
    MultipartEvent,
    PartBegin,
    PartData,
    PartEnd,
    parse_multipart_form_data,
)
from textcompat import (
    filesystem_str_to_text,
    text_to_filesystem_str,
    text_to_uri_str,
    text_to_utf_8_str,
    uri_str_to_text,
)

if sys.version_info >= (3,):
    from queue import Empty
    from urllib.parse import urlsplit
else:
    from Queue import Empty
    from urlparse import urlsplit


DEFAULT_BIND = "localhost"
DEFAULT_PORT = 8000
DEFAULT_THREADS = multiprocessing.cpu_count() or 1
SERVER = "resumable-file-server"
BODY_CHUNK = 65536  # 64 KiB read buffer for multipart request bodies
DOWNLOAD_CHUNK = 65536  # 64 KiB read/write buffer for downloads

ERROR_MESSAGES = {
    400: u"Bad request",
    403: u"Forbidden",
    404: u"Not found",
    405: u"Method not allowed",
    416: u"Requested Range Not Satisfiable",
    500: u"Internal server error",
    501: u"Not implemented",
    503: u"Service unavailable",
}  # type: Dict[int, Text]


HTML_ESCAPES = {
    u"&": u"&amp;",
    u"<": u"&lt;",
    u">": u"&gt;",
    u'"': u"&quot;",
    u"'": u"&#39;",
}


def html_escape(text):
    # type: (Text) -> Text
    """Escape *text* for safe inclusion in HTML body content."""
    result = []
    for ch in text:
        result.append(HTML_ESCAPES.get(ch, ch))
    return u"".join(result)


def close_ignore_broken_pipe(handle):
    # type: (...) -> None
    """Close *handle*, ignoring broken pipe errors."""
    try:
        handle.close()
    except EnvironmentError as e:
        if getattr(e, 'errno', None) != errno.EPIPE:
            raise


WEEKDAY_NAMES = (
    "Mon",
    "Tue",
    "Wed",
    "Thu",
    "Fri",
    "Sat",
    "Sun",
)
MONTH_NAMES = (
    "Jan",
    "Feb",
    "Mar",
    "Apr",
    "May",
    "Jun",
    "Jul",
    "Aug",
    "Sep",
    "Oct",
    "Nov",
    "Dec",
)


def format_current_date_time():
    # type: () -> Text
    """Return the current time as an HTTP-date string."""
    now = time.gmtime(time.time())
    return "%s, %02d %s %04d %02d:%02d:%02d GMT" % (
        WEEKDAY_NAMES[now.tm_wday],
        now.tm_mday,
        MONTH_NAMES[now.tm_mon - 1],
        now.tm_year,
        now.tm_hour,
        now.tm_min,
        now.tm_sec,
    )


def base_headers():
    # type: () -> Dict[Text, List[Text]]
    """Return the headers shared by every response."""
    return {
        "connection": ["close"],
        "date": [format_current_date_time()],
        "server": [SERVER],
    }


def write_error(stream, code, method="GET", extra_headers=None):
    # type: (BinaryIO, int, Text, Optional[Dict[Text, List[Text]]]) -> None
    """Write an HTTP error response to *stream*.

    For HEAD requests the body is suppressed, but Content-Length still
    advertises the size a GET response would have.
    """
    message = ERROR_MESSAGES.get(code, u"Unknown error")

    body = u'\n'.join(
        (
            u'<!DOCTYPE HTML>',
            u'<html>',
            u'<head>',
            u'<meta charset="utf-8">',
            u'<title>%d %s</title>' % (code, message),
            u'</head>',
            u'<body>',
            u'<h1>%d %s</h1>' % (code, message),
            u'</body>',
            u'</html>',
        )
    )

    body_bytes = body.encode("utf-8")

    headers = base_headers()
    headers["content-type"] = ["text/html; charset=utf-8"]
    if extra_headers:
        headers.update(extra_headers)

    if method == u"HEAD":
        headers["content-length"] = [str(len(body_bytes))]
        serialize_http_1_1_response(
            stream,
            status_code=code,
            reason=message,
            headers=headers,
            body=None,
        )
    else:
        serialize_http_1_1_response(
            stream,
            status_code=code,
            reason=message,
            headers=headers,
            body=body_bytes,
        )


def filesystem_user_path_to_internal_path(filesystem_user_path):
    # type: (Text) -> Text
    filesystem_path_verbs = compile_to_fspathverbs(filesystem_user_path, os.path.split)
    current_internal_path = os.getcwd()

    for filesystem_path_verb in filesystem_path_verbs:
        if isinstance(filesystem_path_verb, Root):
            current_internal_path = filesystem_path_verb.root
            continue
        if isinstance(filesystem_path_verb, Current):
            continue
        if isinstance(filesystem_path_verb, Parent):
            current_internal_path = os.path.dirname(current_internal_path)
            continue
        if isinstance(filesystem_path_verb, Child):
            current_internal_path = os.path.join(current_internal_path, filesystem_path_verb.child)
            continue
        raise ValueError('Unsupported filesystem path verb: %r' % (filesystem_path_verb,))

    return os.path.realpath(current_internal_path)


def uri_path_segment_to_internal_child_name(uri_path_segment):
    # type: (Text) -> Optional[Text]
    internal_child_name = uri_str_to_text(uri_path_segment)

    if internal_child_name in (u'', u'.', u'..'):
        return None
    if u'/' in internal_child_name or u'\\' in internal_child_name or u'\x00' in internal_child_name:
        return None

    return text_to_filesystem_str(internal_child_name)


def http_request_uri_path_to_internal_path(internal_root_directory_path, http_request_uri_path):
    # type: (Text, Text) -> Optional[Text]
    http_path_verbs = compile_to_fspathverbs(http_request_uri_path, posixpath.split)
    current_internal_path = internal_root_directory_path

    for http_path_verb in http_path_verbs:
        if isinstance(http_path_verb, Root):
            current_internal_path = internal_root_directory_path
            continue
        if isinstance(http_path_verb, Current):
            continue
        if isinstance(http_path_verb, Parent):
            if current_internal_path == internal_root_directory_path:
                return None
            current_internal_path = os.path.dirname(current_internal_path)
            continue
        if isinstance(http_path_verb, Child):
            internal_child_name = uri_path_segment_to_internal_child_name(http_path_verb.child)
            if internal_child_name is None:
                return None
            current_internal_path = os.path.join(current_internal_path, internal_child_name)
            continue
        raise ValueError('Unsupported HTTP path verb: %r' % (http_path_verb,))

    return current_internal_path


def upload_filename_to_internal_child_name(upload_filename):
    # type: (Text) -> Optional[Text]
    normalized_upload_filename = upload_filename.replace(u'\\', u'/')
    child_names = [
        child_name
        for child_name in normalized_upload_filename.split(u'/')
        if child_name
    ]

    if not child_names:
        return None

    internal_child_name = child_names[-1]
    if internal_child_name in (u'.', u'..'):
        return None
    if u'/' in internal_child_name or u'\\' in internal_child_name or u'\x00' in internal_child_name:
        return None

    return text_to_filesystem_str(internal_child_name)


def internal_path_to_uri_path(internal_root_directory_path, internal_path):
    # type: (Text, Text) -> Text
    relative_internal_path = os.path.relpath(
        os.path.normcase(internal_path),
        os.path.normcase(internal_root_directory_path)
    )

    if relative_internal_path == '.':
        return '/'

    relative_internal_child_names = [
        internal_child_name
        for internal_child_name in relative_internal_path.split(os.sep)
        if internal_child_name not in ('', '.')
    ]
    encoded_relative_uri_segments = [
        text_to_uri_str(filesystem_str_to_text(internal_child_name))
        for internal_child_name in relative_internal_child_names
    ]
    return '/' + '/'.join(encoded_relative_uri_segments)


def iter_body_chunks(reader):
    # type: (BodyReader) -> Iterator[bytes]
    """Yield non-empty request-body chunks from *reader* until EOF."""
    while True:
        chunk = reader.read(BODY_CHUNK)
        if not chunk:
            return
        # ``httppackets`` body readers may return ``bytearray`` (notably on
        # Python 2), but the multipart parser requires ``bytes`` chunks and
        # ``bytes.join`` rejects ``bytearray`` items under Python 2.
        yield bytes(chunk)


def serve_directory(wf, method, directory_internal_path, internal_root_directory_path, client_ip, client_port):
    # type: (BinaryIO, Text, Text, Text, Text, int) -> None
    directory_uri_path = internal_path_to_uri_path(
        internal_root_directory_path,
        directory_internal_path
    )
    directory_display_uri_text = uri_str_to_text(directory_uri_path)
    escaped_display_uri_text = html_escape(directory_display_uri_text)

    html_line_texts = [
        u'<!DOCTYPE html>',
        u'<html>',
        u'<head>',
        u"<meta charset='utf-8'>",
        u'<title>Directory listing for %s</title>' % escaped_display_uri_text,
        u'</head>',
        u'<body>',
        u'<h1>Directory listing for %s</h1>' % escaped_display_uri_text,
        u'<hr>',
        u'<ul>',
    ]

    if directory_internal_path != internal_root_directory_path:
        parent_directory_uri_path = internal_path_to_uri_path(
            internal_root_directory_path,
            os.path.dirname(directory_internal_path)
        )
        html_line_texts.append(u"<li><a href='%s'>../</a></li>" % parent_directory_uri_path)

    internal_child_names = sorted(os.listdir(directory_internal_path))
    for internal_child_name in internal_child_names:
        child_internal_path = os.path.join(directory_internal_path, internal_child_name)
        child_text = filesystem_str_to_text(internal_child_name)

        if os.path.isdir(child_internal_path):
            child_display_text = child_text + u'/'
        else:
            child_display_text = child_text

        child_uri_path = internal_path_to_uri_path(
            internal_root_directory_path,
            child_internal_path
        )

        html_line_texts.append(
            u"<li><a href='%s'>%s</a></li>" % (
                child_uri_path,
                html_escape(child_display_text)
            )
        )

    html_line_texts += [
        u'</ul>',
        u'<hr>',
        u"<form method='POST' enctype='multipart/form-data'>",
        u"<input type='file' name='file' multiple>",
        u"<input type='submit' value='Upload'>",
        u'</form>',
        u'</body>',
        u'</html>'
    ]

    html_page_utf_8_bytes = u'\n'.join(html_line_texts).encode('utf-8')

    headers = base_headers()
    headers["content-type"] = ["text/html; charset=utf-8"]

    if method == u"HEAD":
        headers["content-length"] = [str(len(html_page_utf_8_bytes))]
        serialize_http_1_1_response(
            wf,
            status_code=200,
            reason=u"OK",
            headers=headers,
            body=None,
        )
    else:
        serialize_http_1_1_response(
            wf,
            status_code=200,
            reason=u"OK",
            headers=headers,
            body=html_page_utf_8_bytes,
        )

    logging.info(
        'Served directory listing for %s to %s:%d',
        directory_display_uri_text,
        client_ip,
        client_port
    )


def parse_range_header(range_header, file_size):
    # type: (Text, int) -> Optional[Tuple[int, int]]
    """Parse one RFC 7233 byte-range-specification.

    Returns ``(start, end)`` for a satisfiable range, ``None`` for a valid but
    unsatisfiable one, and raises ``ValueError`` for a malformed or unsupported
    range set. Multiple ranges are deliberately unsupported by this server.
    """
    if not range_header.startswith(u'bytes='):
        raise ValueError('malformed Range header')

    range_spec = range_header[len(u'bytes='):]
    if range_spec.count(u'-') != 1 or u',' in range_spec:
        raise ValueError('malformed or unsupported Range header')

    start_text, end_text = range_spec.split(u'-')
    if start_text:
        if not start_text.isdigit() or (end_text and not end_text.isdigit()):
            raise ValueError('malformed Range header')

        start = int(start_text)
        if start >= file_size:
            return None
        # An end beyond EOF is satisfiable and is truncated to EOF.
        end = min(int(end_text), file_size - 1) if end_text else file_size - 1
        return (start, end) if start <= end else None

    # A suffix range (``bytes=-N``) selects the last N bytes, rather than the
    # first N bytes. ``bytes=-`` is not a valid byte-range-specification.
    if not end_text or not end_text.isdigit():
        raise ValueError('malformed Range header')
    suffix_length = int(end_text)
    if suffix_length == 0 or file_size == 0:
        return None
    return max(file_size - suffix_length, 0), file_size - 1


def serve_file(wf, method, file_internal_path, range_header, client_ip, client_port):
    # type: (BinaryIO, Text, Text, Optional[Text], Text, int) -> None
    file_size = os.path.getsize(file_internal_path)
    start = 0
    end = file_size - 1
    status_code = 200
    reason = u"OK"

    if range_header:
        logging.debug('Range request from %s:%d: %s', client_ip, client_port, range_header)
        try:
            parsed_range = parse_range_header(range_header, file_size)
        except ValueError:
            write_error(wf, 400, method)
            return
        if parsed_range is None:
            write_error(
                wf,
                416,
                method,
                {"content-range": ["bytes */%d" % file_size]}
            )
            return
        start, end = parsed_range
        status_code = 206
        reason = u"Partial Content"

    remaining = end - start + 1

    internal_filename = os.path.basename(file_internal_path)
    filename_text = filesystem_str_to_text(internal_filename)

    headers = base_headers()
    headers["content-type"] = [guess_file_mime_type(internal_filename)]
    headers["content-disposition"] = ["attachment; filename*=UTF-8''%s" % text_to_uri_str(filename_text)]
    headers["content-length"] = [str(remaining)]
    headers["accept-ranges"] = ["bytes"]

    if status_code == 206:
        headers["content-range"] = ["bytes %d-%d/%d" % (start, end, file_size)]

    serialize_http_1_1_response(
        wf,
        status_code=status_code,
        reason=reason,
        headers=headers,
        body=None,
    )

    if method == u"HEAD":
        return

    logging.info(
        'Starting download to %s:%d for file %s (%d bytes remaining)',
        client_ip,
        client_port,
        filename_text,
        remaining
    )

    with open(file_internal_path, 'rb') as file_object:
        file_object.seek(start)

        bytes_sent = 0
        while remaining > 0:
            chunk_size = min(DOWNLOAD_CHUNK, remaining)
            chunk = file_object.read(chunk_size)
            if not chunk:
                break
            try:
                wf.write(chunk)
                wf.flush()
            except socket.error:
                logging.warning(
                    'Client %s:%d disconnected while downloading %s. Bytes sent: %d.',
                    client_ip,
                    client_port,
                    filename_text,
                    bytes_sent
                )
                break
            bytes_sent += len(chunk)
            remaining -= len(chunk)

    logging.info('Completed download to %s:%d for file %s', client_ip, client_port, filename_text)


def write_uploaded_parts(events, upload_directory_internal_path, client_ip, client_port):
    # type: (Iterator[MultipartEvent], Text, Text, int) -> int
    """Write uploaded multipart parts to disk; return the number saved."""
    uploaded_count = 0
    current_file = None  # type: Optional[BinaryIO]
    current_filename = None  # type: Optional[Text]
    bytes_written = 0

    for event in events:
        if isinstance(event, PartBegin):
            internal_child_name = upload_filename_to_internal_child_name(event.filename)
            if internal_child_name is None:
                raise ValueError('Invalid uploaded filename: %s' % (event.filename,))

            destination_path = os.path.join(upload_directory_internal_path, internal_child_name)
            current_file = open(destination_path, 'wb')
            current_filename = event.filename
            bytes_written = 0
        elif isinstance(event, PartData):
            current_file.write(event.data)
            bytes_written += len(event.data)
        elif isinstance(event, PartEnd):
            current_file.close()
            current_file = None
            uploaded_count += 1
            logging.info(
                'Uploaded file %s saved (%d bytes) from %s:%d',
                current_filename,
                bytes_written,
                client_ip,
                client_port
            )
            current_filename = None

    return uploaded_count


def handle_upload_body(
        wf,
        content_type,
        body_reader,
        upload_directory_internal_path,
        internal_root_directory_path,
        client_ip,
        client_port
):
    # type: (BinaryIO, Text, BodyReader, Text, Text, Text, int) -> None
    upload_directory_uri_path = internal_path_to_uri_path(
        internal_root_directory_path,
        upload_directory_internal_path
    )
    upload_directory_display_uri_text = uri_str_to_text(upload_directory_uri_path)

    logging.info(
        'Starting upload to %s from %s:%d',
        upload_directory_display_uri_text,
        client_ip,
        client_port
    )

    try:
        uploaded_count = write_uploaded_parts(
            parse_multipart_form_data(content_type, iter_body_chunks(body_reader)),
            upload_directory_internal_path,
            client_ip,
            client_port
        )
    except ValueError:
        write_error(wf, 400, u"POST")
        return

    if uploaded_count == 0:
        write_error(wf, 400, u"POST")
        return

    headers = base_headers()
    headers["location"] = [upload_directory_uri_path]
    headers["content-length"] = ["0"]

    serialize_http_1_1_response(
        wf,
        status_code=303,
        reason=u"See Other",
        headers=headers,
        body=None,
    )
    wf.flush()

    logging.info('Upload completed for %s:%d (%d files)', client_ip, client_port, uploaded_count)


class ConnectionHandler(object):
    """Serve a single TCP connection."""

    def __init__(self, sock, internal_root_directory_path):
        # type: (socket.socket, Text) -> None
        self.sock = sock
        self.internal_root_directory_path = internal_root_directory_path
        self.rf = None  # type: Optional[BinaryIO]
        self.wf = None  # type: Optional[BinaryIO]
        self.method = u"GET"
        self.upload = None  # type: Optional[Tuple[Text, Text]]

    def close(self):
        # type: () -> None
        for handle in (self.rf, self.wf, self.sock):
            if handle is not None:
                close_ignore_broken_pipe(handle)

    def on_headers(self, method, target, headers):
        # type: (Text, Text, Dict[Text, List[Text]]) -> Decision
        self.method = method

        if method not in (u"GET", u"HEAD", u"POST"):
            write_error(self.wf, 501, method)
            return Decision.ABORT

        request_uri_path = urlsplit(text_to_utf_8_str(target)).path
        requested_internal_path = http_request_uri_path_to_internal_path(
            self.internal_root_directory_path,
            request_uri_path
        )

        if method in (u"GET", u"HEAD"):
            if requested_internal_path is None or not os.path.exists(requested_internal_path):
                write_error(self.wf, 404, method)
            elif os.path.isdir(requested_internal_path):
                serve_directory(
                    self.wf,
                    method,
                    requested_internal_path,
                    self.internal_root_directory_path,
                    self.client_ip,
                    self.client_port
                )
            else:
                range_values = headers.get(u"range")
                range_header = range_values[0] if range_values else None
                serve_file(
                    self.wf,
                    method,
                    requested_internal_path,
                    range_header,
                    self.client_ip,
                    self.client_port
                )
            return Decision.ABORT

        # POST: upload one or more files to the target directory.
        if requested_internal_path is None or not os.path.isdir(requested_internal_path):
            write_error(self.wf, 400, method)
            return Decision.ABORT

        content_type_values = headers.get(u"content-type")
        content_type = content_type_values[0] if content_type_values else u""
        if not content_type.startswith(u"multipart/form-data"):
            write_error(self.wf, 400, method)
            return Decision.ABORT

        # Tell the client to send the body when it uses ``Expect: 100-continue``.
        expect_values = headers.get(u"expect")
        if expect_values and expect_values[0].strip().lower() == u"100-continue":
            serialize_http_1_1_response(
                self.wf,
                status_code=100,
                reason=u"Continue",
                headers={},
                body=None,
            )
            self.wf.flush()

        self.upload = (content_type, requested_internal_path)
        return Decision.READ_BODY

    def on_body(self, reader):
        # type: (BodyReader) -> None
        content_type, upload_directory_internal_path = self.upload
        handle_upload_body(
            self.wf,
            content_type,
            reader,
            upload_directory_internal_path,
            self.internal_root_directory_path,
            self.client_ip,
            self.client_port
        )

    def handle(self):
        # type: () -> None
        try:
            self.rf = self.sock.makefile("rb")
            self.wf = self.sock.makefile("wb")
            self.client_ip, self.client_port = self.sock.getpeername()

            try:
                parse_http_1_1_requests(
                    self.rf,
                    on_headers=self.on_headers,
                    on_body=self.on_body
                )
            except ParserError:
                write_error(self.wf, 400, self.method)
            except Exception as error:
                logging.exception(error)
                write_error(self.wf, 500, self.method)
        except Exception:
            # A single bad connection must never kill a worker thread.
            logging.exception("Unexpected error while serving a connection")
        finally:
            self.close()


def handle_connection(sock, internal_root_directory_path):
    # type: (socket.socket, Text) -> None
    """Handle a single TCP connection: parse request, serve response."""
    ConnectionHandler(sock, internal_root_directory_path).handle()


def drain_ready_results(pool):
    # type: (MinimalThreadPool) -> None
    """Discard completed-task results so the pool's result queue stays bounded."""
    while True:
        try:
            pool.result_queue.get_nowait()
        except Empty:
            return


def main():
    # type: () -> None
    parser = argparse.ArgumentParser(
        description='Start an HTTP file server with resumable download/upload support.'
    )
    parser.add_argument(
        'port',
        type=int,
        nargs='?',
        default=DEFAULT_PORT,
        help='Port to listen on (default: %d)' % DEFAULT_PORT
    )
    parser.add_argument(
        '--host',
        type=str,
        default=DEFAULT_BIND,
        help='Host/IP address to bind (default: %s)' % DEFAULT_BIND
    )
    parser.add_argument(
        '-r',
        '--root',
        type=str,
        default='.',
        help='Filesystem path to the root directory to serve/store files from'
    )
    parser.add_argument(
        '--threads',
        type=int,
        default=DEFAULT_THREADS,
        help='Number of worker threads (default: %d)' % DEFAULT_THREADS
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

    filesystem_root_user_path = args.root
    internal_root_directory_path = filesystem_user_path_to_internal_path(filesystem_root_user_path)

    if not os.path.isdir(internal_root_directory_path):
        logging.error('Error: Root directory %s does not exist.' % filesystem_root_user_path)
        sys.exit(1)

    server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server_sock.bind((args.host, args.port))
    server_sock.listen(128)

    pool = MinimalThreadPool(args.threads)

    logging.info('Serving HTTP on %s port %d ...', args.host, args.port)
    logging.info('Serving files from %s', internal_root_directory_path)

    try:
        while True:
            client_sock, client_addr = server_sock.accept()
            pool.add_task(handle_connection, client_sock, internal_root_directory_path)
            drain_ready_results(pool)
    except KeyboardInterrupt:
        logging.info('Keyboard interrupt received, shutting down.')
    finally:
        server_sock.close()


if __name__ == '__main__':
    main()
