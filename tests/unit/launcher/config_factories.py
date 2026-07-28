"""Importable factories used by launcher configuration tests."""


def build(*, workers: int, credential: str | None = None) -> dict[str, object]:
    return {"workers": workers, "credential": credential}


def fail_with_credential(*, credential: str) -> None:
    raise RuntimeError(f"rejected credential {credential}")


def print_and_fail_with_credential(*, credential: str) -> None:
    print(f"factory received {credential}")
    raise RuntimeError(f"rejected credential {credential}")


not_callable = 3


def build_threads(*, max_threads: int = 1, run_dir: str | None = None):
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
        run_dir=run_dir or "runinfo",
    )
