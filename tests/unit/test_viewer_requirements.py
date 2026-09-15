from __future__ import annotations

import copy
import pickle
from pathlib import Path
from typing import Annotated

import pytest

from bioimageflow import Workflow, inspect_viewing_requirements, serialize_output_schema
from bioimageflow.launcher.payload import serialize_workflow_payload
from bioimageflow_core import (
    EnvironmentSpec,
    IOModel,
    NapariRequirement,
    PackageRequirement,
    ProcessingTool,
    RowConsumption,
    ViewerSpec,
    merge_viewer_specs,
)


DECLARED = ViewerSpec(
    napari=NapariRequirement(
        required_packages=[PackageRequirement("Example_Reader", ">=1,<3")],
        recommended_packages=[PackageRequirement("example-editor", ">=2")],
        napari_version=">=0.5",
        reader_id="example.reader",
    )
)


class ViewerTool(ProcessingTool):
    environment = EnvironmentSpec("viewer-contract-test", {})
    row_consumption = RowConsumption.MAPPED

    class Inputs(IOModel):
        pass

    class Outputs(IOModel):
        image: Annotated[Path, DECLARED]

    def process_row(self, arguments):
        return self.Outputs(image=Path("image.tif"))


def test_worker_safe_requirement_normalization_validation_and_pickle() -> None:
    requirement = PackageRequirement("My.Plugin_Name", "~=1.2")
    assert requirement.distribution == "My.Plugin_Name"
    assert requirement.normalized_name == "my-plugin-name"
    assert pickle.loads(pickle.dumps(requirement)) == requirement

    with pytest.raises(ValueError, match="distribution name"):
        PackageRequirement("not a package!")
    with pytest.raises(ValueError, match="PEP 440"):
        PackageRequirement("example", "latest")
    with pytest.raises(ValueError, match="duplicate"):
        NapariRequirement(required_packages=["Example", "example"])


def test_additive_merge_intersects_versions_and_required_wins() -> None:
    addition = ViewerSpec(
        NapariRequirement(
            required_packages=[
                PackageRequirement("example-reader", ">=2"),
                PackageRequirement("example_editor", "<6"),
            ],
            recommended_packages=[PackageRequirement("Other.Plugin", ">=4")],
            napari_version="<0.7",
        )
    )
    merged = merge_viewer_specs(DECLARED, addition)
    assert merged is not None and merged.napari is not None
    assert [item.normalized_name for item in merged.napari.required_packages] == [
        "example-reader",
        "example-editor",
    ]
    assert str(merged.napari.required_packages[0].version) == "<3,>=1,>=2"
    assert str(merged.napari.required_packages[1].version) == "<6,>=2"
    assert [item.normalized_name for item in merged.napari.recommended_packages] == [
        "other-plugin"
    ]
    assert merged.napari.napari_version == "<0.7,>=0.5"


def test_output_introspection_includes_strict_viewer_wire_metadata() -> None:
    viewer = serialize_output_schema(ViewerTool)["image"]["viewer"]
    requirement = viewer["napari"]["required_packages"][0]
    assert requirement == {
        "distribution": "Example_Reader",
        "normalized_name": "example-reader",
        "version": "<3,>=1",
    }
    assert viewer["napari"]["reader_id"] == "example.reader"


def test_graph_v2_archive_manifest_and_v1_normalization(tmp_path: Path) -> None:
    addition = ViewerSpec(
        NapariRequirement(required_packages=[PackageRequirement("extra-reader", ">=1")])
    )
    workflow = Workflow(storage_path=tmp_path, engine="direct")
    with workflow:
        node = ViewerTool()(viewer_additions={"image": addition})
        workflow.output("image", node["image"], id="public-image")

    graph = workflow.to_dict()
    assert graph["schema_version"] == 2
    assert graph["nodes"][0]["viewer_additions"]["image"] == addition.to_dict()
    loaded = Workflow.from_dict(graph, storage_path=tmp_path / "loaded")
    assert loaded.to_dict() == graph

    archive = workflow.to_archive_dict()
    assert archive["archive_version"] == 2
    snapshot = inspect_viewing_requirements(archive)
    assert snapshot.complete
    assert snapshot.outputs["@workflow::workflow-output/public-image"].viewer == (
        workflow.get_output_viewer_spec("public-image")
    )

    legacy = workflow.to_dict()
    legacy["schema_version"] = 1
    legacy["nodes"][0].pop("viewer_additions")
    legacy["interface"]["outputs"][0]["schema"].pop("viewer")
    normalized = Workflow.from_dict(legacy, storage_path=tmp_path / "legacy")
    assert normalized.to_dict()["schema_version"] == 2
    assert normalized.get_output_viewer_spec("public-image") == DECLARED


