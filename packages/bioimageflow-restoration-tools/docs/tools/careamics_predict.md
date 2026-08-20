# CAREamicsPredict

`CAREamicsPredict` runs CAREamics restoration inference from a checkpoint.
Use it when a workflow has a trained CAREamics, Noise2Void, CARE, or compatible restoration model available.

The required `checkpoint` input is passed to `CAREamist(checkpoint_path=...)`.
The tool calls `CAREamist.predict(pred_data=..., axes="YX", data_type="array")` and requires exactly one finite prediction with the same shape as the 2D input.
