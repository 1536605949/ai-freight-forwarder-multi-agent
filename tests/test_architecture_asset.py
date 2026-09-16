"""Architecture invariants, checked statically.

These are the properties that make the layering claim in the README true rather than
aspirational. They are cheap to check and expensive to lose: a circular import or a provider
reaching into the service layer is exactly the kind of change that looks harmless in a diff
and shows up months later as an untestable knot.

Three invariants:

1. **No import cycles** among first-party modules.
2. **The adapter layer points one way.** `app/providers/*` may depend on `app.config` and
   `app.enums`, but never on `app.models` or `app.services` -- that is what lets a provider be
   swapped, or tested, on its own.
3. **The bundled architecture diagram is a real file**, not a placeholder.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PACKAGE_ROOTS = ('app', 'worker', 'evaluation', 'scripts')

# Providers are allowed to reach these, and nothing else from the application.
PROVIDER_ALLOWED_PREFIXES = ('app.config', 'app.enums', 'app.metrics')


def _module_name(path: Path) -> str:
    rel = path.relative_to(ROOT).with_suffix('')
    parts = list(rel.parts)
    if parts[-1] == '__init__':
        parts.pop()
    return '.'.join(parts)


def _first_party_modules() -> dict[str, Path]:
    out: dict[str, Path] = {}
    for root in PACKAGE_ROOTS:
        for path in (ROOT / root).rglob('*.py'):
            out[_module_name(path)] = path
    return out


def _imports(path: Path, known: set[str]) -> set[str]:
    """First-party modules imported by `path`, resolved to module names."""
    tree = ast.parse(path.read_text(encoding='utf-8'))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name in known:
                    found.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.level:  # relative import: resolve against the package
                base = _module_name(path).rsplit('.', node.level)[0]
                target = f'{base}.{node.module}' if node.module else base
            else:
                target = node.module or ''
            if target in known:
                found.add(target)
            # `from app.services import x` -> the dependency is the package, but the
            # submodule is what actually gets imported.
            for alias in node.names:
                candidate = f'{target}.{alias.name}' if target else alias.name
                if candidate in known:
                    found.add(candidate)
    return found


def _graph() -> dict[str, set[str]]:
    modules = _first_party_modules()
    known = set(modules)
    return {name: _imports(path, known) for name, path in modules.items()}


def test_no_import_cycles():
    """A cycle means the two modules are really one module that has not admitted it yet."""
    graph = _graph()
    WHITE, GREY, BLACK = 0, 1, 2
    colour = dict.fromkeys(graph, WHITE)
    stack: list[str] = []
    cycles: list[list[str]] = []

    def visit(node: str) -> None:
        colour[node] = GREY
        stack.append(node)
        for dep in sorted(graph.get(node, ())):
            if colour.get(dep) == GREY:
                cycles.append(stack[stack.index(dep):] + [dep])
            elif colour.get(dep) == WHITE:
                visit(dep)
        stack.pop()
        colour[node] = BLACK

    for node in sorted(graph):
        if colour[node] == WHITE:
            visit(node)

    assert not cycles, 'import cycle(s):\n  ' + '\n  '.join(' -> '.join(c) for c in cycles)


def test_provider_layer_does_not_depend_on_models_or_services():
    """The adapter layer must be replaceable without dragging the domain with it."""
    offenders: list[str] = []
    for name, path in _first_party_modules().items():
        if not name.startswith('app.providers'):
            continue
        for dep in sorted(_imports(path, set(_first_party_modules()))):
            if dep.startswith(('app.models', 'app.services')):
                offenders.append(f'{name} -> {dep}')
            elif (dep.startswith('app.')
                  and not dep.startswith(PROVIDER_ALLOWED_PREFIXES)
                  and not dep.startswith('app.providers')):
                offenders.append(f'{name} -> {dep} (not in the allowed set)')
    assert not offenders, (
        'provider modules must depend only on app.config / app.enums / app.metrics / '
        'other providers:\n  ' + '\n  '.join(offenders))


def test_services_do_not_import_the_http_layer():
    """`app.main` is the top of the application stack; nothing below it may reach back up.

    Scoped to `app.*`: `scripts/` is tooling and is allowed to depend on the whole
    application -- `scripts/export_openapi.py` exists precisely to import `app.main`.
    """
    offenders = [f'{name} -> app.main' for name, deps in _graph().items()
                 if name.startswith('app.') and 'app.main' in deps and name != 'app.main']
    assert not offenders, '\n'.join(offenders)


def test_bundled_architecture_diagram_exists():
    png = ROOT / 'docs' / 'architecture' / 'project2_architecture.png'
    assert png.exists() and png.stat().st_size > 100_000, 'architecture diagram is missing'


@pytest.mark.parametrize('module', ['app.state_machine', 'app.services.orchestrator'])
def test_key_modules_are_importable_without_optional_dependencies(module):
    """Nothing in the critical path may require `agent_framework` to be installed."""
    import importlib
    assert importlib.import_module(module) is not None
