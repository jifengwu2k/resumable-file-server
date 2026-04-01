# Resumable File Server

A simple, multithreaded HTTP file server supporting resumable downloads and file uploads written in pure Python. Drop-in replacement for `SimpleHTTPServer`, `http.server`.

## Features

- Supports Python 2 and Python 3
- Proper handling of Unicode
- Support for HTTP Range requests (partial downloads)
- Multithreaded serving (handles many clients)
- Directory browsing and multi-file upload via UTF-8 HTML interface
- Specify root directory to serve files from
- Configurable host/IP and port

## Usage

Serve files inside `/home/user` on `localhost:8080`:

```bash
python -m resumable_file_server 8080 --host localhost --port  --root /home/user/
```

Then download with `curl`:

```bash
curl -O -C - http://localhost:8080/largefile.zip
```

You can also upload one or more files (this uploads them to `/home/user/images/`):

```bash
curl -X POST \
  -F "file=@/path/to/photo.jpg" \
  -F "file=@/path/to/document.pdf" \
  http://localhost:8080/images/
```

And you will see an UTF-8 HTML with a multi-file picker and upload button at the bottom if you open `http://localhost:8080/` with your browser.

### Arguments

| Argument       | Description                        | Default                 |
|----------------|------------------------------------|-------------------------|
| `port`         | Port to listen on                  | `8000`                  |
| `--host`       | Host/IP address to bind to         | `localhost`             |
| `--root`, `-r` | Root directory to serve files from | `.` (current directory) |

## Security Notes

-   Requests outside the root directory (e.g., `../../etc/passwd`) are blocked automatically.
-   Only files inside the `--root` are accessible.

## License

MIT License
