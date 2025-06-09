# Resumable File Server


A simple, multithreaded HTTP file server written in pure Python that supports:

- **File serving** from a specified root directory
- **Directory listings** in UTF-8 HTML
- **Resumable downloads** via HTTP `Range` requests
- **File uploads**
- Compatible with **all major operating systems** and **Python 2.7+ / 3.x**

## Features

- Support for HTTP Range requests (partial downloads)
- Multithreaded serving (handles many clients)
- Directory browsing and file upload via HTML interface
- Specify root directory to serve files from
- Configurable host/IP and port
- No dependencies (pure Python)

## Usage

Serve files inside `/home/user/downloads` on port 8080:

```bash
python resumable_file_server.py --host localhost --port 8080 --root /home/user/
```

Then download with `curl`:

```bash
curl -O -C - http://localhost:8080/largefile.zip
```

You can also upload a file (this uploads it to `/home/user/images/`):

```
curl -X POST -F "file=@/path/to/photo.jpg" http://localhost:8080/images/
```

And you will see an UTF-8 HTML with a file picker and upload button at the bottom if you open `http://localhost:8080/` with your browser.

### Arguments

| Argument | Description | Default |
| --- | --- | --- |
| `--port`, `-p` | Port to listen on | `8000` |
| `--host` | Host/IP address to bind to | `'localhost` |
| `--root`, `-r` | Root directory to serve files from | `.` (current directory) |

## Security Notes

-   Requests outside the root directory (e.g., `../../etc/passwd`) are blocked automatically.
-   Only files inside the `--root` are accessible.

## License

MIT License
