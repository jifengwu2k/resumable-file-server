# Resumable File Server

A simple multithreaded HTTP file server that supports **resumable downloads** (via HTTP Range requests) and **serves files securely from a specified directory**.

## Features

- Support for HTTP Range requests (partial downloads)
- Multithreaded serving (handles many clients)
- Specify root directory to serve files from
- Configurable host/IP and port
- No dependencies (pure Python)

## Usage

```bash
python resumable_file_server.py --host your-host --port 8000 --root /path/to/files
```

This will serve files from `/path/to/files` at `http://your-host:8000`.

### Example

Serve files inside `/home/user/downloads` on port 8080:

```bash
python resumable_file_server.py --host localhost --port 8080 --root /home/user/downloads
```

Then download with `curl`:

```bash
curl -O -C - http://localhost:8080/largefile.zip
```

### Arguments

| Argument | Description | Default |
| --- | --- |  |
| `--port`, `-p` | Port to listen on | `8000` |
| `--host` | Host/IP address to bind to | `'localhost` |
| `--root`, `-r` | Root directory to serve files from | `.` (current directory) |

## Security Notes

-   Requests outside the root directory (e.g., `../../etc/passwd`) are blocked automatically.
-   Only files inside the `--root` are accessible.

## License

MIT License
