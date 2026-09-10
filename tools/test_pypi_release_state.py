import io, json, os, runpy, tarfile, tempfile, unittest, urllib.error, zipfile
from pathlib import Path
from unittest.mock import patch

C = Path(__file__).with_name("pypi_release_state.py")
def whl(stamp, data=b"same contents"):
    raw = io.BytesIO()
    with zipfile.ZipFile(raw, "w") as z: z.writestr(zipfile.ZipInfo("package/__init__.py", stamp), data)
    return raw.getvalue()
def sdist(stamp):
    raw = io.BytesIO(); data = b"same contents"
    with tarfile.open(fileobj=raw, mode="w:gz") as tar:
        entry = tarfile.TarInfo("package-1.0/package/__init__.py"); entry.size, entry.mtime = len(data), stamp
        tar.addfile(entry, io.BytesIO(data))
    return raw.getvalue()
class ReleaseStateTest(unittest.TestCase):
    def check(self, local, remote, mode="pre"):
        with tempfile.TemporaryDirectory() as temp:
            dist = Path(temp, "dist"); dist.mkdir(); output = Path(temp, "output")
            for name, data in local.items(): Path(dist, name).write_bytes(data)
            def open_url(url):
                url = str(url)
                if url.startswith("https://pypi.org/"):
                    if not remote:
                        error = urllib.error.HTTPError(url, 404, "missing", None, None)
                        error.close()
                        raise error
                    return io.BytesIO(json.dumps({"urls": [{"filename": n, "url": "https://files/" + n} for n in remote]}).encode())
                return io.BytesIO(remote[url.rsplit("/", 1)[-1]])
            env = {"DIST_DIR": str(dist), "PYPI_PACKAGE": "package", "PYPI_VERSION": "v1.0", "MODE": mode, "GITHUB_OUTPUT": str(output)}
            with patch.dict(os.environ, env, clear=True), patch("urllib.request.urlopen", side_effect=open_url), patch("time.sleep") as sleep:
                try: runpy.run_path(str(C), run_name="__main__")
                except SystemExit as error: return None, str(error), sleep.call_args_list
            return output.read_text(), None, sleep.call_args_list
    def test_precheck_ignores_json_and_reports_publish_or_skip(self):
        file = whl((2020, 1, 1, 0, 0, 0)); local = {"package.whl": file, "attestation.json": b"{}"}
        for remote, state in (({}, "publish"), ({"package.whl": file}, "skip")):
            with self.subTest(remote=remote):
                output, error, _ = self.check(local, remote); self.assertIsNone(error); self.assertEqual(output, f"state={state}\n")
    def test_precheck_reports_publish_for_matching_partial_remote(self):
        file = whl((2020, 1, 1, 0, 0, 0)); output, error, _ = self.check({"package.whl": file, "package.tar.gz": sdist(1)}, {"package.whl": file})
        self.assertIsNone(error); self.assertEqual(output, "state=publish\n")
    def test_rejects_remote_artifact_with_different_contents(self):
        output, error, _ = self.check({"package.whl": whl((2020, 1, 1, 0, 0, 0))}, {"package.whl": whl((2021, 1, 1, 0, 0, 0), b"different")})
        self.assertIsNone(output); self.assertEqual(error, "PyPI artifact content differs from this build")
    def test_postcheck_retries_partial_remote_then_fails(self):
        file = whl((2020, 1, 1, 0, 0, 0)); output, error, sleeps = self.check({"package.whl": file, "package.tar.gz": sdist(1)}, {"package.whl": file}, "post")
        self.assertIsNone(output); self.assertEqual(error, "PyPI upload did not become fully visible"); self.assertEqual([x.args for x in sleeps], [(5,), (5,)])
    def test_accepts_matching_archives_with_different_container_timestamps(self):
        output, error, _ = self.check({"package.whl": whl((2020, 1, 1, 0, 0, 0)), "package.tar.gz": sdist(1)}, {"package.whl": whl((2021, 1, 1, 0, 0, 0)), "package.tar.gz": sdist(2)})
        self.assertIsNone(error); self.assertEqual(output, "state=skip\n")
if __name__ == "__main__": unittest.main()
