import argparse
import hashlib
import http.client
import json
from pathlib import Path, PurePosixPath
import re
import shutil
import sys
import tarfile
import time
import urllib.error
import urllib.parse
import urllib.request

CHUNK_SIZE = 1024 * 1024
USER_AGENT = "HorseResearchStarter/0.1"


class PrivateHeaderRedirect(urllib.request.HTTPRedirectHandler):
    """Do not forward API credentials to a different download host."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        redirected = super().redirect_request(req, fp, code, msg, headers, newurl)
        if redirected is not None:
            old = urllib.parse.urlsplit(req.full_url)
            new = urllib.parse.urlsplit(newurl)
            if (old.scheme, old.netloc) != (new.scheme, new.netloc):
                for name in ("Authorization", "X-dataverse-key"):
                    redirected.remove_header(name)
        return redirected


def open_url(url, headers=None):
    request = urllib.request.Request(
        url, headers={"User-Agent": USER_AGENT, "Accept-Encoding": "identity", **(headers or {})}
    )
    return urllib.request.build_opener(PrivateHeaderRedirect()).open(request, timeout=60)


def fetch_json(url, headers=None):
    with open_url(url, headers) as response:
        return json.load(response)


def save_json(path: Path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def human_size(size):
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if size < 1024 or unit == "TiB":
            return f"{size:,.1f} {unit}"
        size /= 1024


def safe_path(root: Path, name: str) -> Path:
    """Keep downloaded files and extracted members inside their output directory."""
    relative = PurePosixPath(name)
    if relative.is_absolute() or ".." in relative.parts or "\\" in name or ":" in name:
        raise ValueError(f"Unsafe relative path: {name!r}")
    root = root.resolve()
    path = root.joinpath(*relative.parts)
    if path.resolve() != root and root not in path.resolve().parents:
        raise ValueError(f"Path escapes output directory: {name!r}")
    return path


def verify_file(path: Path, size: int, checksum: dict):
    if path.stat().st_size != size:
        raise ValueError(f"Size mismatch for {path}: expected {size} bytes")
    algorithm = checksum["type"].lower().replace("-", "")
    digest = hashlib.new(algorithm)
    with path.open("rb") as source:
        for block in iter(lambda: source.read(CHUNK_SIZE), b""):
            digest.update(block)
    # Some Dataverse MD5 strings omit leading zeroes.
    expected = checksum["value"].lower().zfill(digest.digest_size * 2)
    if digest.hexdigest() != expected:
        raise ValueError(f"Checksum mismatch for {path}; remove this file and retry")


def download_file(url: str, path: Path, size: int, checksum: dict, headers=None):
    """Resume a .part file, verify it, then rename it to the final filename.

    Existing final files are verified and skipped. A server that ignores Range
    causes a restart, never an append of duplicate bytes.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        verify_file(path, size, checksum)
        print(f"Verified existing: {path}", flush=True)
        return path
    partial = path.with_name(path.name + ".part")
    if partial.exists() and partial.stat().st_size > size:
        raise ValueError(f"Oversized partial file: {partial}; remove it and retry")
    for attempt in range(3):
        offset = partial.stat().st_size if partial.exists() else 0
        if offset == size:
            break
        request_headers = dict(headers or {})
        if offset:
            request_headers["Range"] = f"bytes={offset}-"
        try:
            with open_url(url, request_headers) as response:
                if response.headers.get_content_type() in ("text/html", "application/json"):
                    raise ValueError("Download returned a web/error page instead of dataset bytes")
                mode = "wb"
                if response.status == 206:
                    match = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+)", response.headers.get("Content-Range", ""))
                    if not match or int(match[1]) != offset or int(match[3]) != size:
                        raise ValueError("Server returned an inconsistent download range")
                    mode = "ab" if offset else "wb"
                elif response.status == 200:
                    offset = 0
                else:
                    raise ValueError(f"Unexpected download status: {response.status}")
                print(f"Downloading {path.name}: {human_size(offset)} / {human_size(size)}", flush=True)
                last_report = time.monotonic()
                with partial.open(mode) as destination:
                    while block := response.read(CHUNK_SIZE):
                        if offset + len(block) > size:
                            raise ValueError("Server sent more bytes than the source metadata specifies")
                        destination.write(block)
                        offset += len(block)
                        if time.monotonic() - last_report >= 5:
                            print(f"  {human_size(offset)} / {human_size(size)} ({offset / size:.0%})", flush=True)
                            last_report = time.monotonic()
                if offset != size:
                    raise ConnectionError("Incomplete download; retaining partial file for resume")
            break
        except urllib.error.HTTPError as error:
            if error.code not in (408, 429, 500, 502, 503, 504) or attempt == 2:
                raise
            print(f"HTTP {error.code}; retrying download", flush=True)
        except (urllib.error.URLError, ConnectionError, TimeoutError, http.client.IncompleteRead):
            if attempt == 2:
                raise ConnectionError("Download failed after 3 attempts; rerun to resume the .part file") from None
            print("Connection interrupted; resuming download", flush=True)
        time.sleep(2 ** attempt)
    verify_file(partial, size, checksum)
    partial.replace(path)
    print(f"Downloaded and verified: {path}", flush=True)
    return path


