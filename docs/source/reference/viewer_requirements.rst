Portable viewer requirements
============================

Viewer requirements describe software needed to open a particular output.
They are portable authoring metadata, separate from a tool's processing environment, local viewer installations, package discovery, and user preferences.
They never affect execution dependency resolution or computation/cache identity.

Worker-safe declarations
------------------------

Attach :class:`bioimageflow_core.ViewerSpec` to an output field with ``Annotated``:

.. code-block:: python

   from pathlib import Path
   from typing import Annotated

   from bioimageflow_core import (
       IOModel,
       NapariRequirement,
       PackageRequirement,
       ViewerSpec,
   )

   class Outputs(IOModel):
       tracks: Annotated[
           Path,
           ViewerSpec(
               napari=NapariRequirement(
                   required_packages=[
                       PackageRequirement("example-track-reader", ">=1.2,<2"),
                   ],
                   recommended_packages=[
                       PackageRequirement("example-track-editor", ">=1"),
                   ],
                   napari_version=">=0.5",
                   reader_id="example.track-reader",
               )
           ),
       ]

Distribution spelling is retained for display and ``normalized_name`` supplies the canonical PEP 503 comparison key.
Optional ``version`` and ``napari_version`` values are validated PEP 440 specifier sets.
``reader_id`` is an opaque launch-time identifier; it is not required to match a package and the library performs no napari discovery.

Node and workflow additions
---------------------------

``viewer_additions`` adds portable hard or recommended requirements to a configured tool output or a nested workflow's public output ID.
An addition intersects repeated version constraints, promotes a repeated recommendation to required when needed, and cannot remove the tool declaration.

.. code-block:: python

   node = GenericFiles()(
       viewer_additions={
           "path": ViewerSpec(
               NapariRequirement(required_packages=["example-reader"])
           )
       }
   )
   workflow.output(
       "tracks",
       node["path"],
       id="tracks-output",
       viewer_addition=ViewerSpec(
           NapariRequirement(recommended_packages=["example-editor"])
       ),
   )

Use ``node.get_output_viewer_spec(field)`` and ``workflow.get_output_viewer_spec(output_id_or_name)`` to read the recursively resolved declaration.
``serialize_output_schema`` and ``serialize_resolved_outputs`` expose the same value under each output's ``viewer`` key.

Graph, artifact, and retained-result boundaries
-----------------------------------------------

``Workflow.to_dict()`` emits a strict schema-version-2 editable graph.
``Workflow.to_archive_dict()`` emits a strict archive-version-2 portable artifact with ``custom_sources`` and a derived ``viewing_requirements`` snapshot keyed by scoped output identity.
ZIP export always writes that artifact form.
JSON export preserves the existing editable-graph boundary when no custom sources exist and writes the artifact envelope when custom sources require it.
Version-1 graphs and archives load as legacy declarations with no portable additions and serialize back as version 2.
Unknown fields are rejected according to their declared version.

``Workflow.inspect_viewing_requirements(...)`` and :func:`bioimageflow.inspect_viewing_requirements` read the derived archive snapshot without importing or installing tool packages.
The snapshot records ``known`` versus ``unknown`` entries and overall completeness; it is diagnostic export state, not an editable second source of truth.

Run-node ``result.json`` files use ``bioimageflow.run.node_result.v2`` and retain effective viewer metadata beside computation provenance.
``Storage.read_run_node_result(run_id, node_key)`` returns a typed :class:`bioimageflow.storage.RunNodeResult` with typed per-output viewer values.
Version-1 run-node files remain readable as having no retained viewer metadata.

Portable contracts contain no local viewer environment names, IDs, paths, preferences, credentials, process state, install commands, napari manifests, or plugin enabled/discovery state.
