from __future__ import annotations

from collections.abc import Iterable
from functools import lru_cache
from inspect import signature
from typing import Any, cast
from uuid import UUID

from sanic_routing import BaseRouter
from sanic_routing.exceptions import NoMethod, RouteExists
from sanic_routing.exceptions import NotFound as RoutingNotFound
from sanic_routing.group import RouteGroup
from sanic_routing.route import Route
from sanic_routing.utils import parts_to_path, path_to_parts

from sanic.constants import HTTP_METHODS
from sanic.errorpages import check_error_format
from sanic.exceptions import MethodNotAllowed, NotFound, SanicException
from sanic.models.handler_types import RouteHandler
from sanic.negotiation import (
    filter_by_content_type,
    normalize_media_types,
    select_by_accept,
)


ROUTER_CACHE_SIZE = 1024
ALLOWED_LABELS = ("__file_uri__",)


class Router(BaseRouter):
    """The router implementation responsible for routing a `Request` object to the appropriate handler."""  # noqa: E501

    DEFAULT_METHOD = "GET"
    ALLOWED_METHODS = HTTP_METHODS

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._content_variants: dict[
            tuple[str, frozenset[str] | None, str | None], list[Route]
        ] = {}

    def _get(
        self,
        path: str,
        method: str,
        host: str | None,
        content_type: str | None = None,
        accept: str | None = None,
    ) -> tuple[Route, RouteHandler, dict[str, Any]]:
        try:
            route, handler, match_info = self.resolve(
                path=path,
                method=method,
                extra={"host": host} if host else None,
            )
        except RoutingNotFound as e:
            raise NotFound(f"Requested URL {e.path} not found") from None
        except NoMethod as e:
            error = MethodNotAllowed(
                f"Method {method} not allowed for URL {path}",
                method=method,
                allowed_methods=tuple(e.allowed_methods)
                if e.allowed_methods
                else None,
            )
            if method != "HEAD":
                raise error from None
            # A HEAD request may be served by a content-negotiated GET
            # route, since the response body is discarded either way.
            try:
                route, handler, match_info = self.resolve(
                    path=path,
                    method="GET",
                    extra={"host": host} if host else None,
                )
            except (RoutingNotFound, NoMethod):
                raise error from None
            variants = getattr(route.extra, "variants", None)
            if not variants or not any(
                variant.extra.consumes or variant.extra.produces
                for variant in variants
            ):
                raise error from None

        variants = getattr(route.extra, "variants", None)
        if variants and any(
            variant.extra.consumes or variant.extra.produces
            for variant in variants
        ):
            candidates = filter_by_content_type(variants, content_type)
            route = select_by_accept(candidates, accept)
            handler = route.handler

        return route, handler, match_info

    @lru_cache(maxsize=ROUTER_CACHE_SIZE)
    def get(  # type: ignore
        self,
        path: str,
        method: str,
        host: str | None,
        content_type: str | None = None,
        accept: str | None = None,
    ) -> tuple[Route, RouteHandler, dict[str, Any]]:
        """Retrieve a `Route` object containing the details about how to handle a response for a given request

        :param request: the incoming request object
        :type request: Request
        :return: details needed for handling the request and returning the
            correct response
        :rtype: Tuple[ Route, RouteHandler, Dict[str, Any]]

        Args:
            path (str): the path of the route
            method (str): the HTTP method of the route
            host (Optional[str]): the host of the route
            content_type (Optional[str]): the Content-Type header of the
                request, used to select between route variants
            accept (Optional[str]): the Accept header of the request, used
                to select between route variants

        Raises:
            NotFound: if the route is not found
            MethodNotAllowed: if the method is not allowed for the route
            UnsupportedMediaType: if no variant can consume the request body
            NotAcceptable: if no variant can produce an acceptable response

        Returns:
            Tuple[Route, RouteHandler, Dict[str, Any]]: the route, handler, and match info
        """  # noqa: E501
        __tracebackhide__ = True
        return self._get(path, method, host, content_type, accept)

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
            consumes (Optional[Union[str, Iterable[str]]], optional): Media
                types the handler can consume. Allows multiple handlers on
                the same path and method, selected by Content-Type.
            produces (Optional[Union[str, Iterable[str]]], optional): Media
                types the handler can produce. Selects between handlers on
                the same path and method using the Accept header.

        Returns:
            Route: The route object.
        """  # noqa: E501

        if version is not None:
            version = str(version).strip("/").lstrip("v")
            uri = "/".join([f"{version_prefix}{version}", uri.lstrip("/")])

        uri = self._normalize(uri, handler)
        consumes = normalize_media_types(consumes)
        produces = normalize_media_types(produces)

        params = dict(
            path=uri,
            handler=handler,
            methods=frozenset(map(str, methods)) if methods else None,
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
            if host:
                params.update({"requirements": {"host": host}})

            ident = name
            if len(hosts) > 1:
                ident = (
                    f"{name}_{host.replace('.', '_')}"
                    if name
                    else "__unnamed__"
                )

            route_methods = cast("frozenset[str] | None", params["methods"])
            key = (uri, route_methods, host)
            variants = self._content_variants.get(key)
            if overwrite and variants is not None:
                self._content_variants.pop(key)
                variants = None

            if variants is not None and (
                consumes
                or produces
                or any(
                    variant.extra.consumes or variant.extra.produces
                    for variant in variants
                )
            ):
                # Another handler for a path/method that already has one:
                # allowed only as a content-negotiated variant. Identical
                # consumes/produces pairs are ambiguous and rejected.
                for existing in variants:
                    if (existing.extra.consumes, existing.extra.produces) == (
                        consumes,
                        produces,
                    ):
                        methods = (
                            ",".join(sorted(route_methods or ()))
                            or self.DEFAULT_METHOD
                        )
                        raise RouteExists(
                            f"Route already registered: {methods} {uri} "
                            f"with consumes={consumes or 'any'} and "
                            f"produces={produces or 'any'}. Routes that "
                            "share a path and method must declare distinct "
                            "consumes/produces media types."
                        )
                route = self._add_variant(
                    uri,
                    ident,
                    params,
                    strict_slashes,
                    unquote,
                    host,
                )
                route.extra.content_variant_of = variants[0]
                variants.append(route)
            else:
                route = super().add(**params)  # type: ignore
                variants = [route]
                self._content_variants[key] = variants

            route.extra.variants = variants
            route.extra.ident = ident
            route.extra.ignore_body = ignore_body
            route.extra.stream = stream
            route.extra.hosts = hosts
            route.extra.static = static
            route.extra.error_format = error_format
            route.extra.consumes = consumes
            route.extra.produces = produces

            if error_format:
                check_error_format(route.extra.error_format)

            routes.append(route)

        if len(routes) == 1:
            return routes[0]
        return routes

    def _add_variant(
        self,
        uri: str,
        name: str | None,
        params: dict[str, Any],
        strict_slashes: bool,
        unquote: bool,
        host: str | None,
    ) -> Route:
        """Create a route object for a content-negotiated variant.

        The variant is not added to the routing tree itself. Instead, it is
        attached to the primary route of its path/method signature and is
        selected through content negotiation at resolve time.
        """
        path = parts_to_path(
            path_to_parts(uri, self.delimiter), self.delimiter
        )
        strip = path.lstrip if strict_slashes else path.strip
        path = strip(self.delimiter)
        requirements = {"host": host} if host else None
        route = self.route_class(
            self,
            path,
            name or "",
            handler=params["handler"],
            methods=params["methods"] or frozenset({self.DEFAULT_METHOD}),
            requirements=requirements,
            strict=strict_slashes,
            unquote=unquote,
            static="<" not in path and requirements is None,
            regex=self._is_regex(path),
        )
        if name:
            self.name_index[name] = route
        return route

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
    def routes(self) -> tuple[Route, ...]:
        """Return all routes, including content-negotiated variants.

        Returns:
            Tuple[Route, ...]: a tuple of routes
        """
        variants = tuple(
            route
            for group in self._content_variants.values()
            for route in group[1:]
        )
        return super().routes + variants

    @property
    def routes_all(self) -> dict[tuple[str, ...], Route]:
        """Return all routes in the router.

        Returns:
            Dict[Tuple[str, ...], Route]: a dictionary of routes
        """
        return {route.parts: route for route in self.routes}

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
