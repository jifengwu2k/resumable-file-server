#!/usr/bin/env python
import argparse
import logging
import mimetypes
import os
import os.path
import re
import socket
import sys

from strcompat import utf_8_string_to_unicode, unicode_to_utf_8_string, unicode_to_uri_string, uri_string_to_unicode, unicode_to_filesystem_string, filesystem_string_to_unicode

if sys.version_info >= (3,):
    import html

    from http.server import BaseHTTPRequestHandler, HTTPServer as BaseHTTPServer
    from socketserver import ThreadingMixIn

    html_escape = html.escape
else:
    import cgi

    from BaseHTTPServer import BaseHTTPRequestHandler, HTTPServer as BaseHTTPServer
    from SocketServer import ThreadingMixIn

    html_escape = cgi.escape

if sys.version_info >= (3, 11):
    from email.parser import BytesParser
    from email.policy import default as email_default_policy

    def parse_multipart_form_data(handler):
        """
        Parse multipart/form-data from the request handler and return a list of (filename, file_data) tuples.
        Compatible with both legacy (cgi) and modern (email.parser) approaches.
        """
        content_type = handler.headers.get('Content-Type') if hasattr(handler.headers, 'get') else handler.headers.getheader('Content-Type')
        content_length = int(handler.headers.get('Content-Length') or handler.headers.getheader('Content-Length', 0))
        if not content_type.startswith("multipart/form-data"):
            raise ValueError("Invalid Content-Type for multipart/form-data")
        
        # Modern parsing using email.parser (Python 3.11+)
        body = handler.rfile.read(content_length)

        # email.parser expects full multipart MIME message
        message = BytesParser(policy=email_default_policy).parsebytes(
            b"Content-Type: " + content_type.encode() + b"\r\n\r\n" + body
        )

        results = []
        for part in message.iter_parts():
            disposition = part.get_content_disposition()
            if disposition == "form-data":
                filename = part.get_filename()
                if filename:
                    results.append((filename, part.get_payload(decode=True)))
        return results
else:
    import cgi
    
    def parse_multipart_form_data(handler):
        """
        Parse multipart/form-data from the request handler and return a list of (filename, file_data) tuples.
        Compatible with both legacy (cgi) and modern (email.parser) approaches.
        """
        content_type = handler.headers.get('Content-Type') if hasattr(handler.headers, 'get') else handler.headers.getheader('Content-Type')
        if not content_type.startswith("multipart/form-data"):
            raise ValueError("Invalid Content-Type for multipart/form-data")
    
        form = cgi.FieldStorage(fp=handler.rfile, headers=handler.headers, environ={
            'REQUEST_METHOD': 'POST',
            'CONTENT_TYPE': content_type,
        })
        results = []
        for field in form.list or []:
            if field.filename:
                results.append((field.filename, field.file.read()))
        return results

if sys.version_info >= (3, 13):
    def guess_mime_type(filename):
        mimetype, _ = mimetypes.guess_file_type(os.path.basename(filename))
        return mimetype or 'application/octet-stream'
else:
    def guess_mime_type(filename):
        mimetype, _ = mimetypes.guess_type(os.path.basename(filename))
        return mimetype or 'application/octet-stream'


# unicode_path_components <-> uri_path
def unicode_path_components_to_uri_path(unicode_path_components, force_directory=False):
    if not unicode_path_components: return '/'
    else:
        uri_path_components = [unicode_to_uri_string(unicode_path_component) for unicode_path_component in unicode_path_components]
        uri_path_components[0] = '/' + uri_path_components[0]
        if force_directory:
            uri_path_components[-1] = uri_path_components[-1] + '/'
        return '/'.join(uri_path_components)


def uri_path_to_unicode_path_components(uri_path):
    return [uri_string_to_unicode(component) for component in uri_path.split('/') if component]


# unicode_path_components -> filesystem_path (with check)
def unicode_path_components_to_filesystem_path(root_directory_path, unicode_path_components):
    filesystem_path_components = map(unicode_to_filesystem_string, unicode_path_components)

    # realpath ensures symlinks are resolved to prevent path traversal
    absolute_file_path = os.path.realpath(
        os.path.join(root_directory_path, *filesystem_path_components)  # type: ignore
    )

    absolute_root_directory_path = os.path.realpath(root_directory_path)

    if absolute_file_path.startswith(absolute_root_directory_path):
        return absolute_file_path
    else:
        return None


