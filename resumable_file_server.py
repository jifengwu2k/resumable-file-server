#!/usr/bin/env python
# coding=utf-8
# Copyright (c) 2026 Jifeng Wu
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
from __future__ import print_function

import argparse
import logging
import os
import posixpath
import socket
import sys

from fspathverbs import Child, Current, Parent, Root, compile_to_fspathverbs
from guess_file_mime_type import guess_file_mime_type
from textcompat import (
    filesystem_str_to_text,
    uri_str_to_text,
    text_to_uri_str
)

if sys.version_info >= (3,):
    from http.server import BaseHTTPRequestHandler, HTTPServer as BaseHTTPServer
    from socketserver import ThreadingMixIn
    from urllib.parse import urlsplit
else:
    from BaseHTTPServer import BaseHTTPRequestHandler, HTTPServer as BaseHTTPServer
    from SocketServer import ThreadingMixIn
    from urlparse import urlsplit

if sys.version_info >= (3, 2):
    from html import escape
else:
    from cgi import escape

if sys.version_info >= (3, 11):
    from email.parser import BytesParser
    from email.policy import default as email_default_policy


    def parse_multipart_form_data(headers, rfile):
        content_type = headers.get('Content-Type')
        content_length = int(headers.get('Content-Length'))
        if not content_type.startswith('multipart/form-data'):
            raise ValueError('Invalid Content-Type for multipart/form-data')

        bytes_to_parse = bytearray()
        bytes_to_parse.extend(b'Content-Type: ')
        bytes_to_parse.extend(content_type.encode())
        bytes_to_parse.extend(b'\r\n\r\n')
        bytes_to_parse.extend(rfile.read(content_length))

        message = BytesParser(policy=email_default_policy).parsebytes(bytes_to_parse)

        results = []
        for part in message.iter_parts():
            disposition = part.get_content_disposition()
            if disposition == 'form-data':
                filename = part.get_filename()
                if filename:
                    results.append((filename, part.get_payload(decode=True)))
        return results
else:
    from cgi import FieldStorage


    def parse_multipart_form_data(headers, rfile):
        content_type = headers.get('Content-Type')
        if not content_type.startswith('multipart/form-data'):
            raise ValueError('Invalid Content-Type for multipart/form-data')

        form = FieldStorage(
            fp=rfile,
            headers=headers,
            environ={
                'REQUEST_METHOD': 'POST',
                'CONTENT_TYPE': content_type,
            }
        )
        results = []
        for field in form.list or []:
            if field.filename:
                results.append((field.filename, field.file.read()))
        return results


def filesystem_user_path_to_internal_path(filesystem_user_path):
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
    internal_child_name = uri_str_to_text(uri_path_segment)

    if internal_child_name in (u'', u'.', u'..'):
        return None
    if u'/' in internal_child_name or u'\\' in internal_child_name or u'\x00' in internal_child_name:
        return None

    return internal_child_name


def http_request_uri_path_to_internal_path(internal_root_directory_path, http_request_uri_path):
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
    normalized_upload_filename = upload_filename.replace('\\', '/')
    child_names = [
        child_name
        for child_name in normalized_upload_filename.split('/')
        if child_name
    ]

    if not child_names:
        return None

    internal_child_name = child_names[-1]
    if internal_child_name in ('.', '..'):
        return None
    if '/' in internal_child_name or '\\' in internal_child_name or '\x00' in internal_child_name:
        return None

    return internal_child_name


def internal_path_to_uri_path(internal_root_directory_path, internal_path):
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


