"""Unit tests for bioimageflow.merge tools."""

import pandas as pd
import pytest

from bioimageflow_core.arguments import Arguments
from bioimageflow_common_tools import InnerJoin, CrossJoin, JoinOnColumn, Concat, Collect

pytestmark = pytest.mark.package_tools


def _schema(*columns: str) -> dict[str, dict[str, object]]:
    return {
        column: {"type": "int", "default": None, "image_spec": None}
        for column in columns
    }


def test_merge_tools_declare_dataframe_tags():
    assert InnerJoin.tags == ["dataframe", "merge", "join"]
    assert CrossJoin.tags == ["dataframe", "merge", "cross-join"]
    assert JoinOnColumn.tags == ["dataframe", "merge", "join"]
    assert Concat.tags == ["dataframe", "merge", "concat"]
    assert Collect.tags == ["dataframe", "merge", "collect"]


@pytest.fixture
def df_a():
    return pd.DataFrame({"x": [1, 2, 3]}, index=pd.Index(["0", "1", "2"]))


@pytest.fixture
def df_b():
    return pd.DataFrame({"y": [10, 20, 30]}, index=pd.Index(["0", "1", "2"]))


class TestInnerJoin:

    def test_single_df_returns_copy(self, df_a):
        tool = InnerJoin()
        result = tool.merge_dataframes([df_a], Arguments())
        pd.testing.assert_frame_equal(result, df_a)
        assert result is not df_a

    def test_two_dfs_join_on_index(self, df_a, df_b):
        tool = InnerJoin()
        result = tool.merge_dataframes([df_a, df_b], Arguments())
        assert list(result.columns) == ["x", "y"]
        assert len(result) == 3

    def test_duplicate_columns_deduplicated(self):
        df1 = pd.DataFrame({"col": [1, 2]}, index=pd.Index(["0", "1"]))
        df2 = pd.DataFrame({"col": [3, 4]}, index=pd.Index(["0", "1"]))
        tool = InnerJoin()
        result = tool.merge_dataframes([df1, df2], Arguments())
        assert "col" in result.columns
        assert "col__bif_dup" not in result.columns

    def test_keeps_real_columns_that_look_like_internal_suffixes(self):
        df1 = pd.DataFrame({"col": [1], "col__bif_dup": [2]})
        df2 = pd.DataFrame({"col": [3], "other": [4]})

        result = InnerJoin().merge_dataframes([df1, df2], Arguments())

        assert result.to_dict("records") == [
            {"col": 1, "col__bif_dup": 2, "other": 4}
        ]

    def test_rejects_duplicate_columns_within_one_table(self):
        duplicate = pd.DataFrame([[1, 2]], columns=["value", "value"])

        with pytest.raises(ValueError, match="duplicate column"):
            InnerJoin().merge_dataframes([duplicate], Arguments())

    def test_empty_input(self):
        tool = InnerJoin()
        result = tool.merge_dataframes([], Arguments())
        assert len(result) == 0


class TestCrossJoin:

    def test_cross_product(self):
        df1 = pd.DataFrame({"a": [1, 2]}, index=pd.Index(["0", "1"]))
        df2 = pd.DataFrame({"b": [10, 20]}, index=pd.Index(["0", "1"]))
        tool = CrossJoin()
        result = tool.merge_dataframes([df1, df2], Arguments(suffixes=("_left", "_right")))
        assert len(result) == 4
        assert "a" in result.columns
        assert "b" in result.columns

    def test_runtime_and_schema_use_the_same_suffix_plan(self):
        first = pd.DataFrame({"value": [1], "left_only": [2]})
        second = pd.DataFrame({"value": [3], "right_only": [4]})
        arguments = Arguments(suffixes=("_a", "_b"))

        result = CrossJoin().merge_dataframes([first, second], arguments)
        schema = CrossJoin.resolve_merge_schema(
            [_schema("value", "left_only"), _schema("value", "right_only")],
            {"suffixes": ("_a", "_b")},
        )

        assert list(result.columns) == ["value_a", "left_only", "value_b", "right_only"]
        assert schema is not None
        assert list(schema) == list(result.columns)

    @pytest.mark.parametrize("suffixes", [("_same", "_same"), ("", "")])
    def test_rejects_ambiguous_suffixes(self, suffixes):
        with pytest.raises(ValueError, match="distinct"):
            CrossJoin().merge_dataframes(
                [pd.DataFrame({"a": [1]}), pd.DataFrame({"a": [2]})],
                Arguments(suffixes=suffixes),
            )

    def test_rejects_suffix_collision_with_existing_column(self):
        first = pd.DataFrame({"value": [1], "value_left": [2]})
        second = pd.DataFrame({"value": [3]})

        with pytest.raises(ValueError, match="Planned merge output"):
            CrossJoin().merge_dataframes(
                [first, second],
                Arguments(suffixes=("_left", "_right")),
            )


