"""Executor-specific dependency admission without replacing caller modules."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
import importlib.machinery
import importlib.metadata as metadata
import json
from pathlib import Path
import sys
import threading
from typing import Any, Iterator, Optional
from urllib.parse import unquote, urlparse

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

_lock = threading.RLock()
_POLICIES = {"selected_installation", "managed_runtime"}
_host: Optional[dict[str, Any]] = None
_admissions: dict[tuple[Path, str, str], ImportRootAdmission] = {}
_dependency_claims: dict[str, dict[tuple[Path, str], tuple[str, Any]]] = {}


def _host_inventory() -> dict[str, Any]:
    """Snapshot application distribution/import ownership once per process.

    Selected tool roots have their own invalidation boundary. Updating the host
    interpreter or its dependencies requires restarting the process.
    """
    global _host
    with _lock:
        if _host is None:
            distributions = tuple(metadata.distributions())
            imports: dict[str, list[Any]] = {}
            providers: dict[str, list[Any]] = {}
            names: dict[int, set[str]] = {}
            facts: dict[int, tuple[str, str]] = {}
            for distribution in distributions:
                facts[id(distribution)] = (canonicalize_name(distribution.metadata["Name"] or ""), distribution.version)
                providers.setdefault(facts[id(distribution)][0], []).append(distribution)
                names[id(distribution)] = _import_names(distribution)
                for name in names[id(distribution)]:
                    imports.setdefault(name, []).append(distribution)
            _host = dict(distributions=distributions, imports=imports, providers=providers,
                         import_names=names, facts=facts, members={}, layouts={}, sources={})
        return _host


def _distribution_members(distribution: Any) -> dict[Path, str]:
    """Parse each host distribution's RECORD member locations at most once."""
    inventory = _host_inventory()
    with _lock:
        identity = id(distribution)
        if identity in inventory["members"]:
            return inventory["members"][identity]
        members = {
            Path(str(distribution.locate_file(member))).resolve(): str(member).replace("\\", "/")
            for member in distribution.files or ()}
        if identity in inventory["import_names"]:
            inventory["members"][identity] = members
        return members


def invalidate_import_root(root: Any, *, import_package: str) -> None:
    """Retire selected-root facts, keeping the application inventory fixed."""
    path = Path(root).resolve()
    with _lock:
        for key in tuple(_admissions):
            if key[:2] == (path, import_package):
                del _admissions[key]
        for name, claims in tuple(_dependency_claims.items()):
            claims.pop((path, import_package), None)
            if not claims:
                del _dependency_claims[name]


def _import_names(distribution: Any) -> set[str]:
    names = {line.strip() for line in (distribution.read_text("top_level.txt") or "").splitlines()
             if line.strip().isidentifier()}
    for member in distribution.files or ():
        first = member.parts[0] if member.parts else ""
        name = first.removesuffix(".py")
        if name.isidentifier() and not first.endswith((".dist-info", ".data")):
            names.add(name)
    direct_text = distribution.read_text("direct_url.json")
    if direct_text:
        try:
            direct = json.loads(direct_text)
            location = urlparse(direct.get("url", ""))
            if location.scheme == "file" and direct.get("dir_info", {}).get("editable", False):
                project = Path(unquote(location.path))
                for source_root in (project, project / "src"):
                    if source_root.is_dir():
                        for child in source_root.iterdir():
                            name = child.stem if child.is_file() else child.name
                            if name.isidentifier() and (child.suffix == ".py" or (child / "__init__.py").is_file()):
                                names.add(name)
        except (TypeError, ValueError, OSError):
            pass
    return names


def _within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _module_paths(module: Any) -> tuple[Path, ...]:
    source = getattr(module, "__file__", None)
    if isinstance(source, str):
        return (Path(source).resolve(),)
    locations = tuple(getattr(module, "__path__", ()))
    return tuple(Path(location).resolve() for location in locations)


