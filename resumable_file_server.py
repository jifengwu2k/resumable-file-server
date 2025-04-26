#!/usr/bin/env python3
import argparse
import logging
import os
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import unquote


class ResumableFileRequestHandler(BaseHTTPRequestHandler):
    """
    HTTP request handler that supports file downloads and resumable (Range) requests.
    """

    def translate_path(self, path):
        """
        Translate URL path into filesystem path relative to the configured root directory.
        """
        path = unquote(path)
        path = path.lstrip('/')
        full_path = os.path.normpath(os.path.join(self.server.root_directory, path))

        # Prevent directory traversal attacks (e.g., using .. to escape)
        if not full_path.startswith(os.path.abspath(self.server.root_directory)):
            return None
        return full_path

    def do_GET(self):
        """
        Handle GET requests with support for partial content (Range requests).
        """
        file_path = self.translate_path(self.path)
        if not file_path or not os.path.isfile(file_path):
            self.send_error(404, f"File Not Found: {self.path}")
            return

        file_size = os.path.getsize(file_path)
        start = 0
        end = file_size - 1

        range_header = self.headers.get('Range')
        if range_header:
            if not range_header.startswith('bytes=') or '-' not in range_header:
                self.send_error(400, "Invalid Range Header")
                return

            start_str, end_str = range_header[len('bytes='):].split('-', 1)
            start = int(start_str) if start_str else 0
            end = int(end_str) if end_str else file_size - 1

            if start >= file_size or end >= file_size or start > end:
                self.send_error(416, "Requested Range Not Satisfiable")
                return

            self.send_response(206)
            self.send_header('Content-Range', f'bytes {start}-{end}/{file_size}')
        else:
            self.send_response(200)

        self.send_header('Content-Type', 'application/octet-stream')
        self.send_header('Content-Disposition', f'attachment; filename="{os.path.basename(file_path)}"')
        self.send_header('Content-Length', str(end - start + 1))
        self.send_header('Accept-Ranges', 'bytes')
        self.end_headers()

        with open(file_path, 'rb') as f:
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
                except ConnectionResetError:
                    client_ip, client_port = self.client_address
                    logging.warning(
                        f"Client {client_ip}:{client_port} disconnected while downloading '{os.path.basename(file_path)}'. "
                        f"Bytes sent: {bytes_sent}/{end - start + 1}."
                    )
                    break
                bytes_sent += len(chunk)
                remaining -= len(chunk)


class CustomHTTPServer(ThreadingHTTPServer):
    def __init__(self, server_address, RequestHandlerClass, root_directory):
        super().__init__(server_address, RequestHandlerClass)
        self.root_directory = os.path.abspath(root_directory)


def run(host: str, port: int, root: str):
    """
    Start the HTTP server.
    """
    server_address = (host, port)
    httpd = CustomHTTPServer(server_address, ResumableFileRequestHandler, root)
    logging.info(f"Serving files from '{httpd.root_directory}' at http://{host}:{port} (Ctrl+C to stop)...")
    httpd.serve_forever()


def main():
    parser = argparse.ArgumentParser(description="Start a simple HTTP file server with resumable download support.")
    parser.add_argument(
        "-p", "--port",
        type=int,
        default=8000,
        help="Port to listen on (default: 8000)"
    )
    parser.add_argument(
        "--host",
        type=str,
        default="localhost",
        help="Host/IP address to bind (default: localhost)"
    )
    parser.add_argument(
        "-r", "--root",
        type=str,
        default=".",
        help="Root directory to serve files from (default: current directory)"
    )
    args = parser.parse_args()

    if not os.path.isdir(args.root):
        logging.error(f"Error: Root directory '{args.root}' does not exist.")
        exit(1)

    run(args.host, args.port, args.root)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
    logger = logging.getLogger(__name__)

    main()
