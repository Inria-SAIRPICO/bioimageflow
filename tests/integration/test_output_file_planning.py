"""Runtime output guards precede every affected callback and preserve valid reuse."""
from pathlib import Path
import pytest
from bioimageflow import Workflow, OutputPathCollisionError, OutputGroupDestinationMismatchError
from tests.support.output_file_tools import ImageRows
from tests.support.output_file_processing import IndependentMasks, AggregateMask, GroupedMasks


def test_mapped_duplicate_paths_refuse_before_science_and_corrected_run_completes(tmp_path):
    storage=tmp_path/'results'
    with Workflow(engine='direct',storage_path=storage) as workflow:
        source=ImageRows()(paths=['/data/image.tif']*6,name='inputs')
        masks=IndependentMasks()(image=source['image'],threshold=source['threshold'],name='atlas')
        with pytest.raises(OutputPathCollisionError) as error:
            workflow.compute(masks)
        assert not list(storage.rglob('science-called.txt'))
        assert not list(storage.rglob('image_mask.tif'))
        masks.output_templates.update(error.value.report['suggested_templates'])
        frame=workflow.compute(masks)
    assert len(set(frame['mask'])) == 6
    assert [Path(path).read_text() for path in frame['mask']] == [str(i) for i in range(6)]


def test_collective_once_shares_one_file_without_false_collision(tmp_path):
    with Workflow(engine='direct',storage_path=tmp_path/'results') as workflow:
        source=ImageRows()(paths=['/data/image.tif']*6,name='inputs')
        mosaic=AggregateMask()(image=source['image'],name='mosaic')
        result=workflow.compute(mosaic)
    assert len(result)==1
    assert Path(result.iloc[0]['mask']).read_text()=='6'


def test_keyed_groups_share_within_source_and_distinguish_different_sources(tmp_path):
    with Workflow(engine='direct',storage_path=tmp_path/'results') as workflow:
        source=ImageRows()(paths=['/a/image.tif','/a/image.tif','/b/image.tif'],name='inputs')
        masks=GroupedMasks()(image=source['image'],threshold=source['threshold'],name='tracks')
        with pytest.raises(OutputPathCollisionError) as error:
            workflow.compute(masks)
        masks.output_templates.update(error.value.report['suggested_templates'])
        result=workflow.compute(masks)
    assert len(result)==2
    assert sorted(result['count'])==[1,2]
    assert len(set(result['mask']))==2


def test_keyed_row_suffix_is_a_group_disagreement(tmp_path):
    with Workflow(engine='direct',storage_path=tmp_path/'results') as workflow:
        source=ImageRows()(paths=['/a/image.tif']*2,name='inputs')
        masks=GroupedMasks()(image=source['image'],threshold=source['threshold'],name='tracks',output_templates={'mask':'mask_{row_index}.tif'})
        with pytest.raises(OutputGroupDestinationMismatchError):
            workflow.compute(masks)


def test_collective_exact_file_survives_zero_returned_records(tmp_path):
    from bioimageflow import WorkflowExecutionContext
    from bioimageflow.storage import Storage
    from tests.support.output_file_processing import SilentAggregate
    context = WorkflowExecutionContext()
    storage = tmp_path / 'results'
    with Workflow(engine='direct', storage_path=storage) as workflow:
        source = tmp_path / 'input.tif'
        source.write_text('unused source')
        node = SilentAggregate()(image=source, name='silent')
        frame = workflow.compute(node, run_context=context)
    assert frame.empty
    outcome = next(outcome for outcome in context.execution_outcomes if outcome.node_key == 'silent')
    repository = Storage(storage)
    manifest = repository.load_record_manifest(outcome.result_key, outcome.record_id)
    artifact, = [item for item in manifest.outputs if item['kind'] == 'owned_asset']
    assert artifact['output_column'] == 'mask'
    assert repository.resolve_record_asset(outcome.result_key, outcome.record_id, artifact['path']).read_text() == 'artifact without table rows'


def test_same_name_input_and_exact_output_expand_original_input_once(tmp_path):
    from tests.support.output_file_processing import SameNameMapped
    source=tmp_path/'sample.tif'
    source.write_text('original')
    with Workflow(engine='direct', storage_path=tmp_path/'results') as workflow:
        node=SameNameMapped()(image=source,name='same')
        frame=workflow.compute(node)
    assert Path(frame.iloc[0]['image']).name=='sample_mask.tif'
    assert source.read_text()=='original'


def test_same_name_keyed_output_owns_original_inputs_and_correction_evidence(tmp_path):
    from tests.support.output_file_processing import SameNameGrouped
    with Workflow(engine='direct',storage_path=tmp_path/'results') as workflow:
        source=ImageRows()(paths=['/a/sample.tif','/a/sample.tif','/b/sample.tif'],name='inputs')
        masks=SameNameGrouped()(image=source['image'],threshold=source['threshold'],name='same')
        with pytest.raises(OutputPathCollisionError) as error:
            workflow.compute(masks)
        assert error.value.report['total_owners']==2
        plan=error.value.evidence.check_templates({'image':'{image.name}_{group_index}.tif'})
        assert [item.key['image'] for item in plan.output_files['image']]==[Path('/a/sample.tif'),Path('/b/sample.tif')]
        assert [item.path.name for item in plan.output_files['image']]==['sample.tif_0.tif','sample.tif_1.tif']
        masks.output_templates.update({'image':'{image.name}_{group_index}.tif'})
        frame=workflow.compute(masks)
    assert sorted(Path(path).read_text() for path in frame['image'])==['/a/sample.tif','/b/sample.tif']


def test_callback_cannot_mutate_context_to_admit_undeclared_output(tmp_path):
    from tests.support.output_file_processing import MutatingPlanMask
    storage=tmp_path/'results'
    with Workflow(engine='direct',storage_path=storage) as workflow:
        source=ImageRows()(paths=['/a/image.tif'],name='inputs')
        node=MutatingPlanMask()(image=source['image'],threshold=source['threshold'],name='mutation')
        with pytest.raises(ValueError,match='undeclared exact destination'):
            workflow.compute(node)
    assert list(storage.rglob('mutation-rejected.txt'))
    assert not list(storage.rglob('undeclared.tif'))
