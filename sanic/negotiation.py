from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING

from sanic.exceptions import NotAcceptable, UnsupportedMediaType
from sanic.headers import MediaType, parse_accept


if TYPE_CHECKING:
    from sanic_routing.route import Route


def normalize_media_types(
    value: str | Iterable[str] | None,
) -> tuple[str, ...]:
    """Normalize a ``consumes``/``produces`` declaration.

    Accepts a single media type, a comma-separated string, or an iterable
    of media types, and returns a tuple of normalized media type strings.

    Args:
        value (Union[str, Iterable[str], None]): The declared media types.

    Raises:
        ValueError: If one of the values is not a valid media type.

    Returns:
        Tuple[str, ...]: The normalized media types.
    """
    if not value:
        return ()
    if isinstance(value, str):
        value = (value,)

    media_types: list[str] = []
    for item in value:
        for raw in str(item).split(","):
            raw = raw.strip()
            if not raw:
                continue
            media_type = _parse_media_type(raw)
            if media_type is None:
                raise ValueError(f"Invalid media type: {raw}")
            params = "".join(
                f";{key}={val}"
                for key, val in media_type.params.items()
                if key != "q"
            )
            media_types.append(
                f"{media_type.type.lower()}/{media_type.subtype.lower()}"
                f"{params}"
            )
    return tuple(media_types)


def filter_by_content_type(
    routes: list[Route] | tuple[Route, ...],
    content_type: str | None,
) -> list[Route]:
    """Select the route variants able to consume the request body.

    Args:
        routes (List[Route]): The route variants registered on the path.
        content_type (Optional[str]): The raw ``Content-Type`` header.

    Raises:
        UnsupportedMediaType: If no variant can consume the content type.

    Returns:
        List[Route]: The variants that accept the request content type.
    """
    if content_type:
        matched = [
            route
            for route in routes
            if _consumes_match(route.extra.consumes, content_type)
        ]
    else:
        matched = [route for route in routes if not route.extra.consumes]

    if not matched:
        supported = sorted(
            {media for route in routes for media in route.extra.consumes}
        )
        message = "Unsupported media type"
        if content_type:
            message += f": {content_type}"
        if supported:
            message += f". Supported: {', '.join(supported)}"
        raise UnsupportedMediaType(message)
    return matched


def select_by_accept(
    routes: list[Route] | tuple[Route, ...],
    accept: str | None,
) -> Route:
    """Select the route variant best matching the ``Accept`` header.

    Variants are scored by the q value, wildcards, and parameters of the
    matching ``Accept`` entries. Ties are broken in favor of the variant
    that was registered first.

    Args:
        routes (List[Route]): The route variants to choose from.
        accept (Optional[str]): The raw ``Accept`` header.

    Raises:
        NotAcceptable: If no variant can produce an acceptable response.

    Returns:
        Route: The variant that should handle the request.
    """
    accept_list = parse_accept(accept)

    best_route: Route | None = None
    best_key: tuple[int, int, int] | None = None
    for index, route in enumerate(routes):
        produces = route.extra.produces
        for accept_index, accepted in enumerate(accept_list):
            if accepted.q <= 0:
                continue
            specificity = -1
            if produces:
                matched = next(
                    (
                        media_type
                        for media_type in (
                            _parse_media_type(mime) for mime in produces
                        )
                        if media_type and accepted.match(media_type)
                    ),
                    None,
                )
                if matched is None:
                    continue
                specificity = (
                    (matched.type != "*")
                    + (matched.subtype != "*")
                    + len(matched.params)
                )
            key = (accept_index, -specificity, index)
            if best_key is None or key < best_key:
                best_key = key
                best_route = route
            break

    if best_route is None:
        available = sorted(
            {media for route in routes for media in route.extra.produces}
        )
        message = "Not acceptable"
        if accept:
            message += f": {accept}"
        if available:
            message += f". Available: {', '.join(available)}"
        raise NotAcceptable(message)
    return best_route


def _consumes_match(consumes: tuple[str, ...], content_type: str) -> bool:
    if not consumes:
        return True
    request_type = _parse_media_type(content_type)
    if request_type is None:
        return False
    return any(
        request_type.match(declared)
        for declared in (_parse_media_type(mime) for mime in consumes)
        if declared
    )


def _parse_media_type(value: str) -> MediaType | None:
    try:
        media_type = MediaType._parse(value)
    except (ValueError, AttributeError):
        return None
    if media_type is None:
        return None
    media_type.type = media_type.type.lower()
    media_type.subtype = media_type.subtype.lower()
    return media_type
