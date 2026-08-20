from __future__ import annotations

import pandas as pd
import pytest

from bioimageflow_core.arguments import Arguments
from bioimageflow.validation import serialize_input_schema, serialize_output_schema
from bioimageflow_common_tools import (
    FilterTableRows,
    SelectColumns,
    TableFromCsv,
    WriteTable,
)

pytestmark = pytest.mark.package_tools


def test_table_tools_have_serializable_schemas():
    for tool_cls in [TableFromCsv, WriteTable, FilterTableRows, SelectColumns]:
        assert serialize_input_schema(tool_cls)

    assert serialize_output_schema(WriteTable) == {"_passthrough": True}
    assert serialize_output_schema(FilterTableRows) == {"_passthrough": True}
    assert serialize_output_schema(TableFromCsv) == {}
    assert serialize_output_schema(SelectColumns) == {}


def test_select_columns_validates_when_bound_in_workflow(tmp_path):
    from bioimageflow import Workflow

    workflow = Workflow(engine="direct", storage_path=tmp_path)
    with workflow:
        SelectColumns()(columns="sample,score", name="select_columns")

    assert workflow.validate() == []


def test_select_columns_resolves_schema_from_upstream_columns():
    upstream = {
        "sample": {"type": "str", "default": None, "image_spec": None},
        "score": {"type": "int", "default": None, "image_spec": None},
    }

    schema = SelectColumns.resolve_merge_schema(
        [upstream],
        {"columns": "score,sample", "rename_mapping": "score:value"},
    )

    assert schema is not None
    assert list(schema) == ["value", "sample"]
    assert schema["value"]["type"] == "int"


def test_table_from_csv_loads_csv_and_tsv(tmp_path):
    csv_path = tmp_path / "metadata.csv"
    tsv_path = tmp_path / "metadata.tsv"
    csv_path.write_text("sample,score\nA,1\nB,2\n", encoding="utf-8")
    tsv_path.write_text("sample\tscore\nA\t1\nB\t2\n", encoding="utf-8")

    tool = TableFromCsv()

    csv_result = tool.transform(None, Arguments(path=csv_path))
    tsv_result = tool.transform(None, Arguments(path=tsv_path))

    expected = pd.DataFrame({"sample": ["A", "B"], "score": [1, 2]})
    pd.testing.assert_frame_equal(csv_result, expected)
    pd.testing.assert_frame_equal(tsv_result, expected)


def test_table_from_csv_rejects_unknown_suffix(tmp_path):
    path = tmp_path / "metadata.txt"
    path.write_text("sample,score\nA,1\n", encoding="utf-8")

    with pytest.raises(ValueError, match="CSV or TSV"):
        TableFromCsv().transform(None, Arguments(path=path))


def test_write_table_persists_csv_and_tsv(tmp_path):
    df = pd.DataFrame({"sample": ["A", "B"], "score": [1, 2]})
    csv_path = tmp_path / "out.csv"
    tsv_path = tmp_path / "out.tsv"
    tool = WriteTable()

    csv_result = tool.transform(df, Arguments(path=csv_path))
    tsv_result = tool.transform(df, Arguments(path=tsv_path))

    pd.testing.assert_frame_equal(csv_result, df)
    pd.testing.assert_frame_equal(tsv_result, df)
    pd.testing.assert_frame_equal(pd.read_csv(csv_path), df)
    pd.testing.assert_frame_equal(pd.read_csv(tsv_path, sep="\t"), df)


def test_write_table_rejects_unknown_suffix(tmp_path):
    df = pd.DataFrame({"sample": ["A"]})

    with pytest.raises(ValueError, match="CSV or TSV"):
        WriteTable().transform(df, Arguments(path=tmp_path / "out.parquet"))


def test_filter_table_rows_supports_deterministic_operators():
    df = pd.DataFrame(
        {
            "sample": ["A", "B", "C"],
            "score": [1.0, 2.5, 3.5],
            "label": ["nuclei", "membrane", "nuclei"],
        }
    )
    tool = FilterTableRows()

    gt = tool.transform(df, Arguments(column="score", operator="gt", value="2"))
    contains = tool.transform(df, Arguments(column="label", operator="contains", value="cle"))
    in_result = tool.transform(df, Arguments(column="sample", operator="in", value="A,C"))

    assert list(gt["sample"]) == ["B", "C"]
    assert list(contains["sample"]) == ["A", "C"]
    assert list(in_result["sample"]) == ["A", "C"]


def test_filter_table_rows_rejects_invalid_operator_without_eval():
    df = pd.DataFrame({"sample": ["A"], "score": [1]})

    with pytest.raises(ValueError, match="Unsupported operator"):
        FilterTableRows().transform(
            df,
            Arguments(
                column="score",
                operator="__import__('os').system('false')",
                value="1",
            ),
        )


def test_filter_table_rows_rejects_missing_column():
    df = pd.DataFrame({"sample": ["A"]})

    with pytest.raises(KeyError, match="score"):
        FilterTableRows().transform(df, Arguments(column="score", operator="eq", value="1"))


def test_select_columns_keeps_order_and_renames():
    df = pd.DataFrame(
        {
            "sample": ["A", "B"],
            "score": [1, 2],
            "condition": ["ctrl", "treated"],
        }
    )

    result = SelectColumns().transform(
        df,
        Arguments(columns="condition,score", rename_mapping="condition:group,score:value"),
    )

    assert list(result.columns) == ["group", "value"]
    assert result.to_dict("list") == {
        "group": ["ctrl", "treated"],
        "value": [1, 2],
    }


def test_select_columns_rejects_missing_column():
    df = pd.DataFrame({"sample": ["A"]})

    with pytest.raises(KeyError, match="score"):
        SelectColumns().transform(df, Arguments(columns="sample,score", rename_mapping=""))


def test_select_columns_rejects_duplicate_selections():
    df = pd.DataFrame({"sample": ["A"]})

    with pytest.raises(ValueError, match="duplicate selection"):
        SelectColumns().transform(
            df,
            Arguments(columns="sample,sample", rename_mapping=""),
        )


@pytest.mark.parametrize(
    "rename_mapping",
    ["sample:value,score:value", "sample:score"],
)
def test_select_columns_rejects_final_name_collisions(rename_mapping):
    df = pd.DataFrame({"sample": ["A"], "score": [1]})

    with pytest.raises(ValueError, match="unique output names"):
        SelectColumns().transform(
            df,
            Arguments(columns="sample,score", rename_mapping=rename_mapping),
        )


def test_select_columns_rejects_duplicate_rename_sources():
    df = pd.DataFrame({"sample": ["A"]})

    with pytest.raises(ValueError, match="duplicate source"):
        SelectColumns().transform(
            df,
            Arguments(columns="sample", rename_mapping="sample:first,sample:second"),
        )


def test_select_columns_rejects_duplicate_upstream_columns():
    df = pd.DataFrame([["A", "B"]], columns=["sample", "sample"])

    with pytest.raises(ValueError, match="Upstream table contains duplicate"):
        SelectColumns().transform(
            df,
            Arguments(columns="sample", rename_mapping=""),
        )
