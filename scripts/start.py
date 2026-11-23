"""Start the backend and open its bundled frontend once the server is ready."""
import argparse
import importlib.util
from pathlib import Path
import socket
import subprocess
import sys
import time
import urllib.request
import webbrowser

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=8000)
    parser.add_argument('--no-browser', action='store_true', help='Start without opening a browser')
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error('--port must be between 1 and 65535')
    if any(importlib.util.find_spec(name) is None for name in ('uvicorn', 'fastapi')):
        print('Missing dependencies. Run: python -m pip install -e ".[test]"', file=sys.stderr)
        return 1
    try:
        with socket.socket() as probe:
            probe.bind(('127.0.0.1', args.port))
    except OSError as exc:
        print(f'Cannot use port {args.port}: {exc}\nStop the existing server or use start.bat --port 8001.', file=sys.stderr)
        return 1
    url = f'http://127.0.0.1:{args.port}'
    print(f'Starting backend and dashboard at {url}\nPress Ctrl+C here to stop.', flush=True)
    process = None
    try:
        process = subprocess.Popen([
            sys.executable, '-m', 'uvicorn', 'tuneio.main:create_app', '--factory',
            '--host', '127.0.0.1', '--port', str(args.port),
        ], cwd=ROOT)
        # Ignore proxy environment settings for local readiness checks.
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if process.poll() is not None:
                print('Server exited before startup completed. Check the error above.', file=sys.stderr)
                return 1
            try:
                with opener.open(url + '/health', timeout=.5) as response:
                    ready = response.status == 200
            except OSError:
                ready = False
            if ready:
                if not args.no_browser:
                    try:
                        if not webbrowser.open(url):
                            print(f'Open {url} in your browser.')
                    except webbrowser.Error:
                        print(f'Open {url} in your browser.')
                return process.wait()
            time.sleep(.1)
        print('Server did not become ready within 30 seconds.', file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print('\nStopping tuneio...')
        return 0
    finally:
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()


if __name__ == '__main__':
    raise SystemExit(main())
