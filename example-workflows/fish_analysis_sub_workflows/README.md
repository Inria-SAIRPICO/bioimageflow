# FISH Analysis With Sub-Workflows

## Analysis question

Can a repeated FISH spot-analysis pattern be packaged as reusable
sub-workflows while preserving the same biological output as the direct FISH
workflow?

## Data

The workflow uses the same public Cell Image Library FISH images as
`example-workflows/fish_analysis`. The URLs are listed in `workflow.py`; default
tests construct the graph without downloading the images.

## Expected outputs

- Reusable `SpotDetection` and `SpotAnalysis` sub-workflow nodes for FOLS2 and
  CSF1R channels.
- A terminal `avg_spots_per_nucleus` table with average spot counts per nucleus
  for both markers.
- Scoped internal sub-workflow nodes during execution.

## Test coverage

Default tests construct the graph and verify package imports, sub-workflow node
wiring, and terminal output naming. Public-data execution should stay behind a
slow marker because it requires downloads and external segmentation/Atlas
environments.
