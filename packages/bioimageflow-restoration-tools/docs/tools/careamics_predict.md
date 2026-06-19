# CAREamicsPredict

`CAREamicsPredict` runs CAREamics-style restoration inference from a checkpoint when `backend="careamics"`.

For deterministic examples and tests, `backend="baseline"` uses the package restoration baseline while keeping the same output contract.

Real CAREamics execution should be covered by `complete` and `model_runtime` tests.
