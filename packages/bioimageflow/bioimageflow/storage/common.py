"""Output/cache storage primitives.

The on-disk cache schema is versioned, but this module is the clean storage
implementation used by the current runtime.
"""

# Focused storage modules import these shared definitions.
# ruff: noqa: F401

from __future__ import annotations

import base64
import errno
import hashlib
import json
import math
import os
import re
import shutil
import unicodedata
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any, Literal, cast

import pandas as pd


CACHE_SCHEMA_VERSION = "bioimageflow.cache.v1"
CURRENT_SCHEMA = "bioimageflow.cache.current.v1"
RECORD_SCHEMA = "bioimageflow.cache.record.v1"
LINK_SCHEMA = "bioimageflow.link.v1"
RUN_SCHEMA = "bioimageflow.run.v1"
RUN_NODE_RESULT_SCHEMA = "bioimageflow.run.node_result.v1"

_RESERVED_NAMES = {
    "con",
    "prn",
    "aux",
    "nul",
    *(f"com{i}" for i in range(1, 10)),
    *(f"lpt{i}" for i in range(1, 10)),
}
_RESULT_KEY_RE = re.compile(r"^rk_[a-z2-7]{52}$")
_RECORD_ID_RE = re.compile(r"^rec_[a-z2-7]{52}$")
_SHA256_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_INTEGER_RE = re.compile(r"^-?[0-9]+$")
_UNSIGNED_INTEGER_RE = re.compile(r"^[0-9]+$")
_RECORD_MANIFEST_FIELDS = frozenset(
    {"schema", "result_key", "record_id", "dataframe", "outputs"}
)
_OUTPUT_VIEW_MODES = frozenset({"none", "pointer", "symlink", "copy", "hardlink"})
