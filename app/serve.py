"""Serve the app on Windows, without Docker.

The container uses gunicorn, which is POSIX-only and will not run here. This is the
equivalent entry point for a Windows host: it prefers waitress (a proper production WSGI
server that does run on Windows) and falls back to Flask's own server, which is fine for a
handful of testers on an internal network but is not built for anything larger.

    python serve.py

It listens on every interface, so colleagues reach it at http://<this-machine>:9090 .
"""

import socket
import sys

import settings
from app import application


def local_addresses() -> list:
    """The addresses other people on the network can use to reach this machine."""
    host_name = socket.gethostname()
    addresses = [f'http://{host_name}:{settings.LISTEN_PORT}']
    try:
        for interface in socket.getaddrinfo(host_name, None, socket.AF_INET):
            address = f'http://{interface[4][0]}:{settings.LISTEN_PORT}'
            if address not in addresses:
                addresses.append(address)
    except socket.gaierror:
        pass  # No DNS for our own name; the hostname URL above is still worth printing.
    return addresses


def main() -> int:
    print(settings.APPLICATION_NAME)
    print('  Share one of these with your testers:')
    for address in local_addresses():
        print(f'    {address}')
    print('  Press Ctrl+C to stop.\n')

    try:
        from waitress import serve
    except ImportError:
        print('NOTE: waitress is not installed, using the Flask development server.\n'
              '      Fine for a small team; run "pip install waitress" for a sturdier one.\n',
              file=sys.stderr)
        application.run(host=settings.LISTEN_HOST, port=settings.LISTEN_PORT,
                        threaded=True, debug=False)
        return 0

    serve(application, host=settings.LISTEN_HOST, port=settings.LISTEN_PORT,
          threads=settings.SERVER_THREADS)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
