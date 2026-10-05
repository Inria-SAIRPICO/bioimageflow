"""Ready content admission and generation fencing, independent of pool ownership."""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from wetlands import EnvironmentManager, EnvironmentNotReadyError, EnvironmentSpec, ManagedEnvironment

if TYPE_CHECKING:
    from wetlands import RuntimeContentReceipt


class RuntimeAdmission:
    def __init__(self, provider: EnvironmentManager) -> None:
        self.provider = provider

    def admit(
        self, name: str, recipe: EnvironmentSpec, *, provision: bool,
        admissions: dict[tuple[str, str], RuntimeContentReceipt | None] | None,
        owned_environment: ManagedEnvironment | None,
        retire_pool: Callable[[str], ManagedEnvironment | None],
    ) -> RuntimeContentReceipt | None:
        from wetlands import RuntimeContentUnavailableError

        key = (name, recipe.recipe_hash)
        if admissions is not None and key in admissions:
            return admissions[key]
        try:
            environment = self.provider.environment(name)
        except (FileNotFoundError, EnvironmentNotReadyError):
            if not provision:
                receipt = None
                if admissions is not None:
                    admissions[key] = receipt
                return receipt
            environment = self.provider.provision(
                name, recipe, replace_existing=False,
            ).wait_for()
        if environment.recipe_hash != recipe.recipe_hash:
            if not provision:
                if admissions is not None:
                    admissions[key] = None
                return None
            raise ValueError(
                f"Environment {name!r} has a different ready recipe."
            )
        try:
            receipt = environment.runtime_content_receipt()
        except RuntimeContentUnavailableError:
            if not provision:
                if admissions is not None:
                    admissions[key] = None
                return None
            owned_environment = retire_pool(name)
            self.provider.remove(name).wait_for()
            environment = self.provider.provision(
                name, recipe, replace_existing=False,
            ).wait_for()
            receipt = environment.runtime_content_receipt()
        if receipt.recipe_hash != recipe.recipe_hash:
            raise ValueError("Ready runtime receipt does not match the requested recipe")
        if owned_environment is not None and owned_environment.generation_id != receipt.generation_id:
            raise RuntimeError(
                f"Environment {name!r} changed while its pool remains owned; "
                "close that pool before admitting another generation."
            )
        if admissions is not None:
            admissions[key] = receipt
        return receipt

    def validate(
        self, name: str, recipe: EnvironmentSpec, receipt: RuntimeContentReceipt,
    ) -> ManagedEnvironment:
        environment = self.provider.environment(name)
        if (
            receipt.recipe_hash != recipe.recipe_hash
            or environment.recipe_hash != receipt.recipe_hash
            or environment.generation_id != receipt.generation_id
            or environment.lockfile_hash != receipt.lockfile_hash
        ):
            raise RuntimeError(
                f"Environment {name!r} changed after runtime admission."
            )
        return environment

