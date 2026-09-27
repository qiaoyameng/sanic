from __future__ import annotations

from collections.abc import Iterable
from functools import lru_cache
from inspect import signature
from typing import Any
from uuid import UUID

from sanic_routing import BaseRouter
from sanic_routing.exceptions import NoMethod, RouteExists
from sanic_routing.exceptions import NotFound as RoutingNotFound
from sanic_routing.group import RouteGroup
from sanic_routing.route import Route
from sanic_routing.utils import parts_to_path, path_to_parts

from sanic.constants import HTTP_METHODS
from sanic.errorpages import check_error_format
from sanic.exceptions import (
    MethodNotAllowed,
    NotAcceptable,
    NotFound,
    SanicException,
    UnsupportedMediaType,
)
from sanic.headers import MediaType, parse_accept
from sanic.models.handler_types import RouteHandler


ROUTER_CACHE_SIZE = 1024
ALLOWED_LABELS = ("__file_uri__",)


def _parse_media_types(
    value: str | Iterable[str] | None, label: str
) -> tuple[MediaType, ...] | None:
    """Normalize a ``consumes``/``produces`` declaration into media types."""
    if value is None:
        return None
    if isinstance(value, str):
        value = (value,)
    media_types = []
    for item in value:
        try:
            media_type = MediaType._parse(item)
        except ValueError:
            media_type = None
        if media_type is None:
            raise ValueError(
                f"Invalid media type declared in {label}: {item!r}"
            )
        media_types.append(media_type)
    return tuple(media_types) or None


def _media_params(media_type: MediaType) -> dict[str, str]:
    return {k: v for k, v in media_type.params.items() if k != "q"}


def _media_types_overlap(
    first: tuple[MediaType, ...] | None,
    second: tuple[MediaType, ...] | None,
) -> bool:
    """Whether two media type declarations could match the same request.

    ``None`` means "any media type" and therefore overlaps with everything.
    """
    if first is None or second is None:
        return True
    for one in first:
        for two in second:
            if one.type != two.type and one.type != "*" and two.type != "*":
                continue
            if (
                one.subtype != two.subtype
                and one.subtype != "*"
                and two.subtype != "*"
            ):
                continue
            one_params = _media_params(one)
            two_params = _media_params(two)
            shared = one_params.keys() & two_params.keys()
            if any(one_params[key] != two_params[key] for key in shared):
                continue
            return True
    return False


def _consumes_match(
    consumes: tuple[MediaType, ...] | None, content_type: MediaType | None
) -> bool:
    """Whether a request Content-Type satisfies a ``consumes`` declaration."""
    if consumes is None:
        return True
    if content_type is None:
        # No (or unparsable) Content-Type header only matches full wildcards
        return any(
            media.type == "*" and media.subtype == "*" for media in consumes
        )
    return any(content_type.match(media) for media in consumes)