class ResumableFileRequestHandler(BaseHTTPRequestHandler):
    """
    HTTP request handler that supports:
    - GET with Range (resumable download)
    - POST multipart (standard upload)
    """
    def do_GET(self):
        client_ip, client_port = self.client_address

        unicode_uri_path = uri_string_to_unicode(self.path)
        unicode_path_comps = uri_path_to_unicode_path_components(self.path)
        filesystem_path = unicode_path_components_to_filesystem_path(self.server.root_directory, unicode_path_comps)

        # If path is invalid -> 404
        if filesystem_path is None or not os.path.exists(filesystem_path):
            self.send_error(404, "File Not Found: %s" % unicode_uri_path)
        
        # If path is a directory -> generate an HTML listing with an upload form
        elif os.path.isdir(filesystem_path):
            unicode_html_lines = [
                u"<!DOCTYPE html>",
                u"<html>",
                u"<head><meta charset='utf-8'><title>Directory listing for %s</title></head>" % unicode_uri_path,
                u"<body>",
                u"<h1>Directory listing for %s</h1>" % unicode_uri_path,
                u"<hr>",
                u"<ul>",
            ]

            # Add link to parent directory if not at root
            if unicode_path_comps:
                parent_directory_uri_path = unicode_path_components_to_uri_path(unicode_path_comps[:-1], True)
                unicode_html_lines.append(u"<li><a href='%s'>../</a></li>" % parent_directory_uri_path)

            entries = sorted(os.listdir(filesystem_path)) # type: ignore

            for entry in entries:
                entry_path = os.path.join(filesystem_path, entry) # type: ignore

                unicode_entry = filesystem_string_to_unicode(entry)

                if os.path.isdir(entry_path):
                    unicode_displayname = unicode_entry + u"/"
                    entry_uri_path = unicode_path_components_to_uri_path(unicode_path_comps + [unicode_entry + '/'])
                elif os.path.islink(entry_path):
                    unicode_displayname = unicode_entry + u"@"
                    entry_uri_path = unicode_path_components_to_uri_path(unicode_path_comps + [unicode_entry + '@'])
                else:
                    unicode_displayname = unicode_entry
                    entry_uri_path = unicode_path_components_to_uri_path(unicode_path_comps + [unicode_entry])

                unicode_html_lines.append(u"<li><a href='%s'>%s</a></li>" % (entry_uri_path, html_escape(unicode_displayname)))

            unicode_html_lines += [
                u"</ul>",
                u"<hr>",
                u"<form method='POST' enctype='multipart/form-data'>",
                u"<input type='file' name='file'>",
                u"<input type='submit' value='Upload'>",
                u"</form>",
                u"</body>",
                u"</html>"
            ]

            unicode_html_page = u'\n'.join(unicode_html_lines)

            html_page_utf_8_bytes = unicode_html_page.encode('utf-8')

            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(html_page_utf_8_bytes)

            logging.info("Served directory listing for %s to %s:%d", unicode_uri_path, client_ip, client_port)

        # If path is a file -> serve it with resumable download
        else:
            file_size = os.path.getsize(filesystem_path)
            start = 0
            end = file_size - 1

            range_header = self.headers.get('Range') if hasattr(self.headers, 'get') else self.headers.getheader('Range')
            if range_header:
                logging.debug("Range request from %s:%d: %s", client_ip, client_port, range_header)
                if not range_header.startswith('bytes=') or '-' not in range_header:
                    self.send_error(400, "Invalid Range Header")
                    return

                start_str, end_str = range_header[len('bytes='):].split('-', 1)
                try:
                    start = int(start_str) if start_str else 0
                    end = int(end_str) if end_str else file_size - 1
                except ValueError:
                    self.send_error(400, "Invalid Range Format")
                    return

                if start >= file_size or end >= file_size or start > end:
                    self.send_error(416, "Requested Range Not Satisfiable")
                    return

                self.send_response(206)
                self.send_header('Content-Range', 'bytes %d-%d/%d' % (start, end, file_size))
            else:
                self.send_response(200)

            remaining = end - start + 1

            filesystem_filename = os.path.basename(filesystem_path)
            unicode_filename = filesystem_string_to_unicode(filesystem_filename)

            self.send_header('Content-Type', guess_mime_type(filesystem_filename))
            self.send_header('Content-Disposition', "attachment; filename*=UTF-8''%s" % unicode_to_uri_string(unicode_filename))
            self.send_header('Content-Length', str(remaining))
            self.send_header('Accept-Ranges', 'bytes')
            self.end_headers()

            logging.info("Starting download to %s:%d for file %s (%d bytes remaining)", client_ip, client_port, unicode_filename, remaining)

            with open(filesystem_path, 'rb') as f:
                f.seek(start)
                
                bytes_sent = 0
                while remaining > 0:
                    chunk_size = min(4096, remaining)
                    chunk = f.read(chunk_size)
                    if not chunk:
                        break
                    try:
                        self.wfile.write(chunk)
                        self.wfile.flush()
                    except socket.error as e:
                        client_ip, client_port = self.client_address
                        logging.warning('Client %s:%d disconnected while downloading %s. Bytes sent: %d.' % (client_ip, client_port, unicode_filename, bytes_sent))
                        break
                    bytes_sent += len(chunk)
                    remaining -= len(chunk)
            
            logging.info("Completed download to %s:%d for file %s", client_ip, client_port, unicode_filename)

    def do_POST(self):
        client_ip, client_port = self.client_address

        unicode_uri_path = uri_string_to_unicode(self.path)
        unicode_path_comps = uri_path_to_unicode_path_components(self.path)
        filesystem_upload_path = unicode_path_components_to_filesystem_path(self.server.root_directory, unicode_path_comps)

        logging.info("Starting upload to %s from %s:%d", unicode_uri_path, client_ip, client_port)

        if filesystem_upload_path is None or not os.path.isdir(filesystem_upload_path):
            self.send_error(400, "Invalid upload path")
            return

        try:
            files = parse_multipart_form_data(self)
        except Exception as e:
            self.send_error(400, "Invalid multipart/form-data: %s" % str(e))
            return

        for utf_8_filename, filedata in files:
            unicode_filename = utf_8_string_to_unicode(utf_8_filename)
            filesystem_filename = unicode_to_filesystem_string(unicode_filename)

            filesystem_destination_path = os.path.join(filesystem_upload_path, os.path.basename(filesystem_filename)) # type: ignore

            with open(filesystem_destination_path, 'wb') as f:
                f.write(filedata)
            
            logging.info("Uploaded file %s saved (%d bytes) from %s:%d", unicode_filename, len(filedata), client_ip, client_port)

        self.send_response(303)
        self.send_header("Location", self.path)
        self.end_headers()
        logging.info("Upload completed for %s:%d", client_ip, client_port)


