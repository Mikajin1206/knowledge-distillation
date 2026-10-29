"""Explicit module availability with dependencies; defaults to disabled."""
from dataclasses import dataclass


class FeatureDisabled(RuntimeError):
    pass


@dataclass(frozen=True)
class Feature:
    name: str
    version: str
    dependencies: tuple[str, ...] = ()


class Features:
    def __init__(self, modules: list[Feature], enabled: set[str]):
        self._modules = {module.name: module for module in modules}
        if len(self._modules) != len(modules) or any(not m.name or not m.version for m in modules):
            raise ValueError('Each feature must have a unique name and version')
        if enabled - self._modules.keys():
            raise ValueError('Unknown enabled feature')
        for module in modules:
            if set(module.dependencies) - self._modules.keys():
                raise ValueError('Unknown feature dependency')
        self._enabled = frozenset(enabled)

    def require(self, name: str) -> Feature:
        visiting: set[str] = set()
        def check(current: str) -> None:
            if current not in self._modules or current not in self._enabled:
                raise FeatureDisabled(f'Feature unavailable: {current}')
            if current in visiting:
                raise FeatureDisabled(f'Circular feature dependency: {current}')
            visiting.add(current)
            for dep in self._modules[current].dependencies:
                check(dep)
            visiting.remove(current)
        check(name)
        return self._modules[name]
