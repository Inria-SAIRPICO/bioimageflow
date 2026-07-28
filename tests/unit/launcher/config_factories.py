"""Importable factories used by launcher configuration tests."""


def build(*, workers: int, credential: str | None = None) -> dict[str, object]:
    return {"workers": workers, "credential": credential}


not_callable = 3


def build_threads(*, max_threads: int = 1):
    """Return a minimal real Parsl config for launcher integration tests."""
    from parsl import Config
    from parsl.executors.threads import ThreadPoolExecutor

    return Config(
        executors=[
            ThreadPoolExecutor(
                label="threads",
                max_threads=max_threads,
            )
        ],
        retries=0,
    )
