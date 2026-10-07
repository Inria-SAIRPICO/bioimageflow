"""Ordinary Direct tools returning nominal output models through aliases."""

from pathlib import Path

from bioimageflow_core import EnvironmentSpec, IOModel, ProcessingTool, RowConsumption, Template


class AliasInputs(IOModel):
    value: int = 7


class AliasOutputs(IOModel):
    value: int
    result: Path = Template("alias_{row_index}.txt")


class UnrelatedAliasOutputs(IOModel):
    value: int
    result: Path = Template("alias_{row_index}.txt")


# Matching display names and fields confer no nominal declaration authority.
UnrelatedAliasOutputs.__name__ = "AliasOutputs"


def _write_output(model, arguments):
    destination = Path(arguments.result)
    destination.write_text(str(arguments.value))
    return model(value=arguments.value, result=destination)


class GlobalAliasRow(ProcessingTool):
    row_consumption = RowConsumption.MAPPED
    environment = EnvironmentSpec(name="output-alias-direct", dependencies={})
    Inputs = AliasInputs
    Outputs = AliasOutputs

    def process_row(self, arguments, *, context=None):
        return _write_output(AliasOutputs, arguments)


class GlobalAliasBatch(GlobalAliasRow):
    row_consumption = RowConsumption.MAPPED

    def process_batch(self, arguments_list, *, context=None):
        return [_write_output(AliasOutputs, arguments) for arguments in arguments_list]


class SelfOutputsRow(GlobalAliasRow):
    row_consumption = RowConsumption.MAPPED

    def process_row(self, arguments, *, context=None):
        return _write_output(self.Outputs, arguments)


class UnrelatedOutputsRow(GlobalAliasRow):
    row_consumption = RowConsumption.MAPPED

    def process_row(self, arguments, *, context=None):
        return _write_output(UnrelatedAliasOutputs, arguments)


DEFAULT_ALIAS_CALLS = []


class DefaultAliasOutputs(IOModel):
    value: int = 5
    result: Path = Template("default_alias_{row_index}.txt")


class DefaultGlobalAliasRow(ProcessingTool):
    row_consumption = RowConsumption.MAPPED
    environment = EnvironmentSpec(name="output-default-alias-direct", dependencies={})
    Inputs = IOModel
    Outputs = DefaultAliasOutputs

    def process_row(self, arguments, *, context=None):
        output = DefaultAliasOutputs(result=Path(arguments.result))
        DEFAULT_ALIAS_CALLS.append(output.value)
        output.result.write_text(str(output.value))
        return output
