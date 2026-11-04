"""Worker-safe small-file tools for output ownership integration controls."""
from pathlib import Path
from bioimageflow_core import FileGroup, GENERAL_ENV, IOModel, ProcessingTool, RowConsumption, Template


class IndependentMasks(ProcessingTool):
    row_consumption = RowConsumption.MAPPED
    environment = GENERAL_ENV
    class Inputs(IOModel):
        image: Path
        threshold: int
    class Outputs(IOModel):
        mask: Path = Template("{image.stem}_mask.tif")
    def process_row(self, arguments, *, context=None):
        (context.work_dir / "science-called.txt").write_text("called")
        Path(arguments.mask).write_text(str(arguments.threshold))
        return self.Outputs(mask=arguments.mask)


class AggregateMask(ProcessingTool):
    row_consumption = RowConsumption.COLLECTIVE
    environment = GENERAL_ENV
    class Inputs(IOModel):
        image: Path
    class Outputs(IOModel):
        mask: Path = Template("mosaic.tif", group=FileGroup.once())
        count: int
    def process_batch(self, arguments_list, *, context=None):
        destination, = context.output_files["mask"]
        destination.path.write_text(str(len(arguments_list)))
        return [self.Outputs(mask=destination.path,count=len(arguments_list))]


class GroupedMasks(ProcessingTool):
    row_consumption = RowConsumption.COLLECTIVE
    environment = GENERAL_ENV
    collective_reference_inputs = ("image",)
    class Inputs(IOModel):
        image: Path
        threshold: int
    class Outputs(IOModel):
        mask: Path = Template("{image.stem}_mask.tif", group=FileGroup.by("image"))
        count: int
    def process_batch(self, arguments_list, *, context=None):
        outputs=[]
        for destination in context.output_files["mask"]:
            destination.path.write_text(str(len(destination.row_positions)))
            outputs.append(self.Outputs(mask=destination.path,count=len(destination.row_positions)))
        return outputs


class SilentAggregate(AggregateMask):
    row_consumption = RowConsumption.COLLECTIVE
    def process_batch(self, arguments_list, *, context=None):
        destination, = context.output_files["mask"]
        destination.path.write_text("artifact without table rows")
        return []


class SameNameMapped(ProcessingTool):
    row_consumption = RowConsumption.MAPPED
    environment = GENERAL_ENV
    class Inputs(IOModel):
        image: Path
    class Outputs(IOModel):
        image: Path = Template("{image.stem}_mask.tif")
    def process_row(self, arguments):
        Path(arguments.image).write_text("same-name")
        return self.Outputs(image=arguments.image)


class SameNameGrouped(ProcessingTool):
    row_consumption = RowConsumption.COLLECTIVE
    environment = GENERAL_ENV
    class Inputs(IOModel):
        image: Path
        threshold: int
    class Outputs(IOModel):
        image: Path = Template("{image.stem}_mask.tif", group=FileGroup.by("image"))
    def process_batch(self, arguments_list, *, context=None):
        results=[]
        for file in context.output_files["image"]:
            file.path.write_text(str(file.key['image']))
            results.append(self.Outputs(image=file.path))
        return results


class MutatingPlanMask(IndependentMasks):
    row_consumption = RowConsumption.MAPPED
    def process_batch(self, arguments_list, *, context=None):
        try:
            context.output_files['mask']=()
        except TypeError:
            (context.work_dir/'mutation-rejected.txt').write_text('rejected')
        return [self.Outputs(mask=context.assets_dir/'undeclared.tif') for row in arguments_list]
