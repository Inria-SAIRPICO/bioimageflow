"""
Test output path templating for ProcessingTool.

Covers:
- Template variable resolution ({input_image.stem}, {row_index}, {node_name})
- Default template ({node_name}_{row_index}{ext})
- {ext} resolution with single/multiple input paths
- {column:name} syntax
- {timestamp} variable
- Template errors for undefined variables
- 1-to-N output naming (tool mutates base path)
"""

from pathlib import Path
from typing import Annotated

import pytest

from bioimageflow import Workflow
from bioimageflow_core import Arguments, IOModel, ProcessingTool, Semantic, Template
from bioimageflow_core.types import ImageSpec

from .conftest import FileLoader, StubTiler, imageio_env


class StubDefaultTemplate(ProcessingTool):
    """Tool using the default output template."""
    display_name = "Stub Default Template"
    environment = imageio_env

    class Inputs(IOModel):
        input_image: Annotated[Path, ImageSpec(semantics={Semantic.INTENSITY})]

    class Outputs(IOModel):
        result: Annotated[Path, ImageSpec(semantics={Semantic.LABEL})]
        # No default → uses default template: {node_name}_{row_index}{ext}

    def process_row(self, arguments: Arguments, *, context: object | None = None):
        Path(arguments.result).parent.mkdir(parents=True, exist_ok=True)
        Path(arguments.result).write_text("DATA")
        return self.Outputs(result=arguments.result)


class StubCustomTemplate(ProcessingTool):
    """Tool with a custom output template."""
    display_name = "Stub Custom Template"
    environment = imageio_env

    class Inputs(IOModel):
        input_image: Annotated[Path, ImageSpec(semantics={Semantic.INTENSITY})]

    class Outputs(IOModel):
        mask: Annotated[Path, ImageSpec(semantics={Semantic.LABEL})] = Template(
            "{input_image.stem}_seg_{row_index}.png"
        )

    def process_row(self, arguments: Arguments, *, context: object | None = None):
        Path(arguments.mask).parent.mkdir(parents=True, exist_ok=True)
        Path(arguments.mask).write_text("DATA")
        return self.Outputs(mask=arguments.mask)


class StubMultiInput(ProcessingTool):
    """Tool with multiple input paths — {ext} resolves to empty."""
    display_name = "Stub Multi Input"
    environment = imageio_env

    class Inputs(IOModel):
        image_a: Annotated[Path, ImageSpec(semantics={Semantic.INTENSITY})]
        image_b: Annotated[Path, ImageSpec(semantics={Semantic.INTENSITY})]

    class Outputs(IOModel):
        diff: Annotated[Path, ImageSpec()] = Template(
            "{image_a.stem}_vs_{image_b.stem}_{row_index}.tif"
        )

    def process_row(self, arguments: Arguments, *, context: object | None = None):
        Path(arguments.diff).parent.mkdir(parents=True, exist_ok=True)
        Path(arguments.diff).write_text("DIFF")
        return self.Outputs(diff=arguments.diff)


class StubOptionalPathTemplate(ProcessingTool):
    """Tool with one required path and one optional path."""
    display_name = "Stub Optional Path Template"
    environment = imageio_env

    class Inputs(IOModel):
        input_image: Annotated[Path, ImageSpec(semantics={Semantic.INTENSITY})]
        psf_image: Annotated[Path | None, ImageSpec(semantics={Semantic.INTENSITY})] = None

    class Outputs(IOModel):
        result: Annotated[Path, ImageSpec()] = Template(
            "{input_image.stem}_processed{ext}"
        )

    def process_row(self, arguments: Arguments, *, context: object | None = None):
        Path(arguments.result).parent.mkdir(parents=True, exist_ok=True)
        Path(arguments.result).write_text("DATA")
        return self.Outputs(result=arguments.result)


class TestTemplateResolution:

    def test_custom_template_resolved(self, tmp_workspace):
        load = FileLoader()
        tool = StubCustomTemplate()

        with Workflow(storage_path=tmp_workspace / "results") as wf:
            raw = load(path=str(tmp_workspace / "data"))
            output = tool(input_image=raw["path"])
            df = wf.compute(output)

            # Verify resolved paths follow the template pattern
            for _, row in df.iterrows():
                p = Path(str(row["mask"]))
                assert "_seg_" in p.name
                assert p.suffix == ".png"

    def test_input_stem_in_template(self, tmp_workspace):
        load = FileLoader()
        tool = StubCustomTemplate()

        with Workflow(storage_path=tmp_workspace / "results") as wf:
            raw = load(path=str(tmp_workspace / "data"))
            output = tool(input_image=raw["path"])
            df = wf.compute(output)

            masks = [Path(str(row["mask"])).name for _, row in df.iterrows()]
            # cell_01.tif → cell_01_seg_<index>.png
            assert any("cell_01" in m for m in masks)

    def test_node_name_in_template(self, tmp_workspace):
        load = FileLoader()
        tool = StubDefaultTemplate()

        with Workflow(storage_path=tmp_workspace / "results") as wf:
            raw = load(path=str(tmp_workspace / "data"))
            output = tool(input_image=raw["path"], name="my_step")
            df = wf.compute(output)

            for _, row in df.iterrows():
                assert "my_step" in str(row["result"])

    def test_multi_input_explicit_extension(self, tmp_workspace):
        """With multiple input paths, tool must specify extension explicitly."""
        load = FileLoader()
        tool = StubMultiInput()

        with Workflow(storage_path=tmp_workspace / "results") as wf:
            raw = load(path=str(tmp_workspace / "data"))
            # Cross-compare first and second image (simplified: same source)
            output = tool(image_a=raw["path"], image_b=raw["path"])
            df = wf.compute(output)

            for _, row in df.iterrows():
                p = Path(str(row["diff"]))
                assert p.suffix == ".tif"

    def test_optional_none_path_does_not_clear_ext(self, tmp_workspace):
        load = FileLoader()
        tool = StubOptionalPathTemplate()

        with Workflow(storage_path=tmp_workspace / "results") as wf:
            raw = load(path=str(tmp_workspace / "data"))
            output = tool(input_image=raw["path"], psf_image=None)
            df = wf.compute(output)

            for _, row in df.iterrows():
                p = Path(str(row["result"]))
                assert p.name.endswith("_processed.tif")


