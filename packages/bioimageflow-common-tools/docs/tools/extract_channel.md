# ExtractChannel

`ExtractChannel` writes one channel from a channel-first image. It expects the
channel axis to be the first axis, such as `CYX` or `CZYX`.

Inputs are `input_image` and zero-based `channel`. The output is
`output_image`, an intensity image named from the input stem and channel index
unless an explicit path is supplied.

Use it for simple channel separation before segmentation or measurement.
Invalid channel indexes raise the underlying NumPy indexing error, and images
that are not channel-first will produce incorrect slices.

```python
from bioimageflow_core import Arguments
from bioimageflow_common_tools import ExtractChannel

ExtractChannel().process_row(Arguments(input_image="cells.tif", channel=1))
```

## Dependencies and Core Libraries

BioImageFlow core APIs and imageio.

## Assumptions

The first axis is the channel axis and the requested channel index exists.

## Minimal Example

The example above writes the second channel from `cells.tif`.

## Expected Results

The output image is a single-channel slice with one fewer dimension than the
input.

## Failure Modes

Wrong axis order can silently select the wrong data; invalid channel indexes
raise indexing errors.
