from __future__ import annotations

from collections import deque
from collections.abc import Iterable, Sequence
from enum import IntEnum, auto
from heapq import heapify, heappop, heappush
from itertools import count

from sanic.exceptions import SanicException
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
        name (str, optional): An optional name that other middleware can
            use to declare a dependency on this middleware.
        before (Union[str, Sequence[str]], optional): Name(s) of middleware
            that this middleware must run before.
        after (Union[str, Sequence[str]], optional): Name(s) of middleware
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
        *,
        name: str | None = None,
        before: str | Iterable[str] | None = None,
        after: str | Iterable[str] | None = None,
    ) -> None:
        self.func = func
        self.priority = priority
        self.location = location
        self.definition = next(Middleware._counter)
        self.name = name
        self.before = self._normalize(before)
        self.after = self._normalize(after)

    def __call__(self, *args, **kwargs):
        return self.func(*args, **kwargs)

    def __hash__(self) -> int:
        return hash(self.func)

    def __repr__(self) -> str:
        return (
            f"{self.__class__.__name__}("
            f"func=<function {self.func.__name__}>, "
            f"priority={self.priority}, "
            f"name={self.name!r}, "
            f"location={self.location.name})"
        )

    @property
    def order(self) -> tuple[int, int]:
        """Return a tuple of the priority and definition order.

        This is used to sort the middleware.

        Returns:
            tuple[int, int]: The priority and definition order.
        """
        return (self.priority, -self.definition)

    @property
    def execution_key(self) -> tuple[int, int]:
        """Return the sort key for execution order.

        Middleware are executed from the highest priority to the lowest.
        Request middleware with the same priority run in definition order,
        while response middleware run in reverse definition order.

        Returns:
            tuple[int, int]: The sort key for execution order.
        """
        if self.location is MiddlewareLocation.REQUEST:
            return (-self.priority, self.definition)
        return (self.priority, -self.definition)

    @property
    def display_name(self) -> str:
        """Return a human readable name for the middleware.

        Returns:
            str: The middleware name, falling back to the function name.
        """
        if self.name:
            return self.name
        return getattr(self.func, "__name__", repr(self.func))

    @staticmethod
    def _normalize(
        dependencies: str | Iterable[str] | None,
    ) -> tuple[str, ...]:
        if dependencies is None:
            return ()
        if isinstance(dependencies, str):
            return (dependencies,)
        return tuple(dependencies)

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
    def resolve(
        cls,
        middleware: Sequence[Middleware],
        location: MiddlewareLocation,
    ) -> list[Middleware]:
        """Resolve the execution order of a collection of middleware.

        Middleware are ordered by priority and definition order. Middleware
        that declare ``before`` or ``after`` dependencies are additionally
        constrained to run before or after the named middleware. Middleware
        without any dependencies keep their relative priority and
        registration order.

        Args:
            middleware (Sequence[Middleware]): The middleware to order.
            location (MiddlewareLocation): The location of the middleware.

        Raises:
            SanicException: If a dependency references an unknown middleware
                name, if a name is shared by different middleware functions,
                or if the dependencies form a cycle.

        Returns:
            List[Middleware]: The middleware in execution order.
        """
        items = list(middleware)
        named: dict[str, list[int]] = {}
        named_funcs: dict[str, MiddlewareType] = {}
        for index, item in enumerate(items):
            if item.name is None:
                continue
            func = named_funcs.setdefault(item.name, item.func)
            if func is not item.func:
                raise SanicException(
                    f"Middleware name {item.name!r} is used by more than one "
                    "middleware function. Middleware names must be unique "
                    "within a middleware collection."
                )
            named.setdefault(item.name, []).append(index)

        predecessors: list[set[int]] = [set() for _ in items]
        successors: list[set[int]] = [set() for _ in items]
        unknown: list[tuple[Middleware, str, str]] = []

        def link(first: int, second: int) -> None:
            if first == second or second in successors[first]:
                return
            successors[first].add(second)
            predecessors[second].add(first)

        for index, item in enumerate(items):
            for dependency in item.after:
                targets = named.get(dependency)
                if not targets:
                    unknown.append((item, dependency, "after"))
                    continue
                for target in targets:
                    link(target, index)
            for dependency in item.before:
                targets = named.get(dependency)
                if not targets:
                    unknown.append((item, dependency, "before"))
                    continue
                for target in targets:
                    link(index, target)

        if unknown:
            chains = "; ".join(
                f"{item.display_name} -> {dependency} "
                f"(unknown name declared in {kind!r})"
                for item, dependency, kind in unknown
            )
            available = ", ".join(sorted(named)) or "none"
            raise SanicException(
                "Middleware dependency references an unknown name: "
                f"{chains}. Named middleware available: {available}"
            )

        ready = [
            (items[index].execution_key, index)
            for index in range(len(items))
            if not predecessors[index]
        ]
        heapify(ready)
        ordered: list[Middleware] = []
        while ready:
            _, index = heappop(ready)
            ordered.append(items[index])
            for following in successors[index]:
                predecessors[following].discard(index)
                if not predecessors[following]:
                    heappush(
                        ready, (items[following].execution_key, following)
                    )

        if len(ordered) != len(items):
            cycle = cls._find_cycle(items, predecessors)
            chain = " -> ".join(cycle)
            raise SanicException(
                f"Middleware dependency cycle detected: {chain}"
            )
        return ordered

    @staticmethod
    def _find_cycle(
        items: list[Middleware], predecessors: list[set[int]]
    ) -> list[str]:
        remaining = {
            index for index in range(len(items)) if predecessors[index]
        }
        start = next(iter(remaining))
        chain = [start]
        seen = {start: 0}
        current = start
        while True:
            following = next(
                index for index in predecessors[current] if index in remaining
            )
            if following in seen:
                cycle = chain[seen[following] :] + [following]
                return [items[index].display_name for index in cycle]
            seen[following] = len(chain)
            chain.append(following)
            current = following

    @classmethod
    def reset_count(cls) -> None:
        """Reset the counter for the middleware definition order.

        This is used for testing.

        Returns:
            None
        """
        cls._counter = count()
        cls.count = next(cls._counter)
