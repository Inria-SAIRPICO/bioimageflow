"""DownloadImages — download images from URLs."""

from pathlib import Path
from typing import Any

from bioimageflow_core import (
    Arguments,
    Category,
    EnvironmentSpec,
    IOModel,
    ProcessingTool,
)

download_env = EnvironmentSpec(
    name="download",
    dependencies={"python": "3.12", "pip": ["requests"]},
)


class DownloadImages(ProcessingTool):
    """Download images from a list of URLs.

    Takes a list of URLs as a newline-separated string and downloads
    each one to a local directory. Returns one row per downloaded file.
    """
    name = "download_images"
    documentation = "Download images from URLs to a local directory."
    category = Category.UTILITIES
    tags = ["source", "download"]
    environment = download_env

    class Inputs(IOModel):
        urls: str
        output_dir: str = "./data"

    class Outputs(IOModel):
        path: Path
        filename: str
        url: str

    def process_row(self, arguments: Arguments) -> Any:
        import requests

        output_dir = Path(arguments.output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)

        urls = [u.strip() for u in arguments.urls.strip().split("\n") if u.strip()]
        results = []

        for url in urls:
            filename = url.rstrip("/").split("/")[-1]
            dest = output_dir / filename

            if not dest.exists():
                print(f"Downloading {url} ...")
                response = requests.get(url, timeout=120)
                response.raise_for_status()
                dest.write_bytes(response.content)
                print(f"  Saved to {dest} ({len(response.content)} bytes)")
            else:
                print(f"Already downloaded: {dest}")

            results.append(self.Outputs(
                path=dest,
                filename=filename,
                url=url,
            ))

        return results
