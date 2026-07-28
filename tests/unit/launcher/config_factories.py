"""Importable factories used by launcher configuration tests."""


def build(*, workers: int, credential: str | None = None) -> dict[str, object]:
    return {"workers": workers, "credential": credential}


not_callable = 3
