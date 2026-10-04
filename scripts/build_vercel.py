"""Build only public assets. Never copy web/data, reports, tests or secrets."""
from pathlib import Path
import shutil

ROOT = Path(__file__).resolve().parents[1]
PUBLIC_EXTENSIONS = {'.html','.css','.js','.png','.jpg','.jpeg','.svg','.ico','.webp','.woff','.woff2'}


def build(source=ROOT/'web', target=ROOT/'public'):
    source, target = Path(source), Path(target)
    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True)
    for path in source.rglob('*'):
        relative = path.relative_to(source)
        if any(part in ('data','tests') or part.startswith('.') for part in relative.parts):
            continue
        if path.is_symlink():
            raise ValueError('Public build cannot contain symbolic links')
        if path.is_file() and path.suffix.lower() in PUBLIC_EXTENSIONS:
            destination = target/relative
            destination.parent.mkdir(parents=True,exist_ok=True)
            shutil.copyfile(path,destination)
    if not (target/'index.html').is_file() or not (target/'admin.html').is_file():
        raise ValueError('Public entry pages are missing')


if __name__ == '__main__':
    build()
