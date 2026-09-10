#!/usr/bin/env python3
"""Verify PyPI package contents before and after trusted publication."""
import hashlib, io, json, os, sys, tarfile, time, urllib.error, urllib.request, zipfile
from pathlib import Path
def normalized_digest(raw, filename):
    digest = hashlib.sha256()
    if filename.endswith('.whl'):
        archive = zipfile.ZipFile(io.BytesIO(raw)); members = ((item.filename, item.is_dir(), archive.read(item)) for item in archive.infolist())
    else:
        archive = tarfile.open(fileobj=io.BytesIO(raw), mode='r:gz'); members = ((item.name, item.isdir(), archive.extractfile(item).read() if item.isfile() else item.linkname.encode()) for item in archive.getmembers())
    for name, directory, content in sorted(members): digest.update(name.encode()+b'\0'+(b'd' if directory else b'f')+b'\0'+content)
    return digest.hexdigest()
def remote_files(package, version):
    try: payload = json.load(urllib.request.urlopen(f'https://pypi.org/pypi/{package}/{version}/json'))
    except urllib.error.HTTPError as error:
        if error.code == 404: return {}
        raise
    return {item['filename']: normalized_digest(urllib.request.urlopen(item['url']).read(), item['filename']) for item in payload['urls']}
local = {path.name: normalized_digest(path.read_bytes(), path.name) for path in Path(os.environ['DIST_DIR']).iterdir() if path.suffix in {'.whl', '.gz'}}; mode = os.environ.get('MODE', 'pre')
for attempt in range(3):
    remote = remote_files(os.environ['PYPI_PACKAGE'], os.environ['PYPI_VERSION'].removeprefix('v'))
    if set(remote)-set(local): sys.exit('PyPI has unexpected files for this version')
    if any(remote[name] != local[name] for name in set(remote)&set(local)): sys.exit('PyPI artifact content differs from this build')
    if mode == 'pre': state = 'skip' if set(remote)==set(local) else 'publish'; break
    if set(remote)==set(local): state = 'complete'; break
    if attempt == 2: sys.exit('PyPI upload did not become fully visible')
    time.sleep(5)
with open(os.environ['GITHUB_OUTPUT'], 'a') as output: output.write(f'state={state}\n')
