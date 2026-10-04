Pass Arrays Through Shared Memory
=================================

BioImageFlow shares numeric arrays through scoped NPY2 file-backed mmap storage.
Allocation copies data once; subsequent mapped reads use zero-copy NumPy views.
This storage contract does not promise a measured speedup over ordinary files.

Overview
--------

Mapped numeric backing shares one admitted NPY2 file rather than repeatedly encoding an image format.
It still uses files and performs an initial copy; zero-copy views do not imply an observed latency gain.
You can:

1. Write a numpy array to shared memory with
   :func:`~bioimageflow_core.shm.create_shared_output`
2. Annotate the output with :func:`~bioimageflow_core.ImageShared` instead of
   ``Annotated[Path, ImageSpec(...)]``
3. Read it back in the next tool with :func:`~bioimageflow_core.io.load_image`
   --- which returns a zero-copy numpy view

Producing shared arrays
-----------------------

.. code-block:: python

   from pathlib import Path
   from typing import Annotated

   from bioimageflow_core import (
       ProcessingTool, IOModel, GENERAL_ENV, ImageShared, ImageSpec, Arguments, Template,
       RowConsumption,
   )
   from bioimageflow_core.shm import create_shared_output

   class Preprocess(ProcessingTool):
       display_name = "Preprocess"
       environment = GENERAL_ENV
       row_consumption = RowConsumption.MAPPED

       class Inputs(IOModel):
           image: Annotated[Path, ImageSpec()]

       class Outputs(IOModel):
           result: ImageShared()

       def process_row(self, arguments: Arguments) -> "Preprocess.Outputs":
           from skimage.io import imread
           import numpy as np

           img = imread(arguments.image).astype(np.float32)
           img = (img - img.mean()) / (img.std() + 1e-8)

           with create_shared_output(img) as shm_ref:
               return self.Outputs(result=shm_ref)

:func:`~bioimageflow_core.shm.create_shared_output` creates a
:class:`~bioimageflow_core.SharedArray` reference in the active explicit allocation scope.
Lexical exit does not delete its backing; returned references retain a reachable controller owner.

Consuming shared arrays
-----------------------

.. code-block:: python

   from bioimageflow_core.io import load_image

   class Segment(ProcessingTool):
       display_name = "Segment"
       environment = GENERAL_ENV
       row_consumption = RowConsumption.MAPPED

       class Inputs(IOModel):
           image: ImageShared()

       class Outputs(IOModel):
           mask: Annotated[Path, ImageSpec(semantics={"label"})] = Template(
               "{node_name}_mask.tif"
           )

       def process_row(self, arguments: Arguments) -> "Segment.Outputs":
           # Method sketch: supply the scientific run_segmentation implementation.
           with load_image(arguments.image, file_reader=None) as arr:
               # arr is a zero-copy numpy view of the shared memory
               mask = run_segmentation(arr)

           from skimage.io import imsave
           imsave(str(arguments.mask), mask)
           return self.Outputs(mask=arguments.mask)

:func:`~bioimageflow_core.io.load_image` detects that the input is a
``SharedArray`` and maps its admitted numeric backing file.
The resulting NumPy array and derived views retain their mapping beyond lexical exit.

Wiring it together
------------------

.. code-block:: python

   # Workflow and loader are supplied by the containing workflow example.
   preprocess = Preprocess()
   segment = Segment()

   with Workflow(storage_path="./results") as wf:
       raw = loader(folder="/data")
       preprocessed = preprocess(image=raw["image"])
       masks = segment(image=preprocessed["result"])
       result = wf.compute(masks)

Type annotations
----------------

:func:`~bioimageflow_core.ImageShared` produces a shared-memory image
annotation by setting ``formats={"memory"}``. File-based image fields use
``Annotated[Path, ImageSpec(...)]``:

.. code-block:: python

   # File-based: Annotated[Path, ImageSpec(...)]
   image: Annotated[Path, ImageSpec(semantics={"intensity"}, layouts={"YX"})]

   # Memory-based: Annotated[SharedArray, ImageSpec(..., formats={"memory"})]
   image: ImageShared(semantics={"intensity"}, layouts={"YX"})

Type compatibility checking applies the same rules to both --- semantics,
layouts, and dtypes are checked at graph-construction time.

When to use shared memory
-------------------------

- Pipelines that benefit from sharing mapped numeric backing after the initial copy
- Pipelines where multiple tools process the same array
- GPU workflows where data stays in host memory between steps

When to prefer files:

- Results that need to persist across runs (caching)
- Outputs that users need to inspect visually
- Small data where I/O overhead is negligible

Explicit ownership and cleanup
------------------------------

Workflow execution establishes a controller scope automatically; standalone helper calls require an explicit scope.
Returned DataFrames retain their reference owners even when a temporary Workflow is collected.
Output-only producers still need an admitted output allocation namespace; a task with no shared capability needs no such scope.
Workflow/context exit and execution completion do not close returned arrays.

.. code-block:: python

   from bioimageflow_core import SharedMemoryContext
   from bioimageflow_core.shm import create_shared_output, open_shared_array

   owner = SharedMemoryContext("./shared-arrays", max_bytes=512 * 1024 * 1024)
   with owner.activate(), create_shared_output(data) as reference:
       pass
   with open_shared_array(reference) as array:
       view = array[1:]
   status = owner.close()  # pending while mapped views or worker grants remain

Explicit ``close()`` or ``release(reference)`` refuses new controller access.
Already admitted workers and existing base/slice/asarray views retain backing until physical drain.
``CleanupStatus`` reports pending readers, grants, files and errors; Windows deletion failures stay pending for retry.
Workers borrow scopes and never own deletion; public pool close releases grants only after successful physical drain.
No resource tracker, private unregister or automatic context-exit unlink is used.
GC does not initiate accepted-result or whole-owner release; final-reader drainage may finish a previously requested explicit cleanup.
Borrowed and accepted scientific inputs are immutable to consumers by contract; allocate a separate work/output value before modifying pixels.

The accepted lifecycle target requires accessible public owner/status information and exact result/group release, including results callers discard between repeated runs.
Releasing one result must not close unrelated groups.
A reused worker pool can retain physical grants: idle or quota-pressure retirement must physically drain its pool before reclamation, without forcing live mapped views closed.
Finite quota exhaustion refuses clearly if enough space cannot safely be reclaimed.
The exact result/group API and these conformance obligations remain subject to the ordered test and code review; this guide does not invent a cleanup method.
See :doc:`/specs` §8 for the normative ownership and admission contract.
