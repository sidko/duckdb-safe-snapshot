#!/usr/bin/env python3
"""Decide whether a trusted PyPI release needs upload without trusting archives' timestamps."""
import hashlib, io, json, os, sys, tarfile, urllib.error, urllib.request, zipfile
from pathlib import Path

def content_digest(raw: bytes, filename: str) -> str:
    digest = hashlib.sha256()
    if filename.endswith('.whl'):
        archive = zipfile.ZipFile(io.BytesIO(raw)); entries = ((item.filename, item.is_dir(), archive.read(item)) for item in archive.infolist())
    else:
        archive = tarfile.open(fileobj=io.BytesIO(raw), mode='r:gz'); entries = ((item.name, item.isdir(), archive.extractfile(item).read() if item.isfile() else item.linkname.encode()) for item in archive.getmembers())
    for name, directory, body in sorted(entries): digest.update(name.encode() + b'\0' + (b'd' if directory else b'f') + b'\0' + body)
    return digest.hexdigest()
dist = Path(os.environ['DIST_DIR']); package = os.environ['PYPI_PACKAGE']; version = os.environ['PYPI_VERSION'].removeprefix('v')
local = {path.name: content_digest(path.read_bytes(), path.name) for path in dist.iterdir() if path.is_file()}
try: payload = json.load(urllib.request.urlopen(f'https://pypi.org/pypi/{package}/{version}/json'))
except urllib.error.HTTPError as error:
    if error.code != 404: raise
    remote = {}
else: remote = {item['filename']: content_digest(urllib.request.urlopen(item['url']).read(), item['filename']) for item in payload['urls']}
if set(remote) - set(local): sys.exit('PyPI has unexpected files for this version')
for name in set(remote) & set(local):
    if remote[name] != local[name]: sys.exit(f'PyPI artifact content differs: {name}')
with open(os.environ['GITHUB_OUTPUT'], 'a') as output: output.write(f"state={'skip' if set(remote) == set(local) else 'publish'}\n")