def _owns(distribution: Any, module_name: str, module: Any, cache: dict[int, Any],
          layouts: dict[int, tuple[Path, Optional[str]]]) -> bool:
    paths = _module_paths(module)
    if not paths:
        return False
    identity = id(distribution)
    inventory = _host_inventory()
    host_owned = identity in inventory["import_names"]
    selected_layouts = inventory["layouts"] if host_owned else layouts
    if identity not in selected_layouts:
        selected_layouts[identity] = (Path(str(distribution.locate_file(""))).resolve(),
                                      distribution.read_text("direct_url.json"))
    installed_root, direct_text = selected_layouts[identity]
    if not all(_within(path, installed_root) for path in paths) and not direct_text:
        return False
    if identity not in cache:
        cache[identity] = (_distribution_members(distribution) if host_owned else
                           {Path(str(distribution.locate_file(member))).resolve()
                            for member in distribution.files or ()})
    members = cache[identity]
    if all(path in members for path in paths):
        return True
    if getattr(module, "__file__", None) is None and all(
        any(_within(member, path) for member in members) for path in paths
    ):
        return True
    text = direct_text
    if not text:
        return False
    try:
        direct = json.loads(text)
        location = urlparse(direct.get("url", ""))
    except (TypeError, ValueError):
        return False
    if location.scheme != "file" or not direct.get("dir_info", {}).get("editable", False):
        return False
    project = Path(unquote(location.path)).resolve()
    parts = module_name.split(".")
    # Hatch editable installs may omit top_level.txt. Exact direct_url project
    # or src module layout proves membership; arbitrary prefixes do not suffice.
    targets = tuple(root.joinpath(*parts) for root in (project, project / "src"))
    return all(any(path == target.with_suffix(".py") or path == target / "__init__.py"
                   or (getattr(module, "__file__", None) is None and path == target)
                   for target in targets) for path in paths)


@dataclass(frozen=True)
class ImportRootAdmission:
    """Declared selection facts, distinct from executed dependency-byte proof."""

    root: Path
    import_package: str
    dependency_authority: str
    _selected: tuple[Any, ...] = field(repr=False, compare=False)
    _requirements: tuple[Requirement, ...] = field(repr=False, compare=False)
    _versions: tuple[tuple[str, str], ...]
    _snapshot: dict[str, Any] = field(default_factory=dict, repr=False, compare=False)

    def to_scientific_facts(self) -> dict[str, Any]:
        """Return detached declared versions; no path, role, or byte-equality stamp."""
        return {"dependency_versions": dict(self._versions)}

    @property
    def observed_dependencies(self) -> tuple[dict[str, str], ...]:
        """Report detached admitted dependency choices, without pixel/code proof."""
        return tuple(dict(item) for item in self._snapshot["observed"])


def _requirements(distributions: tuple[Any, ...]) -> tuple[Requirement, ...]:
    result = []
    for distribution in distributions:
        for value in distribution.requires or ():
            requirement = Requirement(value)
            if requirement.marker is None or requirement.marker.evaluate({"extra": ""}):
                result.append(requirement)
    return tuple(result)


def admit_import_root(
    root: Any, *, import_package: str,
    dependency_authority: str = "selected_installation",
) -> ImportRootAdmission:
    """Admit active imports without importing tool code or changing search paths."""
    if dependency_authority not in _POLICIES:
        raise ValueError(f"Unknown dependency authority {dependency_authority!r}")
    if not import_package.isidentifier():
        raise ValueError("Selected tool import package must be a top-level identifier")
    path = Path(root).resolve(strict=True)
    if not path.is_dir():
        raise ValueError(f"Selected import root is not a directory: {path}")
    key = (path, import_package, dependency_authority)
    with _lock:
        cached = _admissions.get(key)
        if cached is not None:
            return cached
        return _admit_root(path, import_package, dependency_authority)


def _admit_root(path: Path, import_package: str, dependency_authority: str) -> ImportRootAdmission:
    selected = tuple(metadata.distributions(path=[str(path)]))
    primary = tuple(distribution for distribution in selected
                    if import_package in _import_names(distribution))
    if len(primary) != 1:
        raise ImportError(f"Selected tool {import_package!r} requires exactly one providing distribution in {path}")
    versions = tuple(sorted((canonicalize_name(distribution.metadata["Name"]), distribution.version)
                            for distribution in selected if distribution not in primary))
    requirement_sources = selected
    if dependency_authority == "managed_runtime":
        requirement_sources = primary + (metadata.distribution("bioimageflow-core"),)
    admission = ImportRootAdmission(path, import_package, dependency_authority,
                                    selected, _requirements(requirement_sources), versions)
    observed = _validate(admission)
    _claim_dependencies(admission)
    admission._snapshot["observed"] = observed
    _admissions[(path, import_package, dependency_authority)] = admission
    return admission