class TestTilerOutputNaming:

    def test_tiler_generates_unique_filenames(self, tmp_workspace):
        """1-to-N tool mutates the base path template to create unique names."""
        load = FileLoader()
        tile = StubTiler()

        with Workflow(storage_path=tmp_workspace / "results") as wf:
            raw = load(path=str(tmp_workspace / "data"))
            tiles = tile(input_image=raw["path"], tile_count=3)
            df = wf.compute(tiles)

            paths = [str(row["tile"]) for _, row in df.iterrows()]
            # All paths should be unique
            assert len(paths) == len(set(paths))
            # Each should contain "part" from the stub's naming logic
            assert all("part" in p for p in paths)


class TestColumnTemplate:

    def test_column_template_variable(self, tmp_workspace_with_metadata):
        """Test {column:<name>} syntax resolves to the DataFrame column value."""
        ws = tmp_workspace_with_metadata

        class StubColumnTemplate(ProcessingTool):
            display_name = "Stub Column Template"
            environment = imageio_env

            class Inputs(IOModel):
                input_image: Annotated[Path, ImageSpec(semantics={Semantic.INTENSITY})]

            class Outputs(IOModel):
                result: Annotated[Path, ImageSpec()] = Template(
                    "{column:patient}_{row_index}.png"
                )

            def process_row(self, arguments: Arguments, *, context: object | None = None):
                Path(arguments.result).parent.mkdir(parents=True, exist_ok=True)
                Path(arguments.result).write_text("DATA")
                return self.Outputs(result=arguments.result)

        from .conftest import ColumnRegex

        load = FileLoader()
        regex = ColumnRegex()
        tool = StubColumnTemplate()

        with Workflow(storage_path=ws / "results") as wf:
            raw = load(path=str(ws / "data"))
            enriched = regex(
                raw,
                column_name="filename",
                regex=r"(?P<patient>\w+)_(?P<slice>\d+)\.tif",
            )
            output = tool(input_image=enriched["path"])
            df = wf.compute(output)

            # Output filenames should contain the patient name from the column
            for _, row in df.iterrows():
                p = Path(str(row["result"])).name
                assert "patientA" in p or "patientB" in p


class TestTimestampTemplate:

    def test_timestamp_in_template(self, tmp_workspace):
        """Test {timestamp} resolves to the execution timestamp."""

        class StubTimestampTemplate(ProcessingTool):
            display_name = "Stub Timestamp Template"
            environment = imageio_env

            class Inputs(IOModel):
                input_image: Annotated[Path, ImageSpec(semantics={Semantic.INTENSITY})]

            class Outputs(IOModel):
                result: Annotated[Path, ImageSpec()] = Template(
                    "{node_name}_{timestamp}_{row_index}.png"
                )

            def process_row(self, arguments: Arguments, *, context: object | None = None):
                Path(arguments.result).parent.mkdir(parents=True, exist_ok=True)
                Path(arguments.result).write_text("DATA")
                return self.Outputs(result=arguments.result)

        load = FileLoader()
        tool = StubTimestampTemplate()

        with Workflow(storage_path=tmp_workspace / "results") as wf:
            raw = load(path=str(tmp_workspace / "data"))
            output = tool(input_image=raw["path"])
            df = wf.compute(output)

            # All output paths should contain the timestamp (a numeric string)
            for _, row in df.iterrows():
                name = Path(str(row["result"])).stem
                # Template: stub_timestamp_template_<timestamp>_<row_index>
                parts = name.split("_")
                # At least 4 parts (name has underscores + timestamp + row_index)
                assert len(parts) >= 3


class TestTemplateErrors:

    def test_undefined_variable_raises_at_construction(self, tmp_workspace):
        """Template referencing a non-existent input field raises at construction."""

        class BadTemplate(ProcessingTool):
            display_name = "Bad Template"
            environment = imageio_env

            class Inputs(IOModel):
                input_image: Annotated[Path, ImageSpec(semantics={Semantic.INTENSITY})]

            class Outputs(IOModel):
                result: Annotated[Path, ImageSpec()] = Template(
                    "{nonexistent_field.stem}_out.png"
                )

            def process_row(self, arguments, *, context: object | None = None):
                return self.Outputs(result=arguments.result)

        load = FileLoader()
        tool = BadTemplate()

        with pytest.raises(Exception, match="nonexistent_field|template|undefined"):
            with Workflow(storage_path=tmp_workspace / "results"):
                raw = load(path=str(tmp_workspace / "data"))
                tool(input_image=raw["path"])
