"""Portable primary source facts and interpreter-local executable admission.

These facts bind the selected Python members and effective constructor/scientific
methods. They are not a sandbox or a proof of arbitrary initializer/global state.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
import hashlib
import importlib.abc
import importlib.machinery
import importlib.metadata as metadata
import importlib.util
import inspect
import json
import os
from pathlib import Path, PurePosixPath
import re
import sys
import threading
from types import FunctionType, MethodType, ModuleType
from typing import Any, Dict, Iterator, Mapping, Optional, Tuple, Union
from urllib.parse import unquote, urlparse

from bioimageflow_core.executable_identity import validate_source_callables

PRIMARY_SCHEMA = "bioimageflow.primary_content.v1"
_HASH = re.compile(r"^[0-9a-f]{64}$")
_MODULE = re.compile(r"^[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*$")
_ROLES = ("__new__", "__init__", "process_row", "process_batch")
_BUILTINS = {"object.__new__": object.__new__, "object.__init__": object.__init__}
_lock = threading.RLock()


@dataclass(frozen=True)
class PrimaryFileMember:
    module: str
    path: str
    source_hash: str
    kind: str = field(default="file", init=False)


@dataclass(frozen=True)
class PrimaryInstalledMember:
    module: str
    distribution: str
    version: str
    relative_path: str
    source_hash: str
    kind: str = field(default="installed", init=False)


PrimaryMember = Union[PrimaryFileMember, PrimaryInstalledMember]


@dataclass(frozen=True)
class PrimaryCallback:
    role: str
    module: str
    qualname: str
    kind: str = field(default="python", init=False)


@dataclass(frozen=True)
class PrimaryBuiltinCallback:
    role: str
    owner: str
    kind: str = field(default="builtin", init=False)


PrimaryCallbackOwner = Union[PrimaryCallback, PrimaryBuiltinCallback]


@dataclass(frozen=True)
class PrimaryContentProof:
    members: Tuple[PrimaryMember, ...]
    callbacks: Tuple[PrimaryCallbackOwner, ...]
    schema: str = field(default=PRIMARY_SCHEMA, init=False)


def _text(value: Any, label: str) -> str:
    if type(value) is not str or not value or value != value.strip() or any(ord(c) < 32 for c in value):
        raise ValueError("Invalid primary " + label)
    return value


def _module(value: Any) -> str:
    value = _text(value, "module")
    if _MODULE.fullmatch(value) is None:
        raise ValueError("Invalid primary module")
    return value


def encode_primary_content(proof: PrimaryContentProof) -> Dict[str, Any]:
    if not isinstance(proof, PrimaryContentProof):
        raise TypeError("primary must be a PrimaryContentProof")
    return {"schema": proof.schema, "members": [asdict(member) for member in proof.members],
            "callbacks": [asdict(callback) for callback in proof.callbacks]}


def decode_primary_content(value: Any) -> PrimaryContentProof:
    """Decode only structural facts, without files, imports or attachment."""
    if type(value) is not dict or set(value) != {"schema", "members", "callbacks"} or value["schema"] != PRIMARY_SCHEMA:
        raise ValueError("Unsupported or malformed primary content proof")
    if type(value["members"]) is not list or not value["members"] or type(value["callbacks"]) is not list:
        raise ValueError("Primary members and callbacks must be ordered lists")
    members = []
    for item in value["members"]:
        if type(item) is not dict:
            raise ValueError("Primary member must be an object")
        kind = item.get("kind")
        wanted = {"kind", "module", "source_hash", "path"} if kind == "file" else {"kind", "module", "source_hash", "distribution", "version", "relative_path"}
        if kind not in {"file", "installed"} or set(item) != wanted:
            raise ValueError("Malformed primary member")
        module = _module(item["module"])
        digest = item["source_hash"]
        if type(digest) is not str or _HASH.fullmatch(digest) is None:
            raise ValueError("Invalid primary source_hash")
        if kind == "file":
            path = _text(item["path"], "path")
            if not Path(path).is_absolute() or os.path.normpath(path) != path:
                raise ValueError("Primary path must be absolute and normalized")
            members.append(PrimaryFileMember(module, path, digest))
        else:
            distribution = _text(item["distribution"], "distribution")
            if re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", distribution) is None:
                raise ValueError("Primary distribution must use canonical spelling")
            relative = _text(item["relative_path"], "relative_path")
            if "\\" in relative or PurePosixPath(relative).is_absolute() or any(part in {"", ".", ".."} for part in relative.split("/")):
                raise ValueError("Primary installed member must be a contained relative path")
            members.append(PrimaryInstalledMember(module, distribution, _text(item["version"], "version"), relative, digest))
    if len({member.module for member in members}) != len(members):
        raise ValueError("Duplicate primary member modules")
    callbacks = []
    for item in value["callbacks"]:
        if type(item) is not dict or item.get("role") not in _ROLES:
            raise ValueError("Invalid primary callback role")
        if item.get("kind") == "python" and set(item) == {"kind", "role", "module", "qualname"}:
            module = _module(item["module"])
            if module not in {member.module for member in members}:
                raise ValueError("Primary callback owner has no admitted member")
            callbacks.append(PrimaryCallback(item["role"], module, _text(item["qualname"], "qualname")))
        elif item.get("kind") == "builtin" and set(item) == {"kind", "role", "owner"} and item["owner"] in _BUILTINS:
            callbacks.append(PrimaryBuiltinCallback(item["role"], item["owner"]))
        else:
            raise ValueError("Malformed primary callback owner")
    if callbacks and tuple(callback.role for callback in callbacks) != _ROLES:
        raise ValueError("Primary proof must contain each constructor and scientific callback in order")
    return PrimaryContentProof(tuple(members), tuple(callbacks))


def primary_scientific_facts(proof: PrimaryContentProof) -> Dict[str, Any]:
    """Content identity deliberately excludes shared/installed operational paths."""
    payload = encode_primary_content(decode_primary_content(encode_primary_content(proof)))
    for member in payload["members"]:
        member.pop("path", None)
        # Locator kind does not turn identical source bytes into different science.
        member.pop("kind", None)
        member.pop("relative_path", None)
        member.pop("distribution", None)
        member.pop("version", None)
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return {"primary_content": hashlib.sha256(encoded.encode()).hexdigest()}


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _function(value: Any) -> FunctionType:
    if isinstance(value, (staticmethod, classmethod)):
        value = value.__func__
    if isinstance(value, MethodType):
        value = value.__func__
    if not isinstance(value, FunctionType):
        raise ValueError("Primary callback has no supported Python source owner")
    return value


def _path(member: PrimaryMember) -> Path:
    if isinstance(member, PrimaryFileMember):
        return Path(member.path).resolve(strict=True)
    distribution = metadata.distribution(member.distribution)
    if distribution.version != member.version:
        raise ImportError("Primary distribution version changed: " + member.distribution)
    files = {str(item).replace("\\", "/") for item in distribution.files or ()}
    if member.relative_path not in files:
        # PEP 610 is a distinct source owner, not a fabricated RECORD entry.
        path = _editable_member_path(distribution, member.module, member.relative_path)
        if path is None:
            raise ImportError("Primary installed member has no recorded or editable owner: " + member.module)
        return path
    path = Path(str(distribution.locate_file(member.relative_path))).resolve(strict=True)
    root = Path(str(distribution.locate_file(""))).resolve(strict=True)
    try:
        path.relative_to(root)
    except ValueError as error:
        raise ImportError("Primary installed member escapes its distribution") from error
    return path


def _editable_member_path(distribution: Any, module: str, relative: str) -> Optional[Path]:
    """Resolve a declared editable import member without importing its code."""
    direct_url = distribution.read_text("direct_url.json")
    declared = json.loads(direct_url) if direct_url else {}
    if not declared.get("dir_info", {}).get("editable", False):
        return None
    location = urlparse(declared.get("url", ""))
    if location.scheme != "file":
        return None
    expected = module.replace(".", "/")
    if relative not in {expected + ".py", expected + "/__init__.py"}:
        return None
    project = Path(unquote(location.path)).resolve(strict=True)
    top = module.split(".")[0]
    anchor = sys.modules.get(top)
    if anchor is not None:
        # Use the existing RECORD/PEP 610 import ownership authority; the
        # already-imported package fixes its selected layout for unloaded members.
        from bioimageflow_core.import_context import _owns
        if not _owns(distribution, top, anchor, {}, {}):
            return None
        anchor_file = getattr(anchor, "__file__", None)
    else:
        # A worker may not have imported the editable primary package yet.
        # PathFinder resolves its declared root without running an initializer.
        spec = importlib.machinery.PathFinder.find_spec(top, [str(project), str(project / "src")])
        anchor_file = None if spec is None else spec.origin
    if not isinstance(anchor_file, str):
        return None
    anchor_path = Path(anchor_file).resolve(strict=True)
    matches = set()
    for root in (project, project / "src"):
        if anchor_path not in {(root / top / "__init__.py").resolve(), (root / (top + ".py")).resolve()}:
            continue
        candidate = root / relative
        if not candidate.is_file():
            continue
        resolved = candidate.resolve(strict=True)
        try:
            resolved.relative_to(project)
        except ValueError:
            continue
        matches.add(resolved)
    if len(matches) != 1:
        return None
    return matches.pop()


@dataclass
class PrimaryContentAdmission:
    proof: PrimaryContentProof
    sources: Dict[str, bytes]
    paths: Dict[str, Path]
    qualification: Tuple[str, ...] = ()
    owned_modules: Dict[str, ModuleType] = field(default_factory=dict, repr=False)
    package_roots: Dict[str, Path] = field(default_factory=dict, repr=False)

    def own_module(self, module: ModuleType) -> None:
        self.owned_modules[module.__name__] = module

    def source_hash(self, path_or_module: Any) -> str:
        text = str(path_or_module)
        if text in self.sources:
            return hashlib.sha256(self.sources[text]).hexdigest()
        selected = Path(text).resolve(strict=True)
        for module, path in self.paths.items():
            if path == selected:
                return hashlib.sha256(self.sources[module]).hexdigest()
        raise ValueError("Source is outside the admitted primary content: " + text)

    def scientific_facts(self) -> Dict[str, Any]:
        return primary_scientific_facts(self.proof)

    def _compare(self, module: str, callbacks: Mapping[str, Any], *, required: bool = True) -> None:
        unknown = validate_source_callables(self.sources[module], callbacks, canonicalize=_canonical)
        if required and any(item.endswith(":source-declaration") for item in unknown):
            raise ValueError("Resident/source primary declaration cannot be proven: " + module)
        self.qualification = tuple(sorted(set(self.qualification) | set(unknown)))

    def attest(self, tool: Any) -> Tuple[str, ...]:
        """Compare effective owners before construction and after instance reuse."""
        for owner in self.proof.callbacks:
            actual = getattr(tool, owner.role)
            if isinstance(owner, PrimaryBuiltinCallback):
                # object.__init__ becomes a method-wrapper on an instance.
                if not isinstance(tool, type):
                    actual = inspect.getattr_static(type(tool), owner.role)
                if actual is not _BUILTINS[owner.owner]:
                    raise ValueError("Resident/source primary builtin owner mismatch: " + owner.role)
                continue
            function = _function(actual)
            if function.__qualname__ != owner.qualname:
                raise ValueError("Resident/source primary callback owner mismatch: " + owner.role)
            if Path(function.__code__.co_filename).resolve(strict=True) != self.paths[owner.module]:
                raise ValueError("Resident/source primary callback member mismatch: " + owner.role)
            defining = sys.modules.get(function.__module__)
            if defining is None or function.__globals__ is not vars(defining):
                raise ValueError("Resident/source primary callback globals owner mismatch: " + owner.role)
            self._compare(owner.module, {owner.role: function})
        return self.qualification

    def attest_modules(self, aliases: Optional[Mapping[str, str]] = None) -> None:
        aliases = {} if aliases is None else aliases
        for member in self.proof.members:
            name = aliases.get(member.module, member.module)
            if name not in sys.modules:
                continue
            module = sys.modules[name]
            source = getattr(module, "__file__", None)
            if module is None or not isinstance(source, str) or Path(source).resolve(strict=True) != self.paths[member.module]:
                raise ImportError("Resident primary module conflicts with selected source root/member: " + name)
            callbacks = {}
            seen = set()

            def visit(container: Any) -> None:
                if id(container) in seen:
                    return
                seen.add(id(container))
                for label, value in vars(container).items():
                    if isinstance(value, (staticmethod, classmethod)):
                        value = value.__func__
                    if (isinstance(value, FunctionType) and value.__globals__ is vars(module)
                            and Path(value.__code__.co_filename).resolve() == self.paths[member.module]):
                        callbacks[value.__qualname__] = value
                    elif isinstance(value, type) and value.__module__ == name:
                        visit(value)
            visit(module)
            self._compare(member.module, callbacks, required=False)


def admit_primary_content(proof: PrimaryContentProof) -> PrimaryContentAdmission:
    proof = decode_primary_content(encode_primary_content(proof))
    sources = {}
    paths = {}
    for member in proof.members:
        path = _path(member)
        contents = path.read_bytes()
        if hashlib.sha256(contents).hexdigest() != member.source_hash:
            raise ImportError("Primary source hash mismatch: " + member.module)
        sources[member.module], paths[member.module] = contents, path
    return PrimaryContentAdmission(proof, sources, paths)


def require_primary_coverage(admission: PrimaryContentAdmission, *, module: Optional[str] = None,
                             package_root: Any = None, source_path: Any = None,
                             distribution: Optional[str] = None, version: Optional[str] = None) -> None:
    """Bind a selector to its whole finite Python package before any import.

    External callback owners remain explicitly represented members, rather than
    expanding this check to an arbitrary transitive dependency closure.
    """
    members = {member.module: member for member in admission.proof.members}
    for name, path in admission.paths.items():
        parts = name.split(".")
        directory = path.parent.parent if path.name == "__init__.py" else path.parent
        for count in range(len(parts) - 1, 0, -1):
            parent = ".".join(parts[:count])
            initializer = directory / "__init__.py"
            if initializer.is_file() and admission.paths.get(parent) != initializer.resolve(strict=True):
                raise ImportError("Primary package initializer is missing or conflicts with its member: " + parent)
            directory = directory.parent
    if source_path is not None:
        if Path(source_path).resolve(strict=True) not in admission.paths.values():
            raise ImportError("Selected source file has no primary content member.")
        return
    if module is None or module not in members:
        raise ImportError("Selected module has no primary content member: " + str(module))
    top = module.split(".")[0]
    if distribution is not None:
        selected = members[module]
        if not isinstance(selected, PrimaryInstalledMember) or selected.distribution != distribution or selected.version != version:
            raise ImportError("Selected primary member does not belong to its installed selector.")
        relative = module.replace(".", "/")
        if selected.relative_path not in {relative + ".py", relative + "/__init__.py"}:
            raise ImportError("Selected primary installed member has a noncanonical import path.")
        path = admission.paths[module]
        count = len(module.split(".")) - (0 if path.name == "__init__.py" else 1)
        import_root = path.parents[count]
        package_root = import_root / top
        if not package_root.is_dir():
            package_root = package_root.with_suffix(".py")
    root = Path(package_root)
    if not root.exists():
        raise ImportError("Selected primary package is absent from its import root: " + str(root))
    root = root.resolve(strict=True)
    expected = _package_paths(top, root)
    if module not in expected:
        raise ImportError("Selected module is outside its primary package: " + module)
    for name, path in expected.items():
        if name not in members or admission.paths[name] != path:
            raise ImportError("Primary package member is missing or conflicts with its selector: " + name)
        if distribution is not None:
            member = members[name]
            relative = name.replace(".", "/") + ("/__init__.py" if path.name == "__init__.py" else ".py")
            if (not isinstance(member, PrimaryInstalledMember) or member.distribution != distribution
                    or member.version != version or member.relative_path != relative):
                raise ImportError("Primary package member has a conflicting installed owner: " + name)
    for name, path in admission.paths.items():
        if (name == top or name.startswith(top + ".")) and expected.get(name) != path:
            raise ImportError("Primary package member is outside its selected footprint: " + name)
    admission.package_roots[top] = root


def _package_paths(package: str, root: Path) -> Dict[str, Path]:
    paths = {}
    files = sorted(root.rglob("*.py")) if root.is_dir() else [root]
    for path in files:
        if "__pycache__" in path.parts:
            continue
        if path.is_symlink():
            raise ValueError("Primary source member is a symlink: " + str(path))
        if root.is_dir():
            relative = path.relative_to(root)
            parts = relative.parts[:-1] if path.name == "__init__.py" else (*relative.parts[:-1], path.stem)
            module = ".".join((package, *parts))
        else:
            module = package
        paths[module] = path.resolve(strict=True)
    return paths


def validate_primary_content(tool: Any, proof: PrimaryContentProof) -> PrimaryContentAdmission:
    admission = admit_primary_content(proof)
    admission.attest_modules()
    admission.attest(tool)
    return admission


def relocate_primary_content(proof: PrimaryContentProof, source_root: Any, target_root: Any) -> PrimaryContentProof:
    """Relocate only selected file members; content and external owners stay bound."""
    from dataclasses import replace
    source_root, target_root = Path(source_root).resolve(), Path(target_root).resolve()
    members = []
    for member in proof.members:
        if isinstance(member, PrimaryFileMember):
            try:
                relative = Path(member.path).relative_to(source_root)
            except ValueError:
                pass
            else:
                member = replace(member, path=str(target_root / relative))
        members.append(member)
    return PrimaryContentProof(tuple(members), proof.callbacks)


def capture_primary_package(package: str, package_root: Any) -> PrimaryContentAdmission:
    """Snapshot package Python members before any selected initializer runs."""
    root = Path(package_root).resolve(strict=True)
    members, sources, paths = [], {}, {}
    for module, path in _package_paths(package, root).items():
        contents = path.read_bytes()
        members.append(PrimaryFileMember(module, str(path.resolve(strict=True)), hashlib.sha256(contents).hexdigest()))
        sources[module], paths[module] = contents, path.resolve(strict=True)
    proof = PrimaryContentProof(tuple(members), ())
    return PrimaryContentAdmission(decode_primary_content(encode_primary_content(proof)), sources, paths)


def _distribution_member(module: str, path: Path, digest: str, distribution_name: Optional[str] = None) -> PrimaryMember:
    if distribution_name is not None:
        names = [distribution_name]
    else:
        package_map = getattr(metadata, "packages_distributions", None)
        if package_map is not None:
            names = package_map().get(module.split(".")[0], [])
        else:
            # Python 3.9 predates packages_distributions(). Distribution-owned
            # source admission still uses its actual top-level metadata.
            from bioimageflow_core.import_context import _import_names
            names = [item.metadata["Name"] for item in metadata.distributions()
                     if module.split(".")[0] in _import_names(item)]
    matches = []
    for name in names:
        distribution = metadata.distribution(name)
        canonical = re.sub(r"[-_.]+", "-", distribution.metadata["Name"]).lower()
        for item in distribution.files or ():
            if Path(str(distribution.locate_file(item))).resolve() == path:
                matches.append(PrimaryInstalledMember(module, canonical, distribution.version, str(item).replace("\\", "/"), digest))
                break
        else:
            relative = module.replace(".", "/") + ("/__init__.py" if path.name == "__init__.py" else ".py")
            if _editable_member_path(distribution, module, relative) == path:
                matches.append(PrimaryInstalledMember(module, canonical, distribution.version, relative, digest))
    if len(matches) == 1:
        return matches[0]
    if distribution_name is not None:
        raise ValueError("Selected primary source has no exact distribution member: " + module)
    return PrimaryFileMember(module, str(path), digest)


def capture_primary_content(tool_class: type, *, distribution: Optional[str] = None, package_root: Any = None) -> PrimaryContentAdmission:
    """Read a finite primary snapshot and attest it without executing source."""
    source = Path(getattr(tool_class, "_bif_admitted_source_file", None) or inspect.getsourcefile(tool_class) or inspect.getfile(tool_class)).resolve(strict=True)
    top = tool_class.__module__.split(".")[0]
    root_module = sys.modules.get(top)
    if package_root is None:
        root_file = getattr(root_module, "__file__", None)
        namespace_paths = tuple(getattr(root_module, "__path__", ()))
        root = (Path(root_file).parent if isinstance(root_file, str) and Path(root_file).name == "__init__.py"
                else Path(namespace_paths[0]).resolve(strict=True) if len(namespace_paths) == 1 else source)
    else:
        root = Path(package_root).resolve(strict=True)
    sources = {}
    paths = {}
    members = {}

    def add(module: str, path: Path, explicit_distribution: Optional[str] = None) -> None:
        path = path.resolve(strict=True)
        if module in members:
            if paths[module] != path:
                raise ValueError("Primary module has conflicting source owners: " + module)
            return
        contents = path.read_bytes()
        digest = hashlib.sha256(contents).hexdigest()
        member = (_distribution_member(module, path, digest, explicit_distribution)
                  if explicit_distribution is not None or not (path == root or root.is_dir() and root in path.parents)
                  else PrimaryFileMember(module, str(path), digest))
        members[module], sources[module], paths[module] = member, contents, path

    if root.is_dir():
        for path in sorted(root.rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            if path.is_symlink():
                raise ValueError("Primary source member is a symlink: " + str(path))
            relative = path.relative_to(root)
            parts = relative.parts[:-1] if path.name == "__init__.py" else (*relative.parts[:-1], path.stem)
            add(".".join((top, *parts)), path, distribution)
    else:
        add(tool_class.__module__, source, distribution)
    callbacks = []
    for role in _ROLES:
        actual = getattr(tool_class, role)
        builtin = next((name for name, value in _BUILTINS.items() if actual is value), None)
        if builtin is not None:
            callbacks.append(PrimaryBuiltinCallback(role, builtin))
            continue
        function = _function(actual)
        module = function.__module__
        path = Path(function.__code__.co_filename).resolve(strict=True)
        add(module, path)
        for count in range(1, len(module.split("."))):
            parent = ".".join(module.split(".")[:count])
            loaded = sys.modules.get(parent)
            parent_file = getattr(loaded, "__file__", None)
            if isinstance(parent_file, str) and Path(parent_file).name == "__init__.py":
                add(parent, Path(parent_file))
        callbacks.append(PrimaryCallback(role, module, function.__qualname__))
    proof = PrimaryContentProof(tuple(members[name] for name in sorted(members)), tuple(callbacks))
    admission = PrimaryContentAdmission(decode_primary_content(encode_primary_content(proof)), sources, paths)
    admission.attest_modules()
    admission.attest(tool_class)
    return admission


class _PrimaryLoader(importlib.abc.Loader):
    def __init__(self, admission: PrimaryContentAdmission, member: PrimaryMember):
        self.admission, self.member = admission, member

    def create_module(self, spec: Any) -> None:
        return None

    def exec_module(self, module: ModuleType) -> None:
        name = self.member.module
        self.admission.own_module(module)
        exec(compile(self.admission.sources[name], str(self.admission.paths[name]), "exec", dont_inherit=True), vars(module))


class _PrimaryFinder(importlib.abc.MetaPathFinder):
    def __init__(self, admission: PrimaryContentAdmission, aliases: Mapping[str, str]):
        self.admission = admission
        self.members = {aliases.get(member.module, member.module): member for member in admission.proof.members}

    def find_spec(self, fullname: str, path: Any = None, target: Any = None) -> Any:
        member = self.members.get(fullname)
        if member is None:
            top, _, relative = fullname.partition(".")
            root = self.admission.package_roots.get(top)
            if root is not None:
                candidate = root.joinpath(*relative.split(".")) if relative else root
                if candidate.with_suffix(".py").is_file() or (candidate / "__init__.py").is_file():
                    raise ImportError("Primary package import has no held source member: " + fullname)
            return None
        location = self.admission.paths[member.module]
        return importlib.util.spec_from_file_location(fullname, location,
            loader=_PrimaryLoader(self.admission, member),
            submodule_search_locations=[str(location.parent)] if location.name == "__init__.py" else None)


@contextmanager
def primary_import_context(admission: PrimaryContentAdmission, *, aliases: Optional[Mapping[str, str]] = None) -> Iterator[PrimaryContentAdmission]:
    """Preserve preexisting canonical owners; compile fresh admitted bytes."""
    aliases = {} if aliases is None else aliases
    with _lock:
        admission.attest_modules(aliases)
        before = dict(sys.modules)
        finder = _PrimaryFinder(admission, aliases)
        sys.meta_path.insert(0, finder)
        try:
            yield admission
            admission.attest_modules(aliases)
        except BaseException:
            for name, module in admission.owned_modules.items():
                if name not in before and sys.modules.get(name) is module:
                    sys.modules.pop(name, None)
            raise
        finally:
            if finder in sys.meta_path:
                sys.meta_path.remove(finder)