def extract_tar(archive: Path, destination: Path):
    """Extract regular files/directories only, without executing dataset content."""
    destination.mkdir(parents=True, exist_ok=True)
    count = 0
    with tarfile.open(archive, mode="r|*") as bundle:
        for member in bundle:
            target = safe_path(destination, member.name)
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
            elif member.isfile():
                target.parent.mkdir(parents=True, exist_ok=True)
                with bundle.extractfile(member) as source, target.open("wb") as output:
                    shutil.copyfileobj(source, output, CHUNK_SIZE)
                count += 1
            else:
                raise ValueError(f"Unsupported archive link/special file: {member.name}")
    print(f"Extracted {count} files to {destination}", flush=True)


def cli_error(error):
    if isinstance(error, urllib.error.HTTPError):
        print(f"ERROR: server returned HTTP {error.code}. Check the dataset access page and retry.")
    else:
        print(f"ERROR: {error}")
    return 1

REPO = "mwmathis/Horse-30"
REVISION = "55142e51668207b21f68e40a99e9aed0655e3a3f"
ARCHIVE = "horse10.tar.xz"


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, default=Path("data/horse30"))
    parser.add_argument("--revision", default=REVISION, help="Hugging Face commit or branch; default is a pinned commit")
    parser.add_argument("--download", action="store_true", help="Download the archive; otherwise preview only")
    parser.add_argument("--extract", action="store_true", help="Extract the archive; requires --download")
    args = parser.parse_args(argv)
    if args.extract and not args.download:
        parser.error("--extract requires --download")
    # Resolve a branch to a commit before querying files or downloading bytes.
    info = fetch_json(f"https://huggingface.co/api/datasets/{REPO}/revision/{urllib.parse.quote(args.revision, safe='')}")
    commit = info["sha"]
    entries = fetch_json(f"https://huggingface.co/api/datasets/{REPO}/tree/{commit}")
    record = next((row for row in entries if row["path"] == ARCHIVE), None)
    if record is None or not record.get("lfs", {}).get("oid"):
        raise ValueError("The source archive or its SHA-256 checksum is missing from Hugging Face metadata")
    checksum = {"type": "SHA-256", "value": record["lfs"]["oid"]}
    url = f"https://huggingface.co/datasets/{REPO}/resolve/{commit}/{ARCHIVE}?download=true"
    print(f"Horse-30: {ARCHIVE}, {human_size(record['size'])}; commit {commit}")
    print("8,114 expert-labeled 2D frames, 30 horses, 22 landmarks.")
    print(f"Dataset access and license: https://huggingface.co/datasets/{REPO}")
    if not args.download:
        print("Preview only. Add --download --extract to fetch and unpack the dataset.")
        return 0
    save_json(args.out / "download_manifest.json", {
        "dataset": REPO, "revision": commit, "path": ARCHIVE,
        "size": record["size"], "checksum": checksum, "url": url,
    })
    archive = download_file(url, args.out / ARCHIVE, record["size"], checksum)
    if args.extract:
        extract_tar(archive, args.out / "extracted")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nInterrupted. Run the same command to resume partial downloads.")
        sys.exit(130)
    except (OSError, ValueError, KeyError, tarfile.TarError) as error:
        sys.exit(cli_error(error))
