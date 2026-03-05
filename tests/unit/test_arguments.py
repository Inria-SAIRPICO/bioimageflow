"""Unit tests for bioimageflow_core.arguments."""

import pytest

from bioimageflow_core.arguments import Arguments, parse_index_lineage, parent_index


class TestArguments:

    def test_attribute_access(self):
        args = Arguments(x=1, name="test")
        assert args.x == 1
        assert args.name == "test"

    def test_typo_suggestion(self):
        args = Arguments(diameter=30.0)
        with pytest.raises(AttributeError, match="Did you mean.*diameter"):
            args.diamter  # typo

    def test_no_close_match_shows_available(self):
        args = Arguments(x=1, y=2)
        with pytest.raises(AttributeError, match="Available fields"):
            args.zzz

    def test_private_attr_no_suggestion(self):
        args = Arguments(x=1)
        with pytest.raises(AttributeError):
            args._internal


class TestParseIndexLineage:

    def test_simple_index(self):
        assert parse_index_lineage("0") == ["0"]

    def test_exploded_index(self):
        assert parse_index_lineage("0::1::2") == ["0", "1", "2"]

    def test_single_explosion(self):
        assert parse_index_lineage("5::3") == ["5", "3"]


class TestParentIndex:

    def test_simple_returns_self(self):
        assert parent_index("0") == "0"

    def test_one_level(self):
        assert parent_index("0::1") == "0"

    def test_two_levels(self):
        assert parent_index("0::1::2") == "0::1"