class TestJoinOnColumn:

    def test_join_on_shared_column(self):
        df1 = pd.DataFrame({"key": ["a", "b"], "val1": [1, 2]})
        df2 = pd.DataFrame({"key": ["a", "b"], "val2": [10, 20]})
        tool = JoinOnColumn()
        result = tool.merge_dataframes(
            [df1, df2],
            Arguments(join_column="key", how="inner", suffixes=("_left", "_right")),
        )
        assert len(result) == 2
        assert "val1" in result.columns
        assert "val2" in result.columns

    def test_rejects_invalid_join_type(self):
        df = pd.DataFrame({"key": ["a"]})

        with pytest.raises(ValueError, match="Join type"):
            JoinOnColumn().merge_dataframes(
                [df, df],
                Arguments(join_column="key", how="sideways", suffixes=("_l", "_r")),
            )

    def test_rejects_missing_join_column_with_source_position(self):
        first = pd.DataFrame({"key": ["a"]})
        second = pd.DataFrame({"other": ["a"]})

        with pytest.raises(KeyError, match="upstream table 2"):
            JoinOnColumn().merge_dataframes(
                [first, second],
                Arguments(join_column="key", how="inner", suffixes=("_l", "_r")),
            )

    def test_multiple_sources_match_resolved_schema(self):
        frames = [
            pd.DataFrame({"key": ["a"], "value": [position]})
            for position in range(3)
        ]
        arguments = Arguments(
            join_column="key", how="inner", suffixes=("_left", "_right")
        )

        result = JoinOnColumn().merge_dataframes(frames, arguments)
        schema = JoinOnColumn.resolve_merge_schema(
            [_schema("key", "value") for _ in frames],
            {
                "join_column": "key",
                "how": "inner",
                "suffixes": ("_left", "_right"),
            },
        )

        assert list(result.columns) == ["key", "value_left", "value_right", "value"]
        assert schema is not None
        assert list(schema) == list(result.columns)


class TestConcat:

    def test_vertical_concat(self):
        df1 = pd.DataFrame({"x": [1, 2]})
        df2 = pd.DataFrame({"x": [3, 4]})
        tool = Concat()
        result = tool.merge_dataframes([df1, df2], Arguments())
        assert len(result) == 4
        assert list(result["x"]) == [1, 2, 3, 4]

    def test_preserves_index(self):
        df1 = pd.DataFrame({"x": [1]}, index=pd.Index(["a"]))
        df2 = pd.DataFrame({"x": [2]}, index=pd.Index(["b"]))
        tool = Concat()
        result = tool.merge_dataframes([df1, df2], Arguments())
        assert list(result.index) == ["a", "b"]


class TestCollect:

    def test_collect_is_passthrough(self):
        tool = Collect()
        # Collect uses default merge (inner join) and default transform (identity)
        df = pd.DataFrame({"a": [1], "b": [2]}, index=pd.Index(["0"]))
        result = tool.transform(df, Arguments())
        pd.testing.assert_frame_equal(result, df)

    def test_numeric_rename_plan_avoids_incoming_names(self):
        first = pd.DataFrame({"value": [1]})
        second = pd.DataFrame({"value": [2], "value_1": [3]})

        result = Collect().merge_dataframes([first, second], Arguments())
        schema = Collect.resolve_merge_schema(
            [_schema("value"), _schema("value", "value_1")]
        )

        assert list(result.columns) == ["value", "value_2", "value_1"]
        assert schema is not None
        assert list(schema) == list(result.columns)
