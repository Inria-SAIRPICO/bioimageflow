"""Build real fixture-member proofs without executing their initializers."""

import ast
import hashlib
from pathlib import Path

from bioimageflow_core import ProcessingTool, SourceFileOrigin
from bioimageflow_core.primary_content import (
    PrimaryCallback,
    PrimaryBuiltinCallback,
    PrimaryContentProof,
    PrimaryFileMember,
    capture_primary_content,
)


def literal_proof(module="fixture_tool", class_name="Tool", path="/shared/tool.py"):
    """Strict structural DTO facts for codecs/routes that never load Python."""
    return PrimaryContentProof(
        (PrimaryFileMember(module, path, "a" * 64),),
        (PrimaryBuiltinCallback("__new__", "object.__new__"),
         PrimaryBuiltinCallback("__init__", "object.__init__"),
         PrimaryCallback("process_row", module, class_name + ".process_row"),
         PrimaryCallback("process_batch", module, class_name + ".process_batch")),
    )


def literal_source_origin(path="/shared/tool.py", source_hash="a" * 64, class_name="Tool"):
    return SourceFileOrigin(path, source_hash, class_name, literal_proof(class_name=class_name, path=path))


def source_proof(path, class_name, *, module="fixture_tool", package_root=None):
    path = Path(path).resolve()
    framework = capture_primary_content(ProcessingTool).proof
    members = {member.module: member for member in framework.members}
    if package_root is None:
        files = [(module, path)]
    else:
        package_root = Path(package_root).resolve()
        top = module.split(".")[0]
        files = []
        for item in sorted(package_root.rglob("*.py")):
            if "__pycache__" in item.parts:
                continue
            relative = item.relative_to(package_root)
            suffix = relative.parts[:-1] if item.name == "__init__.py" else (*relative.parts[:-1], item.stem)
            files.append((".".join((top, *suffix)), item))
    for name, item in files:
        members[name] = PrimaryFileMember(name, str(item), hashlib.sha256(item.read_bytes()).hexdigest())
    classes = {node.name: node for node in ast.parse(path.read_bytes()).body if isinstance(node, ast.ClassDef)}

    def owner(name, role):
        node = classes.get(name)
        if node is None:
            return None
        if any(isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and item.name == role for item in node.body):
            return name
        for base in node.bases:
            if isinstance(base, ast.Name):
                inherited = owner(base.id, role)
                if inherited is not None:
                    return inherited
        return None

    callbacks = tuple(
        PrimaryCallback(item.role, module, selected + "." + item.role) if (selected := owner(class_name, item.role)) is not None else item
        for item in framework.callbacks
    )
    return PrimaryContentProof(tuple(members[name] for name in sorted(members)), callbacks)
