"""DownloadImages — download images from URLs."""

from pathlib import Path
from typing import Annotated, Any

from bioimageflow_core import (
    Arguments,
    Category,
    Connectable,
    EnvironmentSpec,
    GUIMeta,
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
        urls: Annotated[str, GUIMeta(
            display_name="URLs",
            description="Newline-separated list of image URLs to download.",
            connectable=Connectable.NEVER,
        )]
        output_dir: Annotated[str, GUIMeta(
            display_name="Output directory",
            description="Local directory where downloaded files are written. Created if it does not exist.",
            connectable=Connectable.NEVER,
        )] = "./data"

    class Outputs(IOModel):
        path: Annotated[Path, GUIMeta(
            display_name="Path",
            description="Local path of the downloaded file.",
        )]
        filename: Annotated[str, GUIMeta(
            display_name="Filename",
            description="Base name of the downloaded file.",
        )]
        url: Annotated[str, GUIMeta(
            display_name="Source URL",
            description="URL from which the file was downloaded.",
        )]

    def process_row(self, arguments: Arguments, *, context: Any = None) -> Any:
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