def test_v1_rejects_v2_fields_and_v2_rejects_unknown_fields(tmp_path: Path) -> None:
    graph = Workflow(storage_path=tmp_path).to_dict()
    graph["schema_version"] = 1
    graph["nodes"] = [{"name": "bad", "type": "workflow", "workflow": graph.copy(), "bindings": {}, "viewer_additions": {}}]
    with pytest.raises(ValueError, match="unknown fields"):
        Workflow.from_dict(graph, storage_path=tmp_path / "legacy")

    graph = Workflow(storage_path=tmp_path).to_dict()
    graph["future"] = True
    with pytest.raises(ValueError, match="fields must be exactly"):
        Workflow.from_dict(graph, storage_path=tmp_path / "future")


def test_unknown_viewer_fields_reject_at_every_v2_wire_layer(tmp_path: Path) -> None:
    declared_napari = DECLARED.napari
    assert declared_napari is not None
    workflow = Workflow(storage_path=tmp_path, engine="direct")
    with workflow:
        node = ViewerTool()()
        workflow.output("image", node["image"], id="public-image")
    graph = workflow.to_dict()

    malformed_values = [
        {"napari": None, "future": True},
        {
            "napari": {
                **declared_napari.to_dict(),
                "future": True,
            }
        },
        {
            "napari": {
                **declared_napari.to_dict(),
                "required_packages": [
                    {
                        **declared_napari.required_packages[0].to_dict(),
                        "future": True,
                    }
                ],
            }
        },
    ]
    for index, malformed in enumerate(malformed_values):
        candidate = copy.deepcopy(graph)
        candidate["nodes"][0]["viewer_additions"] = {"image": malformed}
        with pytest.raises(ValueError):
            Workflow.from_dict(candidate, storage_path=tmp_path / f"node-{index}")

    candidate = copy.deepcopy(graph)
    candidate["interface"]["outputs"][0]["viewer_addition"] = {
        "napari": None,
        "future": True,
    }
    with pytest.raises(ValueError):
        Workflow.from_dict(candidate, storage_path=tmp_path / "interface")

    candidate = workflow.to_archive_dict()
    candidate["viewing_requirements"]["outputs"][
        "@workflow::workflow-output/public-image"
    ]["future"] = True
    with pytest.raises(ValueError, match="Malformed viewing requirement entry"):
        Workflow.inspect_viewing_requirements(candidate)

    candidate = workflow.to_archive_dict()
    candidate["viewing_requirements"]["future"] = True
    with pytest.raises(ValueError, match="Malformed viewing requirements manifest"):
        Workflow.inspect_viewing_requirements(candidate)

    candidate = workflow.to_archive_dict()
    candidate["future"] = True
    with pytest.raises(ValueError, match="version-2 archive envelope"):
        Workflow.inspect_viewing_requirements(candidate)


def test_v1_normalizes_to_the_canonical_v2_launcher_graph(tmp_path: Path) -> None:
    canonical = Workflow(storage_path=tmp_path, engine="direct").to_dict()
    legacy = copy.deepcopy(canonical)
    legacy["schema_version"] = 1

    loaded = Workflow.from_dict(legacy, storage_path=tmp_path / "loaded")
    assert loaded.to_dict() == canonical
    launcher_payload = serialize_workflow_payload(loaded)
    assert launcher_payload["kind"] == "graph_v2"
    assert launcher_payload["payload"] == canonical


