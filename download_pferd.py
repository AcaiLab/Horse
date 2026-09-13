import argparse
from collections import defaultdict
import fnmatch
import hashlib
import http.client
import json
import os
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

SERVER = "https://dataverse.harvard.edu"
DOI = "doi:10.7910/DVN/2EXONE"
DATA_PAGE = "https://doi.org/10.7910/DVN/2EXONE"


def get_metadata(version, headers, metadata_path=None):
    if metadata_path:
        payload = json.loads(metadata_path.read_text(encoding="utf-8"))
    else:
        url = f"{SERVER}/api/datasets/:persistentId/versions/{urllib.parse.quote(version, safe=':')}?"
        payload = fetch_json(url + urllib.parse.urlencode({"persistentId": DOI}), headers)
    data = payload.get("data", payload)
    data = data.get("datasetVersion", data)
    if data.get("datasetPersistentId") != DOI or not data.get("files"):
        raise ValueError("Metadata must be a PFERD Dataverse dataset-version response or JSON export")
    return data


def file_records(metadata):
    records = []
    for item in metadata["files"]:
        data = item["dataFile"]
        directory = item.get("directoryLabel", "")
        relative = "/".join(part for part in (directory, item["label"]) if part)
        records.append({
            "path": relative, "size": data["filesize"], "checksum": data["checksum"],
            "url": f"{SERVER}/api/access/datafile/{data['id']}?format=original",
            "restricted": item.get("restricted", False),
        })
    return sorted(records, key=lambda row: row["path"])


def select_files(records, subset, includes, excludes):
    return [row for row in records
            if (subset == "all" or row["path"].startswith("DEMO/"))
            and (not includes or any(fnmatch.fnmatchcase(row["path"], p) for p in includes))
            and not any(fnmatch.fnmatchcase(row["path"], p) for p in excludes)]


def extract_selected(selected, records, output):
    groups = defaultdict(list)
    for row in records:
        if ".tar.gz.part-" in row["path"]:
            groups[row["path"].split(".part-", 1)[0]].append(row)
    requested_groups = {row["path"].split(".part-", 1)[0] for row in selected
                        if ".tar.gz.part-" in row["path"]}
    # check completeness
    for name in sorted(requested_groups):
        for row in groups[name]:
            path = safe_path(output, row["path"])
            if not path.exists():
                raise ValueError(f"Missing split archive part: {path}. Download every part for this horse first.")
            verify_file(path, row["size"], row["checksum"])
    for row in selected:
        path = safe_path(output, row["path"])
        if path.name.endswith((".tar.gz", ".tar.xz", ".tar")):
            extract_tar(path, path.parent)
    for name in sorted(requested_groups):
        joined = safe_path(output, name)
        if joined.exists():
            raise ValueError(f"Joined archive already exists: {joined}. Move it before joining again.")
        total = sum(row["size"] for row in groups[name])
        if shutil.disk_usage(joined.parent).free < total:
            raise ValueError(f"Joining {name} needs an additional {human_size(total)} of disk space")
        partial = joined.with_name(joined.name + ".joining")
        with partial.open("wb") as target:
            for row in sorted(groups[name], key=lambda r: r["path"]):
                with safe_path(output, row["path"]).open("rb") as source:
                    shutil.copyfileobj(source, target, CHUNK_SIZE)
        partial.replace(joined)
        # Split video archives live in ID_n/VIDEO_DATA/ and contain VIDEO_DATA/.
        extract_tar(joined, joined.parent.parent)


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, default=Path("data/pferd"))
    parser.add_argument("--version", default="1.1", help="Dataverse version; default 1.1, or :latest-published")
    parser.add_argument("--subset", choices=("demo", "all"), default="demo")
    parser.add_argument("--include", action="append", default=[], help="Repeatable path glob; matches are combined with OR")
    parser.add_argument("--exclude", action="append", default=[], help="Repeatable path glob to exclude")
    parser.add_argument("--metadata", type=Path, help="Use an already saved Dataverse metadata JSON file")
    parser.add_argument("--download", action="store_true", help="Download selected files; otherwise preview only")
    parser.add_argument("--extract", action="store_true", help="Extract downloaded tar archives; requires --download")
    args = parser.parse_args(argv)
    if args.extract and not args.download:
        parser.error("--extract requires --download")
    token = os.environ.get("DATAVERSE_API_TOKEN")
    headers = {"X-Dataverse-key": token} if token else {}
    metadata = get_metadata(args.version, headers, args.metadata)
    records = file_records(metadata)
    selected = select_files(records, args.subset, args.include, args.exclude)
    if not selected:
        raise ValueError("No files match. Try --subset all, or change --include/--exclude")
    for row in selected:
        safe_path(args.out, row["path"])
        print(f"{human_size(row['size']):>12}  {row['path']}" + (" [restricted]" if row["restricted"] else ""))
    version = f"{metadata['versionNumber']}.{metadata['versionMinorNumber']}"
    print(f"\nPFERD {version}: {len(selected)} files; {human_size(sum(row['size'] for row in selected))}")
    print(f"Dataset access and terms: {DATA_PAGE}")
    if not args.download:
        print("Preview only. Add --download to fetch these files.")
        return 0
    save_json(args.out / "dataverse_metadata.json", metadata)
    save_json(args.out / "download_manifest.json", {"dataset": DOI, "version": version, "files": selected})
    for row in selected:
        download_file(row["url"], safe_path(args.out, row["path"]), row["size"], row["checksum"], headers)
    if args.extract:
        extract_selected(selected, records, args.out)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nInterrupted. Run the same command to resume partial downloads.")
        sys.exit(130)
    except (OSError, ValueError, KeyError, tarfile.TarError) as error:
        sys.exit(cli_error(error))
