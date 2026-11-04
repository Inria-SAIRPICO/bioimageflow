"""Malformed exact-output projections never reach a scientific callback."""
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
import pytest

from bioimageflow_core import ExecutionContext, PlannedFile, ReferenceRow, RowInvocation, describe_tool_declaration
from bioimageflow_core.worker import _execute_tool
from tests.support.output_file_processing import IndependentMasks, GroupedMasks


def task(tmp_path, *, grouped=False):
    assets=tmp_path/'assets'
    assets.mkdir()
    work=tmp_path/'work'
    work.mkdir()
    tool=GroupedMasks() if grouped else IndependentMasks()
    destination=assets/'mask.tif'
    key=(('image',Path('/data/image.tif')), ) if grouped else ()
    file=PlannedFile(destination,0,key,(0,),())
    context=ExecutionContext(tmp_path,assets,work,work/'rows',
        row_dir=None if grouped else work/'rows/0',
        batch_dir=work/'batch' if grouped else None,row_index=None if grouped else '0',
        output_files={'mask':(file,)})
    row=RowInvocation(0,'0',{'image':Path('/data/image.tif'),'threshold':0,'mask':str(destination)},
                      None if grouped else context.to_dict())
    invocation=SimpleNamespace(row_consumption=tool.row_consumption.value,
        declaration=describe_tool_declaration(tool),mode='process_batch' if grouped else 'row_chunk',
        rows=(row,),reference_rows=(),batch_context=context.to_dict() if grouped else None,
        batch_arguments={},shared_memory_context=None,task_id='task_'+'0'*16,
        node_name='probe',invocation_id='inv_'+'1'*32,cache_attempt_id=None,task_retry=0)
    return invocation,tool,context


@pytest.mark.parametrize('mutation',['missing_owner','wrong_argument','swapped_member'])
def test_row_projection_requires_complete_matching_ownership(tmp_path,mutation):
    invocation,tool,context=task(tmp_path)
    row=invocation.rows[0]
    file=context.output_files['mask'][0]
    if mutation=='missing_owner':
        context=replace(context,output_files={'mask':()})
    elif mutation=='wrong_argument':
        row=replace(row,arguments=dict(row.arguments,mask=str(tmp_path/'assets/other.tif')))
    else:
        context=replace(context,output_files={'mask':(replace(file,row_positions=(1,)),)})
    invocation.rows=(replace(row,context=context.to_dict()),)
    with pytest.raises(ValueError):
        _execute_tool(invocation,tool,task=None)
    assert not (tmp_path/'work/science-called.txt').exists()
    assert not (tmp_path/'assets/mask.tif').exists()


def test_keyed_projection_rejects_swapped_source_key_before_batch(tmp_path):
    invocation,tool,context=task(tmp_path,grouped=True)
    file=context.output_files['mask'][0]
    bad=replace(file,key_values=(('image',Path('/other/image.tif')),))
    invocation.batch_context=replace(context,output_files={'mask':(bad,)}).to_dict()
    with pytest.raises(ValueError,match='key disagrees'):
        _execute_tool(invocation,tool,task=None)
    assert not (tmp_path/'assets/mask.tif').exists()


def test_keyed_projection_ignores_wholly_unrelated_reference(tmp_path):
    invocation,tool,context=task(tmp_path,grouped=True)
    invocation.reference_rows=(ReferenceRow(0,'calibration',{'calibration':'A'}),)
    result=_execute_tool(invocation,tool,task=None)
    assert result['schema']=='bioimageflow.processing_result.v5'
    assert (tmp_path/'assets/mask.tif').read_text()=='1'
