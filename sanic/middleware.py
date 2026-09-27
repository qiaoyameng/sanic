from __future__ import annotations

from collections import deque
from collections.abc import Iterable, Sequence
from enum import IntEnum, auto
from heapq import heapify, heappop, heappush
from itertools import count

from sanic.exceptions import MiddlewareFinalizationError
from sanic.models.handler_types import MiddlewareType


class MiddlewareLocation(IntEnum):
    REQUEST = auto()
    RESPONSE = auto()


class Middleware:
    """Middleware object that is used to encapsulate middleware functions.

    This should generally not be instantiated directly, but rather through
    the `sanic.Sanic.middleware` decorator and its variants.

    Args:
        func (MiddlewareType): The middleware function to be called.
        location (MiddlewareLocation): The location of the middleware.
        priority (int): The priority of the middleware.
        name (Optional[str]): An optional name that other middleware can
            reference when declaring dependencies.
        before (Optional[Union[str, Iterable[str]]]): Names of middleware
            that this middleware must run before.
        after (Optional[Union[str, Iterable[str]]]): Names of middleware
            that this middleware must run after.
    """

    _counter = count()
    count: int

    __slots__ = (
        "func",
        "priority",
        "location",
        "definition",
        "name",
        "before",
        "after",
    )

    def __init__(
        self,
        func: MiddlewareType,
        location: MiddlewareLocation,
        priority: int = 0,
        name: str | None = None,
        before: str | Iterable[str] | None = None,
        after: str | Iterable[str] | None = None,
    ) -> None:
        self.func = func
        self.priority = priority
        self.location = location
        self.name = name
        self.before = self._normalize_dependencies(before)
        self.after = self._normalize_dependencies(after)
        self.definition = next(Middleware._counter)

    def __call__(self, *args, **kwargs):
        return self.func(*args, **kwargs)

    def __hash__(self) -> int:
        return hash(self.func)

    def __repr__(self) -> str:
        return (
            f"{self.__class__.__name__}("
            f"func=<function {self.func.__name__}>, "
            f"priority={self.priority}, "
            f"location={self.location.name}, "
            f"name={self.name})"
        )

    @staticmethod
    def _normalize_dependencies(
        dependencies: str | Iterable[str] | None,
    ) -> tuple[str, ...]:
        """Normalize a dependency declaration to a tuple of names.

        Args:
            dependencies (Optional[Union[str, Iterable[str]]]): The declared
                dependency name(s).

        Returns:
            tuple[str, ...]: The dependency names as a tuple.
        """
        if dependencies is None:
            return ()
        if isinstance(dependencies, str):
            return (dependencies,)
        return tuple(dependencies)

    @staticmethod
    def _label(middleware: Middleware) -> str:
        """Return a human readable label for a middleware.

        Args:
            middleware (Middleware): The middleware to label.

        Returns:
            str: The middleware name, falling back to the function name.
        """
        if middleware.name is not None:
            return middleware.name
        return str(
            getattr(
                middleware.func,
                "__name__",
                middleware.func.__class__.__name__,
            )
        )

    @property
    def order(self) -> tuple[int, int]:
        """Return a tuple of the priority and definition order.

        This is used to sort the middleware.

        Returns:
            tuple[int, int]: The priority and definition order.
        """
        return (self.priority, -self.definition)

    @classmethod
    def resolve(
        cls,
        middleware: Iterable[Middleware],
        strict: bool = True,
    ) -> deque[Middleware]:
        """Order middleware honoring declared dependencies.

        Middleware are expected to be provided in their base execution
        order (sorted by priority and definition order). Middleware that
        declare ``before``/``after`` dependencies are repositioned to
        satisfy those constraints. Middleware without dependencies keep
        their relative base order.

        Args:
            middleware (Iterable[Middleware]): The middleware to order.
            strict (bool): Whether to raise an error when a declared
                dependency refers to an unknown middleware name. When
                ``False``, dependencies on unknown names are ignored.
                Defaults to `True`.

        Raises:
            MiddlewareFinalizationError: If a dependency refers to an
                unknown middleware name (in strict mode), if a name is
                registered more than once, or if the dependencies form
                a cycle. The error message contains the full dependency
                chain involved.

        Returns:
            Deque[Middleware]: The ordered middleware.
        """
        items = list(middleware)
        if not any(item.before or item.after for item in items):
            return deque(items)

        named: dict[str, Middleware] = {}
        for item in items:
            if item.name is None:
                continue
            existing = named.get(item.name)
            if existing is not None and existing is not item:
                raise MiddlewareFinalizationError(
                    "Cannot resolve middleware dependencies: the name "
                    f"{item.name!r} is used by more than one middleware "
                    f"({cls._label(existing)!r} and {cls._label(item)!r})."
                )
            named[item.name] = item

        index: dict[int, int] = {}
        for position, item in enumerate(items):
            index.setdefault(id(item), position)

        successors: list[set[int]] = [set() for _ in items]
        predecessors: list[set[int]] = [set() for _ in items]

        def add_edge(source: int, target: int) -> None:
            if target not in successors[source]:
                successors[source].add(target)
                predecessors[target].add(source)

        for position, item in enumerate(items):
            for dependency in item.before:
                target = named.get(dependency)
                if target is None:
                    if strict:
                        raise MiddlewareFinalizationError(
                            "Cannot resolve middleware dependencies: "
                            f"{cls._label(item)!r} is declared to run "
                            f"before {dependency!r}, but no middleware "
                            f"named {dependency!r} exists. Dependency "
                            f"chain: {cls._label(item)} -> {dependency}"
                        )
                    continue
                add_edge(position, index[id(target)])
            for dependency in item.after:
                target = named.get(dependency)
                if target is None:
                    if strict:
                        raise MiddlewareFinalizationError(
                            "Cannot resolve middleware dependencies: "
                            f"{cls._label(item)!r} is declared to run "
                            f"after {dependency!r}, but no middleware "
                            f"named {dependency!r} exists. Dependency "
                            f"chain: {dependency} -> {cls._label(item)}"
                        )
                    continue
                add_edge(index[id(target)], position)

        degrees = [len(previous) for previous in predecessors]
        heap = [
            position for position, degree in enumerate(degrees) if degree == 0
        ]
        heapify(heap)
        ordered: list[Middleware] = []
        while heap:
            position = heappop(heap)
            ordered.append(items[position])
            for following in successors[position]:
                degrees[following] -= 1
                if degrees[following] == 0:
                    heappush(heap, following)

        if len(ordered) != len(items):
            remaining = {
                position
                for position, degree in enumerate(degrees)
                if degree > 0
            }
            cycle = cls._find_cycle(successors, remaining)
            chain = " -> ".join(cls._label(items[node]) for node in cycle)
            raise MiddlewareFinalizationError(
                "Cannot resolve middleware dependencies: a dependency "
                f"cycle was detected. Dependency chain: {chain}"
            )
        return deque(ordered)

    @staticmethod
    def _find_cycle(
        successors: list[set[int]], remaining: set[int]
    ) -> list[int]:
        """Extract a dependency cycle from the unresolved middleware.

        Args:
            successors (list[set[int]]): The dependency graph edges.
            remaining (set[int]): The indexes of unresolved middleware.

        Returns:
            list[int]: The indexes forming the cycle, with the first
                node repeated at the end.
        """
        color = dict.fromkeys(remaining, 0)
        for start in sorted(remaining):
            if color[start]:
                continue
            color[start] = 1
            path = [start]
            stack = [iter(sorted(successors[start] & remaining))]
            while stack:
                advanced = False
                for following in stack[-1]:
                    state = color.get(following, 2)
                    if state == 1:
                        cycle = path[path.index(following) :]
                        return [*cycle, following]
                    if state == 0:
                        color[following] = 1
                        path.append(following)
                        stack.append(
                            iter(sorted(successors[following] & remaining))
                        )
                        advanced = True
                        break
                if not advanced:
                    stack.pop()
                    color[path.pop()] = 2
        return sorted(remaining)

    @classmethod
    def convert(
        cls,
        *middleware_collections: Sequence[Middleware | MiddlewareType],
        location: MiddlewareLocation,
    ) -> deque[Middleware]:
        """Convert middleware collections to a deque of Middleware objects.

        Args:
            *middleware_collections (Sequence[Union[Middleware, MiddlewareType]]):
                The middleware collections to convert.
            location (MiddlewareLocation): The location of the middleware.

        Returns:
            Deque[Middleware]: The converted middleware.
        """  # noqa: E501
        return deque(
            [
                middleware
                if isinstance(middleware, Middleware)
                else Middleware(middleware, location)
                for collection in middleware_collections
                for middleware in collection
            ]
        )

    @classmethod
    def reset_count(cls) -> None:
        """Reset the counter for the middleware definition order.

        This is used for testing.

        Returns:
            None
        """
        cls._counter = count()
        cls.count = next(cls._counter)
