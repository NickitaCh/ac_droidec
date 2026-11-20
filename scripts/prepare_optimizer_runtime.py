"""Fetch pinned browser runtimes; no game data leaves this installation.

Run locally and on the web host before enabling the browser optimizer.
Large WASM files are deployment assets, not repository source files.
"""
import base64
import hashlib
from io import BytesIO
import json
from pathlib import Path
import tarfile
import urllib.request
import zipfile

ROOT = Path(__file__).resolve().parents[1]
DEST = ROOT / "web/static/vendor/optimizer"
PYODIDE = "0.28.3"
HIGHS = "1.15.4"
PULP = "3.3.0"


def fetch(url):
    with urllib.request.urlopen(url, timeout=120) as response:
        return response.read()


def main():
    DEST.mkdir(parents=True, exist_ok=True)
    records = {}

    def save(name, raw, origin):
        target = DEST / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(raw)
        records[name] = {"sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw), "origin": origin}

    for name in ("pyodide.js", "pyodide.asm.js", "pyodide.asm.wasm", "python_stdlib.zip", "pyodide-lock.json"):
        url = f"https://cdn.jsdelivr.net/pyodide/v{PYODIDE}/full/{name}"
        save(name, fetch(url), url)
    meta = json.loads(fetch(f"https://registry.npmjs.org/highs/{HIGHS}"))
    raw = fetch(meta["dist"]["tarball"])
    expected = meta["dist"]["integrity"].removeprefix("sha512-")
    if base64.b64encode(hashlib.sha512(raw).digest()).decode() != expected:
        raise ValueError("HiGHS integrity check failed")
    with tarfile.open(fileobj=BytesIO(raw), mode="r:gz") as archive:
        for member in archive.getmembers():
            if member.isfile() and (member.name.endswith(("/highs.js", "/highs.wasm")) or "LICENSE" in member.name):
                save(Path(member.name).name, archive.extractfile(member).read(), meta["dist"]["tarball"])
    meta = json.loads(fetch(f"https://pypi.org/pypi/pulp/{PULP}/json"))
    wheel = next(row for row in meta["urls"] if row["filename"].endswith("py3-none-any.whl"))
    raw = fetch(wheel["url"])
    if hashlib.sha256(raw).hexdigest() != wheel["digests"]["sha256"]:
        raise ValueError("PuLP integrity check failed")
    output = BytesIO()
    with zipfile.ZipFile(BytesIO(raw)) as archive, zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as dest:
        for name in archive.namelist():
            if name.startswith("pulp/") and (name.endswith(".py") or name.endswith("pulp.cfg.linux")) and "/tests/" not in name:
                dest.writestr(name, archive.read(name))
            if "LICENSE" in name:
                save("PULP-LICENSE", archive.read(name), wheel["url"])
    save("pulp.zip", output.getvalue(), wheel["url"])
    for required in ("highs.js", "highs.wasm", "pulp.zip"):
        if required not in records:
            raise ValueError("Missing runtime: " + required)
    (DEST / "manifest.json").write_text(json.dumps(records, indent=2), encoding="utf-8")
    print("Browser runtimes ready:", sum(r["bytes"] for r in records.values()) // 1024**2, "MiB")


if __name__ == "__main__":
    main()
