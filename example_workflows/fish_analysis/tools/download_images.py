"""DownloadImages — download images from URLs."""

from pathlib import Path
from typing import Annotated, Any

from bioimageflow_core import (
    Arguments,
    Category,
    Connectable,
    ExecutionContext,
    GENERAL_ENV,
    GUIMeta,
    IOModel,
    ProcessingTool,
    RowConsumption,
)


class DownloadImages(ProcessingTool):
    """Download images from a list of URLs.

    Takes a list of URLs as a newline-separated string and downloads
    each one to a local directory. Returns one row per downloaded file.
    """

    row_consumption = RowConsumption.MAPPED

    name = "download_images"
    documentation = (
        "Download images from URLs into this workflow run's assets directory."
    )
    category = Category.UTILITIES
    tags = ["source", "download"]
    environment = GENERAL_ENV

    class Inputs(IOModel):
        urls: Annotated[
            str,
            GUIMeta(
                display_name="URLs",
                description="Newline-separated list of image URLs to download.",
                connectable=Connectable.NEVER,
            ),
        ]

    class Outputs(IOModel):
        path: Annotated[
            Path,
            GUIMeta(
                display_name="Path",
                description="Local path of the downloaded file.",
            ),
        ]
        filename: Annotated[
            str,
            GUIMeta(
                display_name="Filename",
                description="Base name of the downloaded file.",
            ),
        ]
        url: Annotated[
            str,
            GUIMeta(
                display_name="Source URL",
                description="URL from which the file was downloaded.",
            ),
        ]

    def process_row(
        self,
        arguments: Arguments,
        *,
        context: ExecutionContext | None = None,
    ) -> Any:
        from urllib.request import urlopen

        if context is None:
            raise RuntimeError("DownloadImages requires an execution context.")

        output_dir = context.assets_dir
        output_dir.mkdir(parents=True, exist_ok=True)

        urls = [u.strip() for u in arguments.urls.strip().split("\n") if u.strip()]
        results = []

        for url in urls:
            filename = url.rstrip("/").split("/")[-1]
            dest = output_dir / filename

            if not dest.exists():
                print(f"Downloading {url} ...")
                with urlopen(url, timeout=120) as response:
                    content = response.read()
                dest.write_bytes(content)
                print(f"  Saved to {dest} ({len(content)} bytes)")
            else:
                print(f"Already downloaded: {dest}")

            results.append(
                self.Outputs(
                    path=dest,
                    filename=filename,
                    url=url,
                )
            )

        return results
