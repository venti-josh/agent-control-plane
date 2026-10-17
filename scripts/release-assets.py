"""Prepare the fixed-name assets consumed by install.sh."""
import hashlib
import os
import re
import runpy
import shutil
import tomllib
from pathlib import Path

root = Path(__file__).resolve().parents[1]
version = tomllib.loads((root / 'pyproject.toml').read_text())['project']['version']
if not re.fullmatch(r'\d+\.\d+\.\d+', version):
    raise SystemExit('Release version must be X.Y.Z')
if runpy.run_path(str(root / 'acp/__init__.py'))['__version__'] != version:
    raise SystemExit('Runtime version must match the pyproject.toml version')
if os.getenv('GITHUB_REF_TYPE') == 'tag' and os.environ['GITHUB_REF_NAME'] != f'v{version}':
    raise SystemExit('Tag must match the pyproject.toml version')
release = root / 'dist/release'
if release.exists():
    shutil.rmtree(release)
release.mkdir(parents=True, exist_ok=True)
wheel = f'acp-{version}-py3-none-any.whl'
for name in (wheel, 'constraints.txt'):
    shutil.copyfile(root / 'dist' / name, release / name)
(release / 'VERSION').write_text(version + '\n')
installer = (root / 'scripts/install.sh').read_text().replace("acp_default_version=''", f"acp_default_version='{version}'")
(release / 'install.sh').write_text(installer)
(release / 'SHA256SUMS').write_text(''.join(
    f'{hashlib.sha256((release / name).read_bytes()).hexdigest()}  {name}\n'
    for name in (wheel, 'constraints.txt', 'VERSION', 'install.sh')))
print(f'Release assets: {release}')