def test_archive_snapshot_is_inspectable_before_missing_tool_resolution(tmp_path: Path) -> None:
    workflow = Workflow(storage_path=tmp_path, engine="direct")
    with workflow:
        node = ViewerTool()()
        workflow.output("image", node["image"], id="public-image")
    archive = workflow.to_archive_dict()
    archive["workflow"]["nodes"][0]["tool_module"] = "missing.viewer.tool"
    archive["workflow"]["nodes"][0].pop("source_module", None)
    archive["custom_sources"] = []

    snapshot = Workflow.inspect_viewing_requirements(archive)
    assert snapshot.complete
    assert snapshot.outputs["@workflow::workflow-output/public-image"].viewer == DECLARED
    with pytest.raises(Exception):
        Workflow.from_dict(
            archive,
            storage_path=tmp_path / "missing",
            auto_install=False,
        )


def test_python_authored_viewer_metadata_survives_portable_capture(
    tmp_path: Path,
) -> None:
    definition = tmp_path / "workflow_definition.py"
    definition.write_text(
        """\
from pathlib import Path
from typing import Annotated

from bioimageflow import Workflow
from bioimageflow_core import (
    EnvironmentSpec, IOModel, NapariRequirement, ProcessingTool,
    RowConsumption, ViewerSpec,
)

VIEWER = ViewerSpec(NapariRequirement(required_packages=["python-reader"]))

class PythonViewerTool(ProcessingTool):
    environment = EnvironmentSpec("python-viewer-test", {})
    row_consumption = RowConsumption.MAPPED
    class Inputs(IOModel):
        pass
    class Outputs(IOModel):
        image: Annotated[Path, VIEWER]
    def process_row(self, arguments):
        return self.Outputs(image=Path("image.tif"))

def build_workflow(*, storage_path):
    workflow = Workflow(storage_path=storage_path, engine="direct")
    with workflow:
        image = PythonViewerTool()()
        workflow.output("image", image["image"], id="public-image")
    return workflow
""",
        encoding="utf-8",
    )

    workflow = Workflow.from_python(definition, storage_path=tmp_path / "runtime")
    viewer = workflow.get_output_viewer_spec("public-image")
    assert viewer is not None and viewer.napari is not None
    assert viewer.napari.required_packages[0].normalized_name == "python-reader"
    archive = workflow.to_archive_dict()
    assert archive["custom_sources"]
    assert Workflow.inspect_viewing_requirements(archive).outputs[
        "@workflow::workflow-output/public-image"
    ].viewer == viewer


def test_viewer_metadata_does_not_change_execution_cache_identity(tmp_path: Path) -> None:
    from bioimageflow.engine.common import source_processing_signature_material

    workflow = Workflow(storage_path=tmp_path, engine="direct")
    with workflow:
        plain = ViewerTool()(name="plain")
        augmented = ViewerTool()(
            name="augmented",
            viewer_additions={
                "image": ViewerSpec(
                    NapariRequirement(required_packages=["another-reader"])
                )
            },
        )
    assert source_processing_signature_material(plain) == (
        source_processing_signature_material(augmented)
    )


def test_published_requirements_inherit_and_augment_recursively(tmp_path: Path) -> None:
    child_addition = ViewerSpec(
        NapariRequirement(recommended_packages=["child-helper"])
    )
    invocation_addition = ViewerSpec(
        NapariRequirement(required_packages=["invocation-reader"])
    )
    boundary_addition = ViewerSpec(
        NapariRequirement(recommended_packages=["boundary-helper"])
    )
    child = Workflow(storage_path=tmp_path / "child", name="child", engine="direct")
    with child:
        source = ViewerTool()()
        child.output(
            "image",
            source["image"],
            id="child-image",
            viewer_addition=child_addition,
        )
    parent = Workflow(storage_path=tmp_path / "parent", name="parent", engine="direct")
    with parent:
        nested = child(
            name="nested",
            viewer_additions={"child-image": invocation_addition},
        )
        parent.output(
            "image",
            nested["image"],
            id="parent-image",
            viewer_addition=boundary_addition,
        )

    resolved = parent.get_output_viewer_spec("parent-image")
    assert resolved is not None and resolved.napari is not None
    assert {item.normalized_name for item in resolved.napari.required_packages} == {
        "example-reader",
        "invocation-reader",
    }
    assert {item.normalized_name for item in resolved.napari.recommended_packages} == {
        "example-editor",
        "child-helper",
        "boundary-helper",
    }
    restored = Workflow.from_dict(
        parent.to_dict(include_custom_tools=True),
        storage_path=tmp_path / "restored",
    )
    assert restored.get_output_viewer_spec("parent-image") == resolved
