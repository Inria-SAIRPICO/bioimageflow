"""DownloadImages — download images into workflow-managed storage."""

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
)


class DownloadImages(ProcessingTool):
    """Download a newline-separated list of URLs into the run assets directory."""

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
        filename: Annotated[str, GUIMeta(display_name="Filename")]
        url: Annotated[str, GUIMeta(display_name="Source URL")]

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

        results = []
        for url in (line.strip() for line in arguments.urls.splitlines()):
            if not url:
                continue
            destination = output_dir / url.rstrip("/").split("/")[-1]
            if not destination.exists():
                with urlopen(url, timeout=120) as response:
                    destination.write_bytes(response.read())
            results.append(
                self.Outputs(path=destination, filename=destination.name, url=url)
            )
        return results