class Router(BaseRouter):
    """The router implementation responsible for routing a `Request` object to the appropriate handler."""  # noqa: E501

    DEFAULT_METHOD = "GET"
    ALLOWED_METHODS = HTTP_METHODS

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        # Maps (normalized path, host) -> {method: registered Route} so that
        # multiple handlers can be attached to the same path and method when
        # they declare distinct consumes/produces media types.
        self._negotiation_index: dict[
            tuple[str, str | None], dict[str, Route]
        ] = {}

    def _get(
        self, path: str, method: str, host: str | None
    ) -> tuple[Route, RouteHandler, dict[str, Any]]:
        try:
            return self.resolve(
                path=path,
                method=method,
                extra={"host": host} if host else None,
            )
        except RoutingNotFound as e:
            raise NotFound(f"Requested URL {e.path} not found") from None
        except NoMethod as e:
            raise MethodNotAllowed(
                f"Method {method} not allowed for URL {path}",
                method=method,
                allowed_methods=tuple(e.allowed_methods)
                if e.allowed_methods
                else None,
            ) from None

    @lru_cache(maxsize=ROUTER_CACHE_SIZE)
    def _resolve(
        self, path: str, method: str, host: str | None
    ) -> tuple[Route, RouteHandler, dict[str, Any]]:
        __tracebackhide__ = True
        return self._get(path, method, host)

    def get(  # type: ignore
        self,
        path: str,
        method: str,
        host: str | None,
        content_type: str | None = None,
        accept: str | None = None,
    ) -> tuple[Route, RouteHandler, dict[str, Any]]:
        """Retrieve a `Route` object containing the details about how to handle a response for a given request

        When a route declares ``consumes`` and/or ``produces`` media types,
        the ``Content-Type`` and ``Accept`` headers of the request are used
        to negotiate which of the registered handlers should respond.

        :param request: the incoming request object
        :type request: Request
        :return: details needed for handling the request and returning the
            correct response
        :rtype: Tuple[ Route, RouteHandler, Dict[str, Any]]

        Args:
            path (str): the path of the route
            method (str): the HTTP method of the route
            host (Optional[str]): the host of the route

        Raises:
            NotFound: if the route is not found
            MethodNotAllowed: if the method is not allowed for the route

        Returns:
            Tuple[Route, RouteHandler, Dict[str, Any]]: the route, handler, and match info
        """  # noqa: E501
        __tracebackhide__ = True
        route, handler, kwargs = self._resolve(path, method, host)
        if not getattr(route.extra, "candidates", None):
            return route, handler, kwargs
        negotiated = self._negotiate(path, method, host, content_type, accept)
        return negotiated, negotiated.handler, kwargs

    @lru_cache(maxsize=ROUTER_CACHE_SIZE)
    def _negotiate(
        self,
        path: str,
        method: str,
        host: str | None,
        content_type: str | None,
        accept: str | None,
    ) -> Route:
        """Select a route candidate based on Content-Type and Accept.

        The result is cached per unique combination of path, method, host,
        and headers so that content negotiation does not happen on the hot
        path more than once, and so that interleaved requests with different
        headers each get their own negotiated handler.
        """
        route, _, _ = self._resolve(path, method, host)
        candidates = getattr(route.extra, "candidates", None) or {}
        options = candidates.get(method) or []

        request_type: MediaType | None = None
        if content_type:
            try:
                request_type = MediaType._parse(content_type)
            except ValueError:
                request_type = None

        matched = [
            candidate
            for candidate in options
            if _consumes_match(candidate.extra.consumes, request_type)
        ]
        if not matched:
            expected = sorted(
                {
                    str(media)
                    for candidate in options
                    for media in (candidate.extra.consumes or ())
                }
            )
            raise UnsupportedMediaType(
                f"Unsupported media type: {content_type!r}. "
                + (
                    f"Expected one of: {', '.join(expected)}"
                    if expected
                    else "This endpoint does not accept a request body."
                )
            )

        accepts = parse_accept(accept)
        best: tuple[tuple[Any, ...], Route] | None = None
        for index, candidate in enumerate(matched):
            produces = candidate.extra.produces
            for media in produces or (None,):
                specificity = (
                    (
                        media.type != "*",
                        media.subtype != "*",
                        len(media.params),
                    )
                    if media is not None
                    else (False, False, 0)
                )
                for accept_index, accepted in enumerate(accepts):
                    if accepted.q <= 0:
                        continue
                    if media is None or accepted.match(media):
                        key = (
                            accept_index,
                            tuple(-int(part) for part in specificity),
                            index,
                        )
                        if best is None or key < best[0]:
                            best = (key, candidate)
                        break

        if best is None:
            available = sorted(
                {
                    str(media)
                    for candidate in matched
                    for media in (candidate.extra.produces or ())
                }
            )
            raise NotAcceptable(
                f"Not acceptable: {accept!r}. "
                + (
                    f"Available media types: {', '.join(available)}"
                    if available
                    else "No suitable representation available."
                )
            )

        return best[1]

    def add(  # type: ignore
        self,
        uri: str,
        methods: Iterable[str],
        handler: RouteHandler,
        host: str | Iterable[str] | None = None,
        strict_slashes: bool = False,
        stream: bool = False,
        ignore_body: bool = False,
        version: str | float | int | None = None,
        name: str | None = None,
        unquote: bool = False,
        static: bool = False,
        version_prefix: str = "/v",
        overwrite: bool = False,
        error_format: str | None = None,
        consumes: str | Iterable[str] | None = None,
        produces: str | Iterable[str] | None = None,
    ) -> Route | list[Route]:
        """Add a handler to the router

        Args:
            uri (str): The path of the route.
            methods (Iterable[str]): The types of HTTP methods that should be attached,
                example: ["GET", "POST", "OPTIONS"].
            handler (RouteHandler): The sync or async function to be executed.
            host (Optional[str], optional): Host that the route should be on. Defaults to None.
            strict_slashes (bool, optional): Whether to apply strict slashes. Defaults to False.
            stream (bool, optional): Whether to stream the response. Defaults to False.
            ignore_body (bool, optional): Whether the incoming request body should be read.
                Defaults to False.
            version (Union[str, float, int], optional): A version modifier for the uri. Defaults to None.
            name (Optional[str], optional): An identifying name of the route. Defaults to None.
            consumes (Optional[Union[str, Iterable[str]]], optional): Media types
                this handler can consume (request ``Content-Type``). When set,
                multiple handlers may share the same path and method. Defaults to None.
            produces (Optional[Union[str, Iterable[str]]], optional): Media types
                this handler can produce (response, matched against the request
                ``Accept`` header). Defaults to None.

        Returns:
            Route: The route object.
        """  # noqa: E501

        if version is not None:
            version = str(version).strip("/").lstrip("v")
            uri = "/".join([f"{version_prefix}{version}", uri.lstrip("/")])

        uri = self._normalize(uri, handler)

        consumes_types = _parse_media_types(consumes, "consumes")
        produces_types = _parse_media_types(produces, "produces")
        negotiated = bool(consumes_types or produces_types)

        # Mirror the normalization done by sanic_routing so that the
        # negotiation index lines up with the routing tree.
        normalized_path = parts_to_path(
            path_to_parts(uri, self.delimiter), self.delimiter
        )
        strip = (
            normalized_path.lstrip if strict_slashes else normalized_path.strip
        )
        normalized_path = strip(self.delimiter)

        all_methods = (
            frozenset(map(str, methods))
            if methods
            else frozenset({self.DEFAULT_METHOD})
        )

        params = dict(
            path=uri,
            handler=handler,
            methods=all_methods if methods else None,
            name=name,
            strict=strict_slashes,
            unquote=unquote,
            overwrite=overwrite,
        )

        if isinstance(host, str):
            hosts = [host]
        else:
            hosts = host or [None]  # type: ignore

        routes = []

        for host in hosts:
            params["methods"] = all_methods if methods else None
            requirements = None
            if host:
                requirements = {"host": host}
                params.update({"requirements": requirements})

            ident = name
            if len(hosts) > 1:
                ident = (
                    f"{name}_{host.replace('.', '_')}"
                    if name
                    else "__unnamed__"
                )

            key = (normalized_path, host)
            method_index = self._negotiation_index.setdefault(key, {})
            if overwrite:
                for method in all_methods:
                    method_index.pop(method, None)

            conflict_methods = frozenset(
                method for method in all_methods if method in method_index
            )
            new_methods = all_methods - conflict_methods

            if conflict_methods and not negotiated:
                if any(
                    getattr(method_index[method].extra, "negotiated", False)
                    for method in conflict_methods
                ):
                    raise RouteExists(
                        f"Route already registered: {uri} "
                        f"[{','.join(sorted(conflict_methods))}]. A handler "
                        "that declares no consumes/produces cannot overload "
                        "a content-negotiated route. Declare distinct "
                        "consumes/produces media types, or remove the "
                        "duplicate registration."
                    )
                # Preserve the original duplicate-route error behavior
                new_methods = all_methods

            created: list[Route] = []

            if conflict_methods and negotiated:
                for method in sorted(conflict_methods):
                    existing = method_index[method]
                    if not getattr(existing.extra, "negotiated", False):
                        raise RouteExists(
                            f"Route already registered: {uri} [{method}]. "
                            "Cannot overload a route that does not declare "
                            "consumes/produces. Either remove the duplicate "
                            "registration, or add consumes/produces to all "
                            "handlers for this path and method."
                        )

                candidate = self.route_class(
                    self,
                    normalized_path,
                    name or "",
                    handler=handler,
                    methods=conflict_methods,
                    requirements=requirements,
                    strict=strict_slashes,
                    unquote=unquote,
                    static="<" not in normalized_path and requirements is None,
                    regex=self._is_regex(normalized_path),
                )
                candidate.finalize()
                if name:
                    self.name_index[name] = candidate

                shared_candidates: dict[str, list[Route]] = {}
                for method in sorted(conflict_methods):
                    primary = method_index[method]
                    siblings = primary.extra.candidates[method]
                    for sibling in siblings:
                        if _media_types_overlap(
                            sibling.extra.consumes, consumes_types
                        ) and _media_types_overlap(
                            sibling.extra.produces, produces_types
                        ):
                            raise RouteExists(
                                f"Ambiguous route registration: {uri} "
                                f"[{method}] is already handled by "
                                f"{sibling.name!r} with overlapping "
                                "consumes/produces media types. Handlers "
                                "sharing a path and method must declare "
                                "distinct consumes/produces."
                            )
                    siblings.append(candidate)
                    shared_candidates[method] = siblings
                candidate.extra.candidates = shared_candidates
                created.append(candidate)

            if new_methods:
                params["methods"] = new_methods
                route = super().add(**params)  # type: ignore
                if negotiated:
                    route.extra.candidates = {
                        method: [route] for method in new_methods
                    }
                for method in new_methods:
                    method_index[method] = route
                created.append(route)

            for route in created:
                route.extra.ident = ident
                route.extra.ignore_body = ignore_body
                route.extra.stream = stream
                route.extra.hosts = hosts
                route.extra.static = static
                route.extra.error_format = error_format
                route.extra.negotiated = negotiated
                route.extra.consumes = consumes_types
                route.extra.produces = produces_types
                if not hasattr(route.extra, "candidates"):
                    route.extra.candidates = None

                if error_format:
                    check_error_format(route.extra.error_format)

                routes.append(route)

        if len(routes) == 1:
            return routes[0]
        return routes

    @lru_cache(maxsize=ROUTER_CACHE_SIZE)
    def find_route_by_view_name(
        self, view_name: str, name: str | None = None
    ) -> Route | None:
        """Find a route in the router based on the specified view name.

        Args:
            view_name (str): the name of the view to search for
            name (Optional[str], optional): the name of the route. Defaults to `None`.

        Returns:
            Optional[Route]: the route object
        """  # noqa: E501
        if not view_name:
            return None

        route = self.name_index.get(view_name)
        if not route:
            full_name = self.ctx.app.generate_name(view_name)
            route = self.name_index.get(full_name)

        if not route:
            return None

        return route

    @property
    def routes_all(self) -> dict[tuple[str, ...], Route]:
        """Return all routes in the router.

        Returns:
            Dict[Tuple[str, ...], Route]: a dictionary of routes
        """
        return {route.parts: route for route in super().routes}

    @property
    def routes(self) -> tuple[Route, ...]:
        """Return all routes in the router, including content-negotiated
        candidates that share a path and method with another handler.

        Returns:
            Tuple[Route, ...]: all route objects known to the router
        """
        routes = []
        seen = set()
        for route in super().routes:
            if id(route) not in seen:
                seen.add(id(route))
                routes.append(route)
        for route in list(routes):
            candidates = getattr(route.extra, "candidates", None) or {}
            for siblings in candidates.values():
                for candidate in siblings:
                    if id(candidate) not in seen:
                        seen.add(id(candidate))
                        routes.append(candidate)
        return tuple(routes)

    @property
    def routes_static(self) -> dict[tuple[str, ...], RouteGroup]:
        """Return all static routes in the router.

        _In this context "static" routes do not refer to the `app.static()`
        method. Instead, they refer to routes that do not contain
        any path parameters._

        Returns:
            Dict[Tuple[str, ...], Route]: a dictionary of routes
        """
        return self.static_routes

    @property
    def routes_dynamic(self) -> dict[tuple[str, ...], RouteGroup]:
        """Return all dynamic routes in the router.

        _Dynamic routes are routes that contain path parameters._

        Returns:
            Dict[Tuple[str, ...], Route]: a dictionary of routes
        """
        return self.dynamic_routes

    @property
    def routes_regex(self) -> dict[tuple[str, ...], RouteGroup]:
        """Return all regex routes in the router.

        _Regex routes are routes that contain path parameters with regex
        expressions, or otherwise need regex to resolve._

        Returns:
            Dict[Tuple[str, ...], Route]: a dictionary of routes
        """
        return self.regex_routes

    def finalize(self, *args, **kwargs) -> None:
        """Finalize the router.

        Raises:
            SanicException: if a route contains a parameter name that starts with "__" and is not in ALLOWED_LABELS
        """  # noqa: E501
        super().finalize(*args, **kwargs)

        for route in self.dynamic_routes.values():
            if any(
                label.startswith("__") and label not in ALLOWED_LABELS
                for label in route.labels
            ):
                raise SanicException(
                    f"Invalid route: {route}. Parameter names cannot use '__'."
                )

    def _normalize(self, uri: str, handler: RouteHandler) -> str:
        if "<" not in uri:
            return uri

        sig = signature(handler)
        mapping = {
            param.name: param.annotation.__name__.lower()
            for param in sig.parameters.values()
            if param.annotation in (str, int, float, UUID)
        }

        reconstruction = []
        for part in uri.split("/"):
            if part.startswith("<") and ":" not in part:
                name = part[1:-1]
                annotation = mapping.get(name)
                if annotation:
                    part = f"<{name}:{annotation}>"
            reconstruction.append(part)
        return "/".join(reconstruction)
