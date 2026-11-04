"""Exact destinations are checked by ownership, independently of callback batching."""
from pathlib import Path
from types import SimpleNamespace

import pytest

from bioimageflow import OutputPlanningEvidence, OutputPathCollisionError, OutputGroupDestinationMismatchError
from bioimageflow.storage import CacheCorruptionError
from bioimageflow.template import get_output_templates
from bioimageflow_core import FileGroup, IOModel, PathBase, RowConsumption, Template


class Outputs(IOModel):
    mask: Path = Template("{image.stem}_mask{ext}")
    source: Path


class Grouped(IOModel):
    mask: Path = Template("{image.stem}_mask{ext}", group=FileGroup.by("image"))


class Once(IOModel):
    mask: Path = Template("mosaic.tif", group=FileGroup.once())


def evidence(tmp_path, outputs=Outputs, rows=None, references=(), batch=None, **kwargs):
    if rows is None:
        rows = [{"image": Path("/data/image.tif"), "threshold": i, "row_index": str(i), "_ext": ".tif"} for i in range(6)]
    return OutputPlanningEvidence.capture(node_id="nested/atlas", assets_dir=tmp_path,
            tool=SimpleNamespace(Outputs=outputs, row_consumption=RowConsumption.MAPPED if outputs is Outputs else RowConsumption.COLLECTIVE), row_contexts=rows,
            row_indexes=[str(i) for i in range(len(rows))], reference_contexts=references,
            batch_context=batch or {}, **kwargs)


def test_parameter_sweep_refuses_with_exact_expansion_and_verified_correction(tmp_path):
    with pytest.raises(OutputPathCollisionError) as error:
        evidence(tmp_path).check_templates()
    report = error.value.report
    assert report["total_owners"] == 6
    assert report["conflicts"][0]["total_members"] == 6
    assert report["conflicts"][0]["path"] == "image_mask.tif"
    assert report["conflicts"][0]["members"][0]["parts"] == [
        {"kind": "token", "text": "{image.stem}", "value": "image"},
        {"kind": "literal", "text": "_mask", "value": "_mask"},
        {"kind": "token", "text": "{ext}", "value": ".tif"},
    ]
    plan = error.value.evidence.check_templates(report["suggested_templates"])
    assert len({item.path for item in plan.output_files["mask"]}) == 6
    assert list(tmp_path.iterdir()) == []


def test_custom_correction_can_reference_new_input_values(tmp_path):
    with pytest.raises(OutputPathCollisionError) as error:
        evidence(tmp_path).check_templates()
    plan = error.value.evidence.check_templates({"mask": "{image.stem}_{threshold}.ome.tif"})
    assert plan.output_files["mask"][5].path.name == "image_5.ome.tif"


def test_once_group_is_one_owner_for_many_rows(tmp_path):
    plan = evidence(tmp_path, Once).check_templates()
    owner, = plan.output_files["mask"]
    assert owner.row_positions == tuple(range(6))
    assert len(set(row["mask"] for row in plan.row_paths)) == 1


def test_same_key_shares_one_owner_but_different_paths_with_equal_stems_collide(tmp_path):
    assert len(evidence(tmp_path, Grouped).check_templates().output_files["mask"]) == 1
    rows = [{"image": Path(f"/{source}/image.tif"), "row_index": str(i), "_ext": ".tif"}
            for i, source in enumerate(("a", "a", "b", "b"))]
    with pytest.raises(OutputPathCollisionError) as error:
        evidence(tmp_path, Grouped, rows).check_templates()
    report = error.value.report
    assert report["total_owners"] == 2
    assert "{group_index}" in report["suggested_templates"]["mask"]
    plan = error.value.evidence.check_templates(report["suggested_templates"])
    assert [item.row_positions for item in plan.output_files["mask"]] == [(0, 1), (2, 3)]


def test_row_index_splits_one_group_and_group_index_repairs_it(tmp_path):
    with pytest.raises(OutputGroupDestinationMismatchError) as error:
        evidence(tmp_path, Grouped).check_templates({"mask": "mask_{row_index}.tif"})
    assert error.value.report["code"] == "output_group_destination_mismatch"
    assert error.value.report["suggested_templates"] == {"mask": "mask_{group_index}.tif"}


def test_reference_only_and_none_constant_groups_have_no_fabricated_rows(tmp_path):
    reference = {"image": Path("/a/blank.tif"), "row_index": "reference", "_ext": ".tif"}
    owner, = evidence(tmp_path, Grouped, rows=[], references=[reference]).check_templates().output_files["mask"]
    assert owner.row_positions == () and owner.reference_positions == (0,)
    class NoneGroup(IOModel):
        mask: Path = Template("blank.tif", group=FileGroup.by("image"))
    owner, = evidence(tmp_path, NoneGroup, rows=[], batch={"image": None}).check_templates().output_files["mask"]
    assert owner.key == {"image": None} and owner.row_positions == ()


