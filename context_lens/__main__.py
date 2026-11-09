import argparse
import os
from pathlib import Path
import secrets

from aiohttp import web

from .server import create_app


def load_token(path):
    path = Path(path)
    if path.is_symlink() or path.parent.is_symlink():
        raise ValueError('token path must not be a symlink')
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    except FileExistsError:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd) as handle:
            token = handle.read().strip()
        os.chmod(path, 0o600)
        if len(token) < 24:
            raise ValueError('existing token file is invalid; not overwriting it')
        return token
    token = secrets.token_urlsafe(32)
    with os.fdopen(fd, 'w') as handle:
        handle.write(token + '\n')
    return token


def main():
    parser = argparse.ArgumentParser(description='Context Lens - local opt-in request collector')
    parser.add_argument('--port', type=int, default=18765)
    parser.add_argument('--state', type=Path, default=Path('.runtime'))
    parser.add_argument('--upstream', default='https://api.openai.com/v1', help='fixed Codex API destination, HTTPS only')
    parser.add_argument('--print-url', action='store_true', help='print a private browser login link and exit')
    args = parser.parse_args()
    token = load_token(args.state / 'token')
    if args.print_url:
        print(f'http://127.0.0.1:{args.port}/#token={token}')
        return
    app = create_app(args.state / 'capture.db', token, upstream=args.upstream)
    print(f'Context Lens: http://127.0.0.1:{args.port}/')
    print(f'Private access token: {args.state.resolve() / "token"}')
    print('Recording is off until explicitly enabled for a session. No client settings changed.')
    web.run_app(app, host='127.0.0.1', port=args.port, access_log=None, print=None)


if __name__ == '__main__':
    main()
