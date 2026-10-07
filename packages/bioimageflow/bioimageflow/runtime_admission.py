"""Ready content admission and generation fencing, independent of pool ownership."""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any, Literal

from wetlands import (
    EnvironmentManager, EnvironmentNotReadyError, EnvironmentSpec,
    ManagedEnvironment, Operation, OperationEvent,
)

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
        replace_stale: bool,
        on_preparation: Callable[[Literal["creating", "updating", "reusing"], str | None], None],
        wait_for_operation: Callable[[Operation[Any], Callable[[OperationEvent], None] | None], Any],
        on_provision_event: Callable[[OperationEvent], None] | None,
        on_removal_event: Callable[[OperationEvent], None] | None,
    ) -> RuntimeContentReceipt | None:
        from wetlands import RuntimeContentUnavailableError

        key = (name, recipe.recipe_hash)
        if admissions is not None and key in admissions:
            receipt = admissions[key]
            if receipt is not None:
                self.validate(name, recipe, receipt)
            return receipt
        if admissions is not None and any(
            admitted_name == name and receipt is not None
            for (admitted_name, _), receipt in admissions.items()
        ):
            raise ValueError("This operation already admitted a different recipe for the selected runtime")

        prepared = False

        def provision_environment() -> ManagedEnvironment:
            nonlocal prepared
            prepared = True
            return wait_for_operation(
                self.provider.provision(name, recipe, replace_existing=False),
                on_provision_event,
            )

        def replace_environment(existing_hash: str) -> ManagedEnvironment:
            nonlocal owned_environment
            on_preparation("updating", existing_hash)
            # Successful physical close retires only the selected grants. A
            # failed close propagates before removal and keeps its retry owner.
            owned_environment = retire_pool(name)
            wait_for_operation(self.provider.remove(name), on_removal_event)
            return provision_environment()

        try:
            environment = self.provider.environment(name)
        except (FileNotFoundError, EnvironmentNotReadyError):
            if not provision:
                receipt = None
                if admissions is not None:
                    admissions[key] = receipt
                return receipt
            on_preparation("creating", None)
            environment = provision_environment()
        if environment.recipe_hash != recipe.recipe_hash:
            if not provision:
                if admissions is not None:
                    admissions[key] = None
                return None
            if not replace_stale:
                raise ValueError(
                    f"Environment {name!r} has a different ready recipe."
                )
            environment = replace_environment(environment.recipe_hash)
        # Provisioning may reuse another caller's managed publication. Its
        # actual recipe, not the requested operation, remains authoritative.
        if environment.recipe_hash != recipe.recipe_hash:
            raise ValueError("Prepared runtime does not match the requested recipe")
        try:
            receipt = environment.runtime_content_receipt()
        except RuntimeContentUnavailableError:
            if not provision:
                if admissions is not None:
                    admissions[key] = None
                return None
            environment = replace_environment(environment.recipe_hash)
            receipt = environment.runtime_content_receipt()
        if receipt.recipe_hash != recipe.recipe_hash:
            raise ValueError("Ready runtime receipt does not match the requested recipe")
        if owned_environment is not None and owned_environment.generation_id != receipt.generation_id:
            raise RuntimeError(
                f"Environment {name!r} changed while its pool remains owned; "
                "close that pool before admitting another generation."
            )
        if not prepared:
            on_preparation("reusing", environment.recipe_hash)
        self.validate(name, recipe, receipt)
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