def _validate(admission: ImportRootAdmission) -> tuple[dict[str, str], ...]:
    snapshot = admission._snapshot
    if not snapshot:
        selected_by_import: dict[str, list[Any]] = {}
        inventory = _host_inventory()
        providers = {name: list(items) for name, items in inventory["providers"].items()}
        distribution_facts: dict[int, tuple[str, str]] = dict(inventory["facts"])
        versions_by_name: dict[str, set[str]] = {}
        import_names: dict[int, set[str]] = dict(inventory["import_names"])
        for distribution in admission._selected:
            fact = (canonicalize_name(distribution.metadata["Name"] or ""), distribution.version)
            distribution_facts[id(distribution)] = fact
            providers.setdefault(fact[0], []).append(distribution)
        for claims in _dependency_claims.values():
            for _, distribution in claims.values():
                identity = id(distribution)
                if identity not in distribution_facts:
                    fact = (canonicalize_name(distribution.metadata["Name"] or ""), distribution.version)
                    distribution_facts[identity] = fact
                    providers.setdefault(fact[0], []).append(distribution)
        for distribution in admission._selected:
            names = _import_names(distribution)
            import_names[id(distribution)] = names
            normalized, version = distribution_facts[id(distribution)]
            versions_by_name.setdefault(normalized, set()).add(version)
            for name in names:
                if name != admission.import_package:
                    selected_by_import.setdefault(name, []).append(distribution)
        # Host-only requirements still require actual membership and compatible
        # versions, even when no dependency was copied into the selected tree.
        required_names = {canonicalize_name(requirement.name) for requirement in admission._requirements}
        for normalized in required_names:
            for distribution in providers.get(normalized, ()):
                identity = id(distribution)
                if identity not in import_names:
                    import_names[identity] = _import_names(distribution)
                for name in import_names[identity]:
                    if name != admission.import_package and name not in selected_by_import:
                        selected_by_import[name] = [distribution]
        snapshot.update(selected_by_import=selected_by_import, providers=providers,
                        distribution_facts=distribution_facts, versions_by_name=versions_by_name,
                        ownership={}, layouts={}, discovered={}, requirements={})
    selected_by_import = snapshot["selected_by_import"]
    providers = snapshot["providers"]
    distribution_facts = snapshot["distribution_facts"]
    # Resident membership is checked only at first admission. The resulting
    # dependency choice stays fixed until controlled root retirement/restart.
    ownership = snapshot["ownership"]
    layouts = snapshot["layouts"]
    discovered = snapshot["discovered"]
    observed: dict[tuple[str, str], dict[str, str]] = {}
    active_distributions: dict[int, Any] = {}
    for name, module in tuple(sys.modules.items()):
        candidates = selected_by_import.get(name.split(".", 1)[0], ())
        if not candidates:
            continue
        actual = []
        module_providers = providers
        # A previously admitted dependency can outlive its temporary sys.path
        # entry. Discover metadata at its normal import layout, then require
        # exact RECORD/editable membership rather than trusting the path guess.
        for source in _module_paths(module):
            depth = len(name.split(".")) if source.name == "__init__.py" else max(len(name.split(".")) - 1, 0)
            if depth < len(source.parents):
                owner_root = source.parents[depth]
                if owner_root not in discovered:
                    discovered[owner_root] = tuple(metadata.distributions(path=[str(owner_root)]))
                    for distribution in discovered[owner_root]:
                        identity = id(distribution)
                        fact = (canonicalize_name(distribution.metadata["Name"] or ""), distribution.version)
                        distribution_facts[identity] = fact
                        module_providers.setdefault(fact[0], []).append(distribution)
        selected_names = {distribution_facts[id(candidate)][0] for candidate in candidates}
        for normalized in selected_names:
            for distribution in module_providers.get(normalized, ()):
                if _owns(distribution, name, module, ownership, layouts):
                    actual.append(distribution)
                    active_distributions[id(distribution)] = distribution
        # Metadata may appear through more than one search path; deduplicate facts.
        facts = {distribution_facts[id(dist)] for dist in actual}
        if not facts:
            raise ImportError(f"Dependency {name!r} has unknown loaded distribution ownership for {admission.root}")
        for normalized, version in facts:
            selected_versions = snapshot["versions_by_name"].get(normalized, set())
            if admission.dependency_authority == "selected_installation" and selected_versions and version not in selected_versions:
                raise ImportError(f"Dependency {name!r} conflicts with selected installation: loaded {normalized}=={version}, selected {sorted(selected_versions)}")
            observed[(normalized, version)] = {
                "distribution": normalized, "version": version,
                "authority": admission.dependency_authority,
            }
    requirements = admission._requirements
    if admission.dependency_authority == "managed_runtime":
        for identity, distribution in active_distributions.items():
            if identity not in snapshot["requirements"]:
                snapshot["requirements"][identity] = _requirements((distribution,))
            requirements += snapshot["requirements"][identity]
    # Check known dependency choices once, including selected lazy imports.
    # An absent, unimported distribution is not controller admission authority:
    # ProcessingTool runtime requirements can be supplied only by its worker.
    for requirement in requirements:
        normalized = canonicalize_name(requirement.name)
        if any(name == normalized for name, _ in observed):
            continue
        candidates = providers.get(normalized, ())
        selected = [item for item in candidates if item in admission._selected]
        claims = _dependency_claims.get(normalized, {})
        chosen = selected if admission.dependency_authority == "selected_installation" and selected else [item[1] for item in claims.values()] or list(candidates)
        if not chosen:
            continue
        version = chosen[0].version
        observed[(normalized, version)] = {"distribution": normalized, "version": version,
                                           "authority": admission.dependency_authority}
    for normalized, version in observed:
        for requirement in requirements:
            if canonicalize_name(requirement.name) == normalized and version not in requirement.specifier:
                raise ImportError(f"Dependency {normalized!r} loaded version {version} does not satisfy {requirement}")
    # Roots actually installed without metadata must not silently bypass ownership.
    for top in {name.split(".", 1)[0] for name in sys.modules}:
        if top == admission.import_package or top in selected_by_import:
            continue
        if importlib.machinery.PathFinder.find_spec(top, [str(admission.root)]) is not None:
            raise ImportError(f"Dependency {top!r} has no declared selected distribution owner in {admission.root}")
    return tuple(observed[key] for key in sorted(observed))


