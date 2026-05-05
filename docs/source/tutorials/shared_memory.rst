Shared Memory
=============

BioImageFlow supports zero-copy array transfer between tools using shared
memory. This avoids writing intermediate arrays to disk, which is significantly
faster for large images.

Overview
--------

Instead of saving an image to a file and loading it in the next tool, you can:

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
       ProcessingTool, EnvironmentSpec, ImageShared, ImageSpec, Arguments, Template,
   )
   from bioimageflow_core.shm import create_shared_output

   class Preprocess(ProcessingTool):
       display_name = "Preprocess"
       environment = EnvironmentSpec(name="skimage", dependencies={})

       class Inputs:
           image: Annotated[Path, ImageSpec()]

       class Outputs:
           result: ImageShared()

       def process_row(self, arguments: Arguments) -> "Preprocess.Outputs":
           from skimage.io import imread
           import numpy as np

           img = imread(arguments.image).astype(np.float32)
           img = (img - img.mean()) / (img.std() + 1e-8)

           with create_shared_output(img) as shm_ref:
               return self.Outputs(result=shm_ref)

:func:`~bioimageflow_core.shm.create_shared_output` creates a
:class:`~bioimageflow_core.SharedArray` reference. The shared memory block
persists after the context manager exits --- only the local handle is closed.

Consuming shared arrays
-----------------------

.. code-block:: python

   from bioimageflow_core.io import load_image

   class Segment(ProcessingTool):
       display_name = "Segment"
       environment = EnvironmentSpec(name="cellpose", dependencies={})

       class Inputs:
           image: ImageShared()

       class Outputs:
           mask: Annotated[Path, ImageSpec(semantics={"label"})] = Template(
               "{node_name}_mask.tif"
           )

       def process_row(self, arguments: Arguments) -> "Segment.Outputs":
           with load_image(arguments.image, file_reader=None) as arr:
               # arr is a zero-copy numpy view of the shared memory
               mask = run_segmentation(arr)

           from skimage.io import imsave
           imsave(str(arguments.mask), mask)
           return self.Outputs(mask=arguments.mask)

:func:`~bioimageflow_core.io.load_image` detects that the input is a
``SharedArray`` and attaches to the shared memory block. The resulting numpy
array shares the same underlying buffer --- no data is copied.

Wiring it together
------------------

.. code-block:: python

   preprocess = Preprocess()
   segment = Segment()

   with Workflow() as wf:
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

- Large intermediate arrays that would be slow to write/read as files
- Pipelines where multiple tools process the same array
- GPU workflows where data stays in host memory between steps

When to prefer files:

- Results that need to persist across runs (caching)
- Outputs that users need to inspect visually
- Small data where I/O overhead is negligible
