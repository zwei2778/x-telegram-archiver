"""Package only the reviewed public files; never walk a private data directory."""
import hashlib
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

ROOT = Path(__file__).resolve().parent.parent
VERSION = '0.1.0-preview'
FILES = (
    'archiver.py', 'prepare_x_session.py', 'requirements-runtime.txt',
    'Dockerfile', 'compose.yaml', '.env.example', '.gitignore', '.dockerignore',
    'README.md', 'LICENSE', 'THIRD_PARTY.md', 'SECURITY.md', 'CHANGELOG.md',
    'docs/NAS.md', 'docs/NETWORK.md', 'docs/X_SESSION.md', 'docs/DEVELOPMENT.md',
    'tests/test_archiver.py', 'tests/test_user_queue_async.py',
    'tests/test_user_progress.py', 'tests/test_x_session_runtime.py',
    'tests/test_setup_user.py', 'tests/test_prepare_x_session.py',
    '.github/workflows/tests.yml', 'tools/package_release.py',
)


def main():
    output = ROOT / 'dist'
    output.mkdir(exist_ok=True)
    archive = output / ('x-telegram-archiver-' + VERSION + '.zip')
    paths = []
    for name in FILES:
        path = ROOT / name
        if path.is_symlink() or not path.is_file():
            raise RuntimeError('A reviewed public file is missing or symbolic')
        if any(part in ('data', 'private', 'secrets', 'proxy-state', 'browser_profile', '.venv')
               for part in path.relative_to(ROOT).parts):
            raise RuntimeError('Private paths may not be packaged')
        paths.append((name, path))
    with ZipFile(archive, 'x', compression=ZIP_DEFLATED) as bundle:
        for name, path in paths:
            bundle.write(path, 'x-telegram-archiver/' + name)
    with ZipFile(archive) as bundle:
        expected = sorted('x-telegram-archiver/' + name for name in FILES)
        if sorted(bundle.namelist()) != expected or bundle.testzip() is not None:
            raise RuntimeError('Public archive verification failed')
    checksum = hashlib.sha256(archive.read_bytes()).hexdigest()
    (output / 'SHA256SUMS').write_text(checksum + '  ' + archive.name + '\n')
    print('Reviewed public files:', len(FILES))
    print(archive)


if __name__ == '__main__':
    main()