class ThreadingHTTPServer(ThreadingMixIn, BaseHTTPServer):
    pass


class CustomHTTPServer(ThreadingHTTPServer):
    def __init__(self, server_address, RequestHandlerClass, root_directory):
        ThreadingHTTPServer.__init__(self, server_address, RequestHandlerClass)
        self.root_directory = os.path.abspath(root_directory)


def run(host, port, root):
    server_address = (host, port)
    httpd = CustomHTTPServer(server_address, ResumableFileRequestHandler, root) # type: ignore
    logging.info("Serving files from %s at http://%s:%d (Ctrl+C to stop)..." % (httpd.root_directory, host, port))
    httpd.serve_forever()


def main():
    parser = argparse.ArgumentParser(description="Start an HTTP file server with resumable upload/download support.")
    parser.add_argument("port", type=int, nargs='?', default=8000, help="Port to listen on (default: 8000)")
    parser.add_argument("--host", type=str, default="0.0.0.0", help="Host/IP address to bind (default: 0.0.0.0)")
    parser.add_argument("-r", "--root", type=str, default=".", help="Root directory to serve/store files from")
    args = parser.parse_args()

    if not os.path.isdir(args.root):
        logging.error("Error: Root directory %s does not exist." % args.root)
        exit(1)

    run(args.host, args.port, args.root)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
    main()
