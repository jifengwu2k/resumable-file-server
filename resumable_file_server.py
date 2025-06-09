#!/usr/bin/env python
import argparse
import logging
import mimetypes
import os
import os.path
import re
import socket
import sys

if sys.version_info >= (3,):
    import html

    from http.server import BaseHTTPRequestHandler, HTTPServer as BaseHTTPServer
    from socketserver import ThreadingMixIn
    from urllib.parse import quote, unquote

    def html_escape(s):
        return html.escape(s)

    # unicode <-> uri
    def quote_unicode(unicode_str):
        return quote(unicode_str)
    
    def unquote_to_unicode(quoted):
        return unquote(quoted)

    # unicode <-> filesystem_encoded
    def unicode_to_filesystem_encoded(unicode_str):
        return unicode_str

    def filesystem_encoded_to_unicode(filesystem_encoded):
        return filesystem_encoded
else:
    import cgi

    from BaseHTTPServer import BaseHTTPRequestHandler, HTTPServer as BaseHTTPServer
    from SocketServer import ThreadingMixIn
    from urllib import quote, unquote

    def html_escape(s):
        return cgi.escape(s)

    # unicode <-> uri
    def quote_unicode(unicode_str):
        return quote(unicode_str.encode('utf-8'))
    
    def unquote_to_unicode(quoted):
        # UTF-8 is required to encode non-ASCII characters into valid URIs.
        return unicode(unquote(quoted), 'utf-8')
    
    # unicode <-> filesystem_encoded
    filesystem_encoding = sys.getfilesystemencoding() or 'utf-8'

    def unicode_to_filesystem_encoded(unicode_str):
        return unicode_str.encode(filesystem_encoding)

    def filesystem_encoded_to_unicode(filesystem_encoded):
        return unicode(filesystem_encoded, filesystem_encoding)

# unicode_path_components <-> uri_path
def unicode_path_components_to_uri_path(unicode_path_components, force_directory=False):
    if not unicode_path_components: return '/'
    else:
        uri_path_components = [quote_unicode(unicode_path_component) for unicode_path_component in unicode_path_components]
        uri_path_components[0] = '/' + uri_path_components[0]
        if force_directory:
            uri_path_components[-1] = uri_path_components[-1] + '/'
        return '/'.join(uri_path_components)

def uri_path_to_unicode_path_components(uri_path):
    return [unquote_to_unicode(component) for component in uri_path.split('/') if component]

# unicode_path_components -> filesystem_path (with check)
def unicode_path_components_to_filesystem_path(root_directory_path, unicode_path_components):
    filesystem_encoded_path_components = map(unicode_to_filesystem_encoded, unicode_path_components)

    # realpath ensures symlinks are resolved to prevent path traversal
    absolute_file_path = os.path.realpath(
        os.path.join(root_directory_path, *filesystem_encoded_path_components)  # type: ignore
    )

    absolute_root_directory_path = os.path.realpath(root_directory_path)

    if absolute_file_path.startswith(absolute_root_directory_path):
        return absolute_file_path
    else:
        return None

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

class ResumableFileRequestHandler(BaseHTTPRequestHandler):
    """
    HTTP request handler that supports:
    - GET with Range (resumable download)
    - POST multipart (standard upload)
    """
    def do_GET(self):
        unicode_uri_path = unquote_to_unicode(self.path)
        unicode_path_comps = uri_path_to_unicode_path_components(self.path)
        fs_path = unicode_path_components_to_filesystem_path(self.server.root_directory, unicode_path_comps)

        # If path is invalid -> 404
        if fs_path is None or not os.path.exists(fs_path):
            self.send_error(404, "File Not Found: %s" % self.path)
            return
        # If path is a directory -> generate an HTML listing with an upload form
        elif os.path.isdir(fs_path):
            try:
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

                filesystem_encoded_entries = sorted(os.listdir(fs_path)) # type: ignore

                for fs_encoded_entry in filesystem_encoded_entries:
                    fs_entry_path = os.path.join(fs_path, fs_encoded_entry) # type: ignore

                    unicode_entry = filesystem_encoded_to_unicode(fs_encoded_entry)

                    if os.path.isdir(fs_entry_path):
                        unicode_displayname = unicode_entry + u"/"
                        entry_uri_path = unicode_path_components_to_uri_path(unicode_path_comps + [unicode_entry + '/'])
                    elif os.path.islink(fs_entry_path):
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

                utf8_encoded_html_page = b'\n'.join(map(lambda line: line.encode('utf-8'), unicode_html_lines))

                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()

                self.wfile.write(utf8_encoded_html_page)

            except Exception as e:
                logging.error("Error generating directory listing: %s", str(e))
                self.send_error(500, "Failed to list directory")
            return
        # If path is a file -> serve it with resumable download
        else:
            file_size = os.path.getsize(fs_path)
            start = 0
            end = file_size - 1

            range_header = self.headers.get('Range') if hasattr(self.headers, 'get') else self.headers.getheader('Range')
            if range_header:
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

            fs_filename = os.path.basename(fs_path)
            unicode_filename = filesystem_encoded_to_unicode(fs_filename)

            self.send_header('Content-Type', guess_mime_type(fs_filename))
            self.send_header('Content-Disposition', "attachment; filename*=UTF-8''%s" % quote_unicode(unicode_filename))
            self.send_header('Content-Length', str(end - start + 1))
            self.send_header('Accept-Ranges', 'bytes')
            self.end_headers()

            try:
                with open(fs_path, 'rb') as f:
                    f.seek(start)
                    remaining = end - start + 1
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
                            logging.warning('Client %s:%d disconnected while downloading %s. Bytes sent: %d/%d.' % (client_ip, client_port, fs_filename, bytes_sent, end - start + 1))
                            break
                        bytes_sent += len(chunk)
                        remaining -= len(chunk)
            except IOError:
                self.send_error(500, "Internal Server Error while reading file.")

    def do_POST(self):
        unicode_path_comps = uri_path_to_unicode_path_components(self.path)
        fs_upload_path = unicode_path_components_to_filesystem_path(self.server.root_directory, unicode_path_comps)

        if fs_upload_path is None or not os.path.isdir(fs_upload_path):
            self.send_error(400, "Invalid upload path")
            return

        try:
            files = parse_multipart_form_data(self)
        except Exception as e:
            self.send_error(400, "Invalid multipart/form-data: %s" % str(e))
            return

        uploaded = False

        for filename, filedata in files:
            filename = os.path.basename(filename)
            fs_dest_path = os.path.join(fs_upload_path, filename) # type: ignore

            try:
                with open(fs_dest_path, 'wb') as f:
                    f.write(filedata)
                logging.info("Uploaded file %s saved", filename)
                uploaded = True
            except Exception as e:
                logging.error("Failed to save file: %s", str(e))
                self.send_error(500, "Failed to save uploaded file")
                return

        if uploaded:
            self.send_response(303)
            self.send_header("Location", self.path)
            self.end_headers()
        else:
            self.send_error(400, "No files uploaded")


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
    parser.add_argument("-p", "--port", type=int, default=8000, help="Port to listen on (default: 8000)")
    parser.add_argument("--host", type=str, default="localhost", help="Host/IP address to bind (default: localhost)")
    parser.add_argument("-r", "--root", type=str, default=".", help="Root directory to serve/store files from")
    args = parser.parse_args()

    if not os.path.isdir(args.root):
        logging.error("Error: Root directory %s does not exist." % args.root)
        exit(1)

    run(args.host, args.port, args.root)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    main()