def _claim_dependencies(admission: ImportRootAdmission) -> None:
    if admission.dependency_authority != "selected_installation":
        return
    selected = admission._snapshot["selected_by_import"]
    choices = {canonicalize_name(item.metadata["Name"] or ""): item
               for items in selected.values() for item in items}
    for name, distribution in choices.items():
        for version, _ in _dependency_claims.get(name, {}).values():
            if version != distribution.version:
                raise ImportError(f"Dependency {name!r} conflicts with selected installation: admitted {version}, selected {distribution.version}")
    roots = []
    host = _host_inventory()["import_names"]
    for name, distribution in choices.items():
        claims = _dependency_claims.setdefault(name, {})
        if claims:
            distribution = next(iter(claims.values()))[1]
        claims[(admission.root, admission.import_package)] = (distribution.version, distribution)
        if id(distribution) not in host:
            root = str(Path(str(distribution.locate_file(""))).resolve())
            if root not in roots:
                roots.append(root)
    admission._snapshot["dependency_roots"] = tuple(roots)


@contextmanager
def selected_import_root(admission: ImportRootAdmission) -> Iterator[ImportRootAdmission]:
    """Serialize owned contexts, restore caller paths, and preserve primary errors.

    Managed dependencies resolve in their actual runtime; only the synthetic
    tool package's __path__ points into the controller store. External Python
    threads and arbitrary module/global mutation are not isolated by this lock.
    """
    with _lock:
        before = list(sys.path)
        if admission.dependency_authority == "selected_installation":
            roots: list[str] = list(admission._snapshot.get("dependency_roots", ()))
            if str(admission.root) not in roots:
                roots.append(str(admission.root))
            sys.path[:0] = roots
        try:
            yield admission
        except BaseException:
            raise
        finally:
            sys.path[:] = before
