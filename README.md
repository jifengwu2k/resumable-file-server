# Resumable File Server

A simple HTTP file server supporting resumable downloads and file uploads, written in pure Python using [`httppackets`](https://github.com/jifengwu2k/httppackets) for HTTP parsing/serialization and [`minimal-thread-pool`](https://github.com/jifengwu2k/minimal-thread-pool) for concurrency.

## Features

- Supports Python 2 and Python 3
- Proper handling of Unicode
- Support for HTTP Range requests (partial downloads)
- Multithreaded serving (handles many clients)
- Streaming uploads: GB-level files are written to disk in chunks, never buffered in memory
- Directory browsing and multi-file upload via UTF-8 HTML interface
- Specify root directory to serve files from
- Configurable host/IP, port, and worker thread count

## Installation

```bash
pip install resumable-file-server
```

## Usage

Serve files inside `/home/user` on `localhost:8080`:

```bash
resumable_file_server 8080 --host localhost --root /home/user/
```

Then download with `curl` (resumable with `-C -`):

```bash
curl -O -C - http://localhost:8080/largefile.zip
```

Upload one or more files (this stores them in `/home/user/images/`):

```bash
curl -X POST \
  -F "file=@/path/to/photo.jpg" \
  -F "file=@/path/to/document.pdf" \
  http://localhost:8080/images/
```

Open `http://localhost:8080/` in a browser to browse directories and use the built-in multi-file upload form.

### Arguments

| Argument       | Description                        | Default                 |
|----------------|------------------------------------|-------------------------|
| `port`         | Port to listen on                  | `8000`                  |
| `--host`       | Host/IP address to bind to         | `localhost`             |
| `--root`, `-r` | Root directory to serve files from | `.` (current directory) |
| `--threads`    | Number of worker threads           | CPU count (auto-detected) |

## Testing

A self-contained smoke test exercises directory listing, range downloads,
HEAD responses, error responses, path-traversal protection, and multipart
upload streaming (including a file larger than one read buffer):

```bash
python -m test.smoke_test
```

Run it with either Python 2 or Python 3 from the repository root.

## Security Notes

- Requests outside the root directory (e.g., `../../etc/passwd`) are blocked automatically.
- Only files inside the `--root` are accessible.
- Uploaded filenames are sanitized to a single path component, so `filename="../../evil.txt"` cannot escape the upload directory.

## Contributing

Contributions are welcome! Please submit pull requests or open issues on the GitHub repository.

## License

This project is licensed under the [MIT License](LICENSE).
