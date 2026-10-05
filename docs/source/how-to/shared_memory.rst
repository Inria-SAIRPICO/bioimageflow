Pass Arrays Through Shared Memory
=================================

BioImageFlow shares numeric arrays through scoped NPY2 file-backed mmap storage.
Allocation copies producer data, and publication makes one independent sealed numeric copy.
Subsequent accepted reads use read-only zero-copy NumPy views; already sealed pass-through values are not copied again.
This storage contract does not promise a measured speedup over ordinary files.

Overview
--------

Mapped numeric backing shares one admitted NPY2 file rather than repeatedly encoding an image format.
It uses files, allocation and one publication copy; zero-copy reads do not imply a measured latency gain.
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
Both ``with owner:`` and ``owner.activate()`` are nestable lexical activation contexts and restore the exact previous caller on exit.
Root and task namespace acquisition use exclusive creation; failed marker or quota initialization retires only the newly acquired, identity-checked namespace.
An existing target is refused without adoption or deletion.
If rollback cannot finish, the original exception remains primary and its ``shared_scope_cleanup`` diagnostic records the retained root, identity and cleanup errors for explicit caller recovery.

.. code-block:: python

   from bioimageflow_core import SharedMemoryContext
   from bioimageflow_core.shm import create_shared_output, open_shared_array

   owner = SharedMemoryContext("./shared-arrays", max_bytes=512 * 1024 * 1024)
   with owner.activate(), create_shared_output(data) as reference:
       pass
   accepted = owner.publish(reference)  # one independent accepted snapshot
   with open_shared_array(accepted) as array:
       view = array[1:]  # read-only zero-copy view
   status = owner.close()  # pending while mapped views or worker grants remain

Explicit ``close()`` or ``release(reference)`` refuses new controller access.
Already admitted workers and existing base/slice/asarray views retain backing until physical drain.
``CleanupStatus`` reports pending readers, grants, leases, files and errors; Windows deletion failures stay pending for retry.
Workers borrow scopes and never own deletion; public pool close releases grants only after successful physical drain.
No resource tracker, private unregister or automatic context-exit unlink is used.
Loss of the final returned reference releases its exact group leases, without closing a whole owner or unrelated results.
Retained unopened references and mapped views remain pins; final-reader drainage may finish a pending group release.
Borrowed and accepted scientific inputs are immutable to consumers by contract; allocate a separate work/output value before modifying pixels.


A producer allocation remains writable until it is independently published.
``publish(reference)`` or recursive ``publish_value(value)`` copies each distinct mutable backing once in that admission and persists its read-only identity and digest.
Already sealed pass-through references reuse their physical backing and exact bound owner.
``open_shared_array(accepted, writable=True)`` refuses, and SDK image writers refuse existing canonical/hardlink/symlink aliases whose backing is read-only.
This does not sandbox arbitrary trusted Python or hostile filesystem replacement.
``content_identity(reference)`` reads the seal digest without rehashing accepted pixels; an unsealed producer identity is only a current-byte planning preview and cannot prove stability across later writes.
Actual cached execution publishes inputs before computing its authoritative identity.

Exact returned groups
---------------------

The ordinary Workflow return remains a pandas DataFrame.
Use ``result_groups(value)`` on that captured return rather than infer ownership from DataFrame attrs or a latest-result pointer.

.. code-block:: python

   from bioimageflow import result_groups

   groups = result_groups(result)
   statuses = [group.release() for group in groups]
   # Existing mapped views may keep a release pending.
   statuses = [group.status() for group in groups]

Every ``ResultGroup`` exposes captured ``node_name``, ``group_id`` and ``consumed_rows`` metadata plus ``release()``, ``status()`` and ``released``.
An explicitly retained handle intentionally keeps its leases alive until release or discard.
A caller-provided ``WorkflowExecutionContext.result_groups`` weakly observes live handles; the context never silently pins discarded result data.
Array-free results have no shared-array groups; unaccepted or foreign bindings are refused.
New per-group descriptors preserve the same sealed physical locator and exact original owner, while release of one descriptor’s group leaves another group and existing views usable.
This preserves resource identity, not Python descriptor-object identity.

A reused worker pool may retain physical grants.
Call the owning manager’s ``retire_idle(env_name)`` to physically close an idle selected pool before reclamation; a close error retains ownership for explicit retry.
Never force-close live mapped views or release unrelated environments.
Finite quota exhaustion still refuses if safe reclamation is unavailable; this does not promise an automatic quota-pressure scheduler.
Owner-wide physical grant accounting may conservatively delay cleanup behind another task in the same owner.
Arbitrary Python objects in DataFrames are not sealed by these array/group APIs.
See :doc:`/specs` §8 for the normative ownership and admission contract.

Native NumPy values
-------------------

``accept_native_array(array)`` returns a plain NumPy ndarray with immutable bytes-backed data and independent shape/dtype metadata.
Acceptance copies a mutable producer once; changing a retained producer view cannot alter accepted pixels, and the accepted array cannot enable writable access with ``setflags``.
Re-admitting a C-contiguous array already backed by immutable bytes creates a new ndarray descriptor over the same data without another pixel copy.
A read-only flag or a read-only memoryview over mutable storage does not establish immutable backing.
Recursive ``publish_value`` deduplicates repeated native producers within one admission and returns separate view descriptors for their consumers.
``accept_native_values(value)`` applies the same native acceptance to dictionary, list and tuple leaves together, preserving other leaves under their existing contract.
Processing callback outputs are captured before the next row callback can reuse or mutate a producer buffer; batch outputs are captured after their single callback returns.
Workers make independent sealed shared-array output copies only in their admitted output namespace, while controller validation and physical grant drain still own result disposition and deletion.
Already sealed inputs are reused, and failed or unreturned worker copies remain controller-owned until physical retirement.
Native values retain their dtype, shape and values, including zero-dimensional, empty, structured and noncontiguous inputs; acceptance normalizes storage to C layout without preserving source strides.
Python-object dtypes and dtype metadata, including nested structured-field metadata, are refused.
Native cache assets hydrate as native ndarray values rather than SharedArray references; their immutable bytes lifetime needs no shared-array result-group lease.
This finite array contract does not provide a generic deepcopy policy for arbitrary Python objects.
