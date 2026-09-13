import argparse
from pathlib import Path
import sys
import tarfile
import urllib.parse

from download_pferd import cli_error, download_file, extract_tar, fetch_json, human_size, save_json

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