def test_two_output_fields_are_distinct_owners(tmp_path):
    class Two(IOModel):
        first: Path = Template("one.tif", group=FileGroup.once())
        second: Path = Template("one.tif", group=FileGroup.once())
    with pytest.raises(OutputPathCollisionError):
        evidence(tmp_path, Two).check_templates()


def test_case_equivalence_is_checked_using_selected_filesystem_rules(tmp_path):
    rows = [{"name": "A", "row_index": "0"}, {"name": "a", "row_index": "1"}]
    class Names(IOModel):
        mask: Path = Template("{name}.tif")
    with pytest.raises(OutputPathCollisionError):
        evidence(tmp_path, Names, rows, case_sensitive=False).check_templates()
    assert len(evidence(tmp_path, Names, rows, case_sensitive=True).check_templates().output_files["mask"]) == 2


def test_reference_paths_are_not_destinations_and_bases_do_not_claim_safety(tmp_path):
    class Mixed(IOModel):
        existing: Path
        tiles: Path = PathBase("{image.stem}_tile.tif")
    assert get_output_templates(Mixed, IOModel) == {"tiles": "{image.stem}_tile.tif"}
    with pytest.raises(ValueError, match="returned reference"):
        get_output_templates(Mixed, IOModel, {"existing": "new.tif"})
    plan = evidence(tmp_path, Mixed).check_templates()
    assert plan.output_files == {}
    assert len({row["tiles"] for row in plan.row_paths}) == 1


def test_unsafe_or_unresolved_paths_are_not_file_owners(tmp_path):
    for pattern in ("../escape.tif", "{missing}.tif"):
        with pytest.raises((ValueError, CacheCorruptionError)):
            evidence(tmp_path).check_templates({"mask": pattern})


def test_empty_mapped_batch_has_no_once_owners_but_collective_batch_does(tmp_path):
    class Two(IOModel):
        first: Path = Template('same.tif',group=FileGroup.once())
        second: Path = Template('same.tif',group=FileGroup.once())
    kwargs=dict(node_id='empty',assets_dir=tmp_path,row_contexts=[],row_indexes=[])
    mapped=OutputPlanningEvidence.capture(tool=SimpleNamespace(Outputs=Two,row_consumption=RowConsumption.MAPPED),**kwargs)
    assert mapped.check_templates().output_files=={'first':(),'second':()}
    collective=OutputPlanningEvidence.capture(tool=SimpleNamespace(Outputs=Two,row_consumption=RowConsumption.COLLECTIVE),**kwargs)
    with pytest.raises(OutputPathCollisionError):
        collective.check_templates()


def test_collective_constant_naming_base_remains_in_batch_arguments(tmp_path):
    class Base(IOModel):
        tiles: Path = PathBase('{image.stem}_tiles.tif')
    plan=evidence(tmp_path,Base,rows=[],batch={'image':Path('/source/cells.tif')}).check_templates()
    assert plan.batch_paths=={'tiles':str(tmp_path/'cells_tiles.tif')}
    assert plan.output_files=={}


def test_keyed_fields_take_only_their_eligible_reference_rows(tmp_path):
    class Mixed(IOModel):
        image_file: Path=Template('{image.stem}.tif',group=FileGroup.by('image'))
        calibration_file: Path=Template('calibration_{calibration}.txt',group=FileGroup.by('calibration'))
    plan=evidence(tmp_path,Mixed,rows=[],references=[{'image':Path('/data/cells.tif')},{'calibration':'A'}]).check_templates()
    assert plan.output_files['image_file'][0].reference_positions==(0,)
    assert plan.output_files['calibration_file'][0].reference_positions==(1,)
    class Composite(IOModel):
        file: Path=Template('group.tif',group=FileGroup.by('image','channel'))
    with pytest.raises(ValueError,match='missing keys'):
        evidence(tmp_path,Composite,rows=[],references=[{'image':Path('/data/cells.tif')}]).check_templates()


def test_unicode_equivalent_names_collide_without_rewriting_explanations(tmp_path):
    class Named(IOModel):
        file: Path=Template('{name}.tif')
    rows=[{'name':'caf\u00e9','row_index':'0'},{'name':'cafe\u0301','row_index':'1'}]
    with pytest.raises(OutputPathCollisionError) as error:
        evidence(tmp_path,Named,rows).check_templates()
    assert error.value.report['conflicts'][0]['path']=='caf\u00e9.tif'


