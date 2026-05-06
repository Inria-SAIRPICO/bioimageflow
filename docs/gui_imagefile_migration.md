# GUI Schema Migration: ImageFile

BioImageFlow now serializes file-based image fields as:

```json
{
  "type": "ImageFile",
  "image_spec": { "...": "..." }
}
```

Previously the same fields used `"type": "ImagePath"`. Update GUI code that
branches on schema `type` to treat `"ImageFile"` as the file-backed image
widget/input kind.

Use this mapping:

| Old schema type | New schema type | Meaning |
| --- | --- | --- |
| `"ImagePath"` | `"ImageFile"` | File path carrying image metadata via `image_spec` |
| `"ImageShared"` | `"ImageShared"` | Shared-memory image value |
| `"Path"` | `"Path"` | Generic filesystem path, not necessarily an image |

Practical update checklist:

1. Replace comparisons such as `field.type === "ImagePath"` with
   `field.type === "ImageFile"`.
2. Keep using `field.image_spec` for semantics, layouts, dtypes, and formats.
3. Do not infer image-ness from `"Path"`; only `"ImageFile"` and
   `"ImageShared"` are image schema types.
4. Keep output pins and input widgets for `"ImageFile"` behaviorally equivalent
   to the previous `"ImagePath"` behavior.

The Python tool annotation that produces `"ImageFile"` is:

```python
Annotated[Path, ImageSpec(...)]
```

with optional `GUIMeta(...)` as an additional `Annotated` metadata entry.
