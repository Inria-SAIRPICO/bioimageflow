"""Output templating engine for ProcessingTool."""

import logging
import re
from pathlib import Path
from typing import Any

from bioimageflow_core.tool import IOModel, Template
from bioimageflow.validation import is_path_type, validate_output_template_defaults

logger = logging.getLogger("bioimageflow")

_SPECIAL_VARS = {'node_name', 'row_index', 'timestamp', 'ext'}
_VALID_PROPERTIES = {'stem', 'name', 'ext', 'exts'}


def get_output_templates(
    outputs_cls: type[IOModel],
    inputs_cls: type[IOModel],
    overrides: dict[str, str] | None = None,
) -> dict[str, str]:
    """Extract templates from explicit Template defaults.

    Path outputs without a Template default use the built-in default template.
    Non-path outputs may not declare Template defaults.
    """
    templates: dict[str, str] = {}
    output_annotations = outputs_cls._get_all_annotations()
    input_annotations = inputs_cls._get_all_annotations()

    validate_output_template_defaults(outputs_cls)

    # Count path-typed input fields for {ext} resolution
    path_input_fields = [
        name for name, ann in input_annotations.items()
        if is_path_type(ann)
    ]

    for name, ann in output_annotations.items():
        if not is_path_type(ann):
            continue
        default = getattr(outputs_cls, name, None)
        if isinstance(default, Template):
            templates[name] = default.pattern
        else:
            # Default template
            if len(path_input_fields) == 1:
                templates[name] = "{node_name}_{row_index}{ext}"
            else:
                templates[name] = "{node_name}_{row_index}"

    if overrides:
        for name, template in overrides.items():
            if name in templates and isinstance(template, str) and template:
                templates[name] = template
    return templates


def validate_template(template_str: str, input_annotations: dict[str, Any]) -> None:
    """Check that all template references are valid.

    - ``{field.xxx}`` — *field* must exist in Inputs, *xxx* must be a valid
      property (stem, name, ext, exts).
    - ``{bare}`` — must be a special variable, a ``column:`` reference, or an
      input field.  Unknown bare references emit a warning.
    """
    # Check {field.property} patterns
    for match in re.finditer(r'\{(\w+)\.(\w+)\}', template_str):
        field_name, prop_name = match.group(1), match.group(2)
        if field_name not in input_annotations and field_name not in _SPECIAL_VARS:
            if not field_name.startswith('column:'):
                raise ValueError(
                    f"Template references undefined input field '{field_name}'. "
                    f"Available input fields: {list(input_annotations.keys())}"
                )
        if prop_name not in _VALID_PROPERTIES:
            logger.warning(
                "Template property '%s' on field '%s' is not recognised. "
                "Valid properties: %s",
                prop_name, field_name, sorted(_VALID_PROPERTIES),
            )

    # Check bare {name} patterns (not {field.prop}, not {column:name})
    for match in re.finditer(r'\{(\w+)\}', template_str):
        bare = match.group(1)
        if bare in _SPECIAL_VARS:
            continue
        if bare in input_annotations:
            continue
        logger.warning(
            "Template references undefined variable '{%s}'. "
            "Known special variables: %s, known input fields: %s",
            bare, sorted(_SPECIAL_VARS), list(input_annotations.keys()),
        )


def resolve_template(template_str: str, context: dict[str, Any]) -> str:
    """
    Resolve a template string using the provided context dict.

    Context should include:
    - node_name, row_index, timestamp
    - Input field values (for .stem, .name, .ext, .exts properties)
    - column:<name> lookups
    """
    result = template_str

    # Resolve {column:<name>} patterns first
    def replace_column(match: re.Match[str]) -> str:
        col_name = match.group(1)
        columns: dict[str, Any] = context.get('_columns', {})
        if col_name in columns:
            return str(columns[col_name])
        return match.group(0)

    result = re.sub(r'\{column:(\w+)\}', replace_column, result)

    # Resolve {field.property} patterns
    def replace_field_prop(match: re.Match[str]) -> str:
        field_name = match.group(1)
        prop = match.group(2)
        value = context.get(field_name)
        if value is None:
            return match.group(0)
        p = Path(str(value))
        if prop == 'stem':
            return p.stem
        elif prop == 'name':
            return p.name
        elif prop == 'ext':
            return p.suffix
        elif prop == 'exts':
            return ''.join(p.suffixes)
        return match.group(0)

    result = re.sub(r'\{(\w+)\.(\w+)\}', replace_field_prop, result)

    # Resolve {ext} — special variable
    if '{ext}' in result:
        ext_value = context.get('_ext', '')
        result = result.replace('{ext}', str(ext_value))

    # Resolve bare variables ({node_name}, {row_index}, scalar input fields, ...).
    def replace_bare(match: re.Match[str]) -> str:
        key = match.group(1)
        if key in context and not key.startswith('_'):
            return str(context[key])
        return match.group(0)

    result = re.sub(r'\{(\w+)\}', replace_bare, result)

    return result