class ResumableFileRequestHandler(BaseHTTPRequestHandler, object):
    __slots__ = ()

    def do_GET(self):
        client_ip, client_port = self.client_address

        request_uri_path = urlsplit(self.path).path
        requested_internal_path = http_request_uri_path_to_internal_path(
            self.server.internal_root_directory_path,
            request_uri_path
        )

        if requested_internal_path is None or not os.path.exists(requested_internal_path):
            self.send_error(404, 'File Not Found: %s' % uri_str_to_text(request_uri_path))
            return

        if os.path.isdir(requested_internal_path):
            directory_uri_path = internal_path_to_uri_path(
                self.server.internal_root_directory_path,
                requested_internal_path
            )
            directory_display_uri_text = uri_str_to_text(directory_uri_path)

            html_line_texts = [
                u'<!DOCTYPE html>',
                u'<html>',
                u'<head>',
                u"<meta charset='utf-8'>",
                u'<title>Directory listing for %s</title>' % directory_display_uri_text,
                u'</head>',
                u'<body>',
                u'<h1>Directory listing for %s</h1>' % directory_display_uri_text,
                u'<hr>',
                u'<ul>',
            ]

            if requested_internal_path != self.server.internal_root_directory_path:
                parent_directory_uri_path = internal_path_to_uri_path(
                    self.server.internal_root_directory_path,
                    os.path.dirname(requested_internal_path)
                )
                html_line_texts.append(u"<li><a href='%s'>../</a></li>" % parent_directory_uri_path)

            internal_child_names = sorted(os.listdir(requested_internal_path))  # type: ignore
            for internal_child_name in internal_child_names:
                child_internal_path = os.path.join(requested_internal_path, internal_child_name)  # type: ignore
                child_text = filesystem_str_to_text(internal_child_name)

                if os.path.isdir(child_internal_path):
                    child_display_text = child_text + u'/'
                    child_uri_path = internal_path_to_uri_path(
                        self.server.internal_root_directory_path,
                        child_internal_path
                    )
                else:
                    child_display_text = child_text
                    child_uri_path = internal_path_to_uri_path(
                        self.server.internal_root_directory_path,
                        child_internal_path
                    )

                html_line_texts.append(
                    u"<li><a href='%s'>%s</a></li>" % (
                        child_uri_path,
                        escape(child_display_text, True)
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

            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.end_headers()
            self.wfile.write(html_page_utf_8_bytes)

            logging.info(
                'Served directory listing for %s to %s:%d',
                directory_display_uri_text,
                client_ip,
                client_port
            )
            return

        file_size = os.path.getsize(requested_internal_path)
        start = 0
        end = file_size - 1

        range_header = self.headers.get('Range')
        if range_header:
            logging.debug('Range request from %s:%d: %s', client_ip, client_port, range_header)
            if not range_header.startswith('bytes=') or '-' not in range_header:
                self.send_error(400, 'Invalid Range Header')
                return

            start_str, end_str = range_header[len('bytes='):].split('-', 1)
            try:
                start = int(start_str) if start_str else 0
                end = int(end_str) if end_str else file_size - 1
            except ValueError:
                self.send_error(400, 'Invalid Range Format')
                return

            if start >= file_size or end >= file_size or start > end:
                self.send_error(416, 'Requested Range Not Satisfiable')
                return

            self.send_response(206)
            self.send_header('Content-Range', 'bytes %d-%d/%d' % (start, end, file_size))
        else:
            self.send_response(200)

        remaining = end - start + 1
        internal_filename = os.path.basename(requested_internal_path)
        filename_text = filesystem_str_to_text(internal_filename)

        self.send_header('Content-Type', guess_file_mime_type(internal_filename))
        self.send_header(
            'Content-Disposition',
            "attachment; filename*=UTF-8''%s" % text_to_uri_str(filename_text)
        )
        self.send_header('Content-Length', str(remaining))
        self.send_header('Accept-Ranges', 'bytes')
        self.end_headers()

        logging.info(
            'Starting download to %s:%d for file %s (%d bytes remaining)',
            client_ip,
            client_port,
            filename_text,
            remaining
        )

        with open(requested_internal_path, 'rb') as file_object:
            file_object.seek(start)

            bytes_sent = 0
            while remaining > 0:
                chunk_size = min(4096, remaining)
                chunk = file_object.read(chunk_size)
                if not chunk:
                    break
                try:
                    self.wfile.write(chunk)
                    self.wfile.flush()
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

    def do_POST(self):
        client_ip, client_port = self.client_address

        request_uri_path = urlsplit(self.path).path
        upload_directory_internal_path = http_request_uri_path_to_internal_path(
            self.server.internal_root_directory_path,
            request_uri_path
        )

        if upload_directory_internal_path is None or not os.path.isdir(upload_directory_internal_path):
            self.send_error(400, 'Invalid upload path')
            return

        upload_directory_uri_path = internal_path_to_uri_path(
            self.server.internal_root_directory_path,
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
            uploaded_files = parse_multipart_form_data(self.headers, self.rfile)
        except Exception as error:
            self.send_error(400, 'Invalid multipart/form-data: %s' % str(error))
            return

        if not uploaded_files:
            self.send_error(400, 'No files were uploaded')
            return

        pending_uploads = []
        for upload_filename, file_data in uploaded_files:
            internal_child_name = upload_filename_to_internal_child_name(upload_filename)
            if internal_child_name is None:
                self.send_error(400, 'Invalid uploaded filename: %s' % upload_filename)
                return
            pending_uploads.append((upload_filename, internal_child_name, file_data))

        uploaded_count = 0
        for upload_filename, internal_child_name, file_data in pending_uploads:
            destination_internal_path = os.path.join(
                upload_directory_internal_path,
                internal_child_name
            )  # type: ignore

            with open(destination_internal_path, 'wb') as file_object:
                file_object.write(file_data)

            uploaded_count += 1
            logging.info(
                'Uploaded file %s saved (%d bytes) from %s:%d',
                upload_filename,
                len(file_data),
                client_ip,
                client_port
            )

        self.send_response(303)
        self.send_header('Location', upload_directory_uri_path)
        self.end_headers()
        logging.info('Upload completed for %s:%d (%d files)', client_ip, client_port, uploaded_count)


class ThreadingHTTPServer(ThreadingMixIn, BaseHTTPServer, object):
    __slots__ = ()


class ResumableFileServer(ThreadingHTTPServer, object):
    __slots__ = ('internal_root_directory_path',)

    def __init__(self, server_address, request_handler_class, internal_root_directory_path):
        ThreadingHTTPServer.__init__(self, server_address, request_handler_class)
        self.internal_root_directory_path = os.path.realpath(internal_root_directory_path)


def run(host, port, internal_root_directory_path):
    server_address = (host, port)
    httpd = ResumableFileServer(
        server_address,
        ResumableFileRequestHandler,
        internal_root_directory_path
    )  # type: ignore

    logging.info('Serving HTTP on %s port %d ...', host, port)
    logging.info('Serving files from %s', httpd.internal_root_directory_path)
    httpd.serve_forever()


def main():
    logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

    parser = argparse.ArgumentParser(description='Start an HTTP file server with resumable upload/download support.')
    parser.add_argument('port', type=int, nargs='?', default=8000, help='Port to listen on (default: 8000)')
    parser.add_argument('--host', type=str, default='localhost', help='Host/IP address to bind (default: localhost)')
    parser.add_argument('-r', '--root', type=str, default='.', help='Filesystem path to the root directory to serve/store files from')
    args = parser.parse_args()

    filesystem_root_user_path = args.root
    internal_root_directory_path = filesystem_user_path_to_internal_path(filesystem_root_user_path)

    if not os.path.isdir(internal_root_directory_path):
        logging.error('Error: Root directory %s does not exist.' % filesystem_root_user_path)
        sys.exit(1)

    run(args.host, args.port, internal_root_directory_path)


if __name__ == '__main__':
    main()