def test_batch_ownership_travels_once_and_row_chunk_projection_is_linear(tmp_path):
    from bioimageflow.engine.arguments import _ArgumentsMixin
    from bioimageflow_core import ExecutionContext, PlannedFile
    from bioimageflow.output_planning import OutputFilePlan
    from tests.support.output_file_processing import AggregateMask, IndependentMasks
    count=1000
    contexts=[ExecutionContext(tmp_path,tmp_path/'assets',tmp_path/'work',tmp_path/'work/rows',
                row_dir=tmp_path/'work/rows'/str(i),row_index=str(i)) for i in range(count)]
    shared=PlannedFile(tmp_path/'assets/mosaic.tif',0,(),tuple(range(count)),())
    plan=OutputFilePlan({'mask':(shared,)},tuple({} for _ in contexts),(),{})
    projected=_ArgumentsMixin()._output_file_row_contexts(SimpleNamespace(tool=AggregateMask()),contexts,plan)
    assert projected is contexts
    assert all(context.output_files=={} for context in projected)
    class CountedFiles(list):
        visits=0
        def __iter__(self):
            for item in super().__iter__():
                self.visits+=1
                yield item
    files=CountedFiles(PlannedFile(tmp_path/'assets'/f'{i}.tif',i,(),(i,),()) for i in range(count))
    plan=OutputFilePlan({'mask':files},tuple({} for _ in contexts),(),{})
    projected=_ArgumentsMixin()._output_file_row_contexts(SimpleNamespace(tool=IndependentMasks()),contexts,plan)
    assert files.visits==count
    assert all(context.output_files['mask'][0].row_positions==(i,) for i,context in enumerate(projected))


def test_constant_inspection_warns_only_with_complete_non_timestamp_inputs(tmp_path):
    from bioimageflow import inspect_constant_output_files
    from tests.support.output_file_processing import SameNameMapped
    class Two(IOModel):
        first: Path=Template('same.tif')
        second: Path=Template('same.tif')
    tool=SimpleNamespace(Inputs=IOModel,Outputs=Two,row_consumption=RowConsumption.MAPPED)
    reports=inspect_constant_output_files(tool,{},node_id='constant',assets_dir=tmp_path)
    assert reports[0]['code']=='output_path_collision'
    assert inspect_constant_output_files(tool,{},node_id='constant',templates={'second':'other.tif'},assets_dir=tmp_path)==[]
    assert inspect_constant_output_files(tool,{},node_id='constant',templates={'first':'{timestamp}.tif'},assets_dir=tmp_path)==[]
    assert inspect_constant_output_files(SameNameMapped(),{},node_id='unknown',assets_dir=tmp_path)==[]
    assert not list(tmp_path.iterdir())


def test_repeated_display_indices_use_verified_group_index_fallback(tmp_path):
    rows=[{'image':Path('/source/image.tif'),'row_index':'same','_ext':'.tif'} for _ in range(2)]
    with pytest.raises(OutputPathCollisionError) as error:
        evidence(tmp_path,Outputs,rows).check_templates()
    assert '{group_index}' in error.value.report['suggested_templates']['mask']


def test_context_file_projection_is_immutable_detached_and_picklable(tmp_path):
    import pickle
    from bioimageflow_core import ExecutionContext,PlannedFile
    file=PlannedFile(tmp_path/'assets/file.tif',0)
    original={'mask':(file,)}
    context=ExecutionContext(tmp_path,tmp_path/'assets',tmp_path/'work',tmp_path/'work/rows',
                batch_dir=tmp_path/'work/batch',output_files=original)
    original.clear()
    assert context.output_files['mask']==(file,)
    with pytest.raises(TypeError):
        context.output_files['mask']=()
    assert pickle.loads(pickle.dumps(context))==context


@pytest.mark.parametrize('pattern,expected',[
    ('{image.stem}.ome.tif','image_0.ome.tif'),
    ('{image.stem}.ome{ext}','image_0.ome.tif'),
    ('{image.stem}{image.ext}','image_0.tif'),
    ('{image.stem}{image.exts}','image_0.tif'),
])
def test_verified_suffix_suggestions_preserve_declared_extensions(tmp_path,pattern,expected):
    with pytest.raises(OutputPathCollisionError) as error:
        evidence(tmp_path).check_templates({'mask':pattern})
    plan=error.value.evidence.check_templates(error.value.report['suggested_templates'])
    assert plan.output_files['mask'][0].path.name==expected


@pytest.mark.parametrize('names,case_sensitive',[(('A','a'),False),(('caf\u00e9','cafe\u0301'),True)])
def test_equivalent_member_spellings_receive_one_exact_owner_path(tmp_path,names,case_sensitive):
    class Shared(IOModel):
        file: Path=Template('{label}.txt',group=FileGroup.once())
    rows=[{'label':name,'row_index':str(i)} for i,name in enumerate(names)]
    plan=evidence(tmp_path,Shared,rows,case_sensitive=case_sensitive).check_templates()
    owner,=plan.output_files['file']
    assert [row['file'] for row in plan.row_paths]==[str(owner.path)]*2
