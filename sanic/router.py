from __future__ import annotations

from collections.abc import Iterable
from functools import lru_cache
from inspect import signature
from typing import Any
from unicodedata import normalize
from urllib.parse import quote, unquote
from uuid import UUID

from sanic_routing import BaseRouter
from sanic_routing.exceptions import NoMethod
from sanic_routing.exceptions import NotFound as RoutingNotFound
from sanic_routing.group import RouteGroup
from sanic_routing.route import Route

from sanic.constants import HTTP_METHODS
from sanic.errorpages import check_error_format
from sanic.exceptions import MethodNotAllowed, NotFound, SanicException
from sanic.models.handler_types import RouteHandler


ROUTER_CACHE_SIZE = 1024
ALLOWED_LABELS = ("__file_uri__",)


def normalize_nfc_path(path: str) -> str:
    """Normalize a raw (still percent-encoded) URL path to NFC form.

    Each segment is percent-decoded exactly once, normalized to Unicode
    NFC, and re-encoded. Segments whose decoded form would contain a path
    separator (``/`` or ``\\``) are left untouched so that encodings like
    ``%2F``, ``%5C``, or double encodings like ``%252F`` can never be
    turned into new path levels by normalization.

    Args:
        path (str): The raw request path.

    Returns:
        str: The canonical NFC, percent-encoded path used for matching.
    """
    if path.isascii() and "%" not in path:
        return path
    return "/".join(
        _normalize_nfc_segment(segment) for segment in path.split("/")
    )


def _normalize_nfc_segment(segment: str) -> str:
    decoded = unquote(segment, errors="replace")
    if "/" in decoded or "\\" in decoded:
        return segment
    return quote(normalize("NFC", decoded))


class Router(BaseRouter):
    """The router implementation responsible for routing a `Request` object to the appropriate handler."""  # noqa: E501

    DEFAULT_METHOD = "GET"
    ALLOWED_METHODS = HTTP_METHODS

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._nfc_origins: dict[str, str] = {}

    @property
    def nfc_paths(self) -> bool:
        """Whether NFC path normalization mode is enabled.

        Returns:
            bool: True when the ``NFC_PATHS`` config option is enabled.
        """
        app = getattr(self.ctx, "app", None)
        return bool(app and app.config.get("NFC_PATHS", False))

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

    def get(  # type: ignore
        self, path: str, method: str, host: str | None
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

        Raises:
            NotFound: if the route is not found
            MethodNotAllowed: if the method is not allowed for the route

        Returns:
            Tuple[Route, RouteHandler, Dict[str, Any]]: the route, handler, and match info
        """  # noqa: E501
        __tracebackhide__ = True
        if self.nfc_paths:
            path = normalize_nfc_path(path)
        return self._get_cached(path, method, host)

    @lru_cache(maxsize=ROUTER_CACHE_SIZE)
    def _get_cached(  # type: ignore
        self, path: str, method: str, host: str | None
    ) -> tuple[Route, RouteHandler, dict[str, Any]]:
        __tracebackhide__ = True
        return self._get(path, method, host)

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

        Returns:
            Route: The route object.
        """  # noqa: E501

        if version is not None:
            version = str(version).strip("/").lstrip("v")
            uri = "/".join([f"{version_prefix}{version}", uri.lstrip("/")])

        uri = self._normalize(uri, handler)

        if self.nfc_paths:
            uri = self._normalize_nfc_uri(uri, overwrite)

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

            route = super().add(**params)  # type: ignore
            route.extra.ident = ident
            route.extra.ignore_body = ignore_body
            route.extra.stream = stream
            route.extra.hosts = hosts
            route.extra.static = static
            route.extra.error_format = error_format

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

    def _normalize_nfc_uri(self, uri: str, overwrite: bool) -> str:
        normalized = normalize("NFC", uri)
        origin = self._nfc_origins.setdefault(normalized, uri)
        if origin != uri:
            if not overwrite:
                raise SanicException(
                    "Route collision after NFC normalization: "
                    f"{origin!r} and {uri!r} both normalize to "
                    f"{normalized!r}. Routes must be unambiguous when "
                    "the NFC_PATHS option is enabled."
                )
            self._nfc_origins[normalized] = uri
        return normalized

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
