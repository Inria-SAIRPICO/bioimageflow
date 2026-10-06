"""DownloadImages — download images into workflow-managed storage."""

from pathlib import Path
from hashlib import sha256
import re
from typing import Annotated, Any
from urllib.parse import unquote, urlsplit

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
    """Download a newline-separated list of URLs into the run assets directory."""

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
            filename = unquote(urlsplit(url).path.rstrip("/").split("/")[-1]) or "download"
            asset_name = re.sub(r"[^A-Za-z0-9._-]", "_", filename)
            if asset_name in {".", ".."}:
                asset_name = "download"
            url_dir = output_dir / sha256(url.encode("utf-8")).hexdigest()
            url_dir.mkdir(parents=True, exist_ok=True)
            destination = url_dir / asset_name
            if not destination.exists():
                with urlopen(url, timeout=120) as response:
                    destination.write_bytes(response.read())
            results.append(
                self.Outputs(path=destination, filename=filename, url=url)
            )
        return results
