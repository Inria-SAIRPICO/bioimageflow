Branching Workflows
===================

Real pipelines rarely form a straight line. BioImageFlow supports arbitrary
DAGs --- tools can consume outputs from multiple upstream nodes, and a single
node's output can feed into several downstream tools.

Fan-out: one source, multiple consumers
----------------------------------------

.. code-block:: python

   with Workflow() as wf:
       raw = loader(folder="/data")

       # Two independent branches from the same source
       masks = segment(image=raw["image"])
       enhanced = enhance(image=raw["image"])

       results = wf.compute(masks, enhanced)

.. code-block:: text

                +--> Segment
   LoadImages --|
                +--> Enhance

Both branches execute independently. The engine determines the optimal order
via topological sort.

Fan-in: multiple sources into one tool
---------------------------------------

Use column references from different nodes:

.. code-block:: python

   with Workflow() as wf:
       raw = loader(folder="/data")
       masks = segment(image=raw["image"])

       # Overlay needs both the original image and the mask
       overlay_result = overlay(
           image=raw["image"],
           mask=masks["mask"],
       )
       result = wf.compute(overlay_result)

.. code-block:: text

   LoadImages --> Segment --+
       |                    |
       +-----> Overlay <----+

The engine aligns rows by index --- row 0 of ``raw`` matches row 0 of
``masks``.

Diamond DAGs
------------

Combine fan-out and fan-in to form diamond patterns:

.. code-block:: python

   with Workflow() as wf:
       raw = loader(folder="/data")

       masks = segment(image=raw["image"])
       features = extract_features(image=raw["image"])

       # Combine results from both branches
       combined = merge(
           mask=masks["mask"],
           feature_vector=features["vector"],
       )
       result = wf.compute(combined)

.. code-block:: text

                +--> Segment --------+
   LoadImages --|                    +--> Merge
                +--> ExtractFeatures-+

Deep pipelines
--------------

Chain as many steps as needed:

.. code-block:: python

   with Workflow() as wf:
       raw = loader(folder="/data")
       preprocessed = denoise(image=raw["image"])
       masks = segment(image=preprocessed["denoised"])
       refined = postprocess(mask=masks["mask"])
       stats = measure(mask=refined["refined_mask"])
       result = wf.compute(stats)

Named nodes
-----------

By default, nodes are named after their tool. Give explicit names to
distinguish multiple uses of the same tool:

.. code-block:: python

   blur_low = GaussianBlur()
   blur_high = GaussianBlur()

   with Workflow() as wf:
       raw = loader(folder="/data")
       smooth = blur_low(image=raw["image"], sigma=1.0, name="blur_low")
       very_smooth = blur_high(image=raw["image"], sigma=5.0, name="blur_high")
       result = wf.compute(smooth, very_smooth)

Node names must be unique within a workflow.
