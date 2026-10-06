"""Bounded, in-memory readers for zipped Shapefiles and KML documents.

KML coordinates are WGS84 longitude/latitude. Altitudes, when supplied, are
validated but omitted: this service measures horizontal lengths and areas.
"""

from __future__ import annotations

import codecs
import lzma
import math
import stat
import struct
import zlib
from dataclasses import dataclass
from datetime import date, datetime
from io import BytesIO
from pathlib import PurePosixPath
from typing import Any
from zipfile import BadZipFile, ZipFile

import shapefile
from defusedxml import ElementTree
from defusedxml.common import DefusedXmlException
from pyproj import CRS
from pyproj.exceptions import CRSError
from shapely.errors import GEOSException
from shapely.geometry import (
    GeometryCollection,
    LineString,
    MultiLineString,
    MultiPoint,
    MultiPolygon,
    Point,
    Polygon,
    shape,
)
from shapely.geometry.base import BaseGeometry

MAX_EXPANDED_BYTES = 50 * 1024 * 1024
MAX_ZIP_ENTRIES = 64
MAX_COORDINATES = 250_000
MAX_XML_ELEMENTS = 500_000
MAX_XML_DEPTH = 64
KML_NAMESPACES = {
    "",
    "http://www.opengis.net/kml/2.2",
    "http://earth.google.com/kml/2.0",
    "http://earth.google.com/kml/2.1",
    "http://earth.google.com/kml/2.2",
}
GX_NAMESPACE = "http://www.google.com/kml/ext/2.2"
SUPPORTED_KML_GEOMETRIES = {"Point", "LineString", "LinearRing", "Polygon", "MultiGeometry"}
UNSUPPORTED_KML_GEOMETRIES = {"Model", "Track", "MultiTrack"}


class ParseError(ValueError):
    """An input file is malformed, unsafe, or exceeds a configured limit."""


@dataclass(frozen=True)
class ParsedFeature:
    feature_id: str
    geometry: BaseGeometry | None
    properties: dict[str, Any]
    unsupported_geometry: str | None = None


@dataclass(frozen=True)
class ParsedDataset:
    crs: CRS
    features: list[ParsedFeature]


@dataclass
class _CoordinateBudget:
    used: int = 0

    def add(self, count: int) -> None:
        self.used += count
        if self.used > MAX_COORDINATES:
            raise ParseError(f"The file exceeds the {MAX_COORDINATES:,}-coordinate limit.")


def parse_file(filename: str, content: bytes, *, max_features: int = 10_000) -> ParsedDataset:
    """Read a .zip Shapefile or .kml without extracting files to disk."""
    if max_features < 1:
        raise ValueError("max_features must be positive")
    if not content:
        raise ParseError("The uploaded file is empty.")
    if len(content) > MAX_EXPANDED_BYTES:
        raise ParseError("The file exceeds the parser's 50 MiB size limit.")
    extension = PurePosixPath(filename).suffix.lower()
    if extension == ".zip":
        return _parse_shapefile(content, max_features)
    if extension == ".kml":
        return _parse_kml(content, max_features)
    raise ParseError("Upload a .zip containing one Shapefile or a .kml file.")


def _read_archive(content: bytes) -> dict[str, bytes]:
    """Validate every member before reading any of its bytes."""
    try:
        with ZipFile(BytesIO(content)) as archive:
            entries = archive.infolist()
            if len(entries) > MAX_ZIP_ENTRIES:
                raise ParseError(f"ZIP archives may contain at most {MAX_ZIP_ENTRIES} entries.")
            seen: set[str] = set()
            members = []
            total_size = 0
            for entry in entries:
                name = entry.orig_filename
                raw_parts = name.rstrip("/").split("/")
                if (
                    not name
                    or "\\" in name
                    or name.startswith("/")
                    or any(part in {"", ".", ".."} or ":" in part for part in raw_parts)
                    or "\x00" in name
                ):
                    raise ParseError("ZIP contains an unsafe or ambiguous member path.")
                normalized = name.rstrip("/").casefold()
                if normalized in seen:
                    raise ParseError("ZIP contains duplicate member paths (ignoring case).")
                seen.add(normalized)
                if stat.S_ISLNK(entry.external_attr >> 16):
                    raise ParseError("ZIP archives must not contain symbolic links.")
                if entry.flag_bits & 1:
                    raise ParseError("Encrypted ZIP members are not supported.")
                total_size += entry.file_size
                if total_size > MAX_EXPANDED_BYTES:
                    raise ParseError("ZIP expanded contents exceed the 50 MiB limit.")
                if not entry.is_dir():
                    members.append((normalized, entry))
            result = {}
            for name, entry in members:
                with archive.open(entry) as member:
                    data = member.read(MAX_EXPANDED_BYTES + 1)
                if len(data) != entry.file_size or len(data) > MAX_EXPANDED_BYTES:
                    raise ParseError("ZIP member size is invalid or exceeds the limit.")
                result[name] = data
            return result
    except ParseError:
        raise
    except (
        BadZipFile,
        OSError,
        EOFError,
        RuntimeError,
        NotImplementedError,
        ValueError,
        zlib.error,
        lzma.LZMAError,
    ) as exc:
        raise ParseError("The ZIP archive is corrupt or uses unsupported compression.") from exc


def _shapefile_encoding(cpg: bytes | None) -> str:
    if cpg is None:
        return "utf-8"
    if len(cpg) > 128:
        raise ParseError("The Shapefile .cpg encoding declaration is invalid.")
    try:
        value = cpg.decode("utf-8-sig").strip()
        encoding = "utf-8" if value == "65001" else f"cp{value}" if value.isdecimal() else value
        codecs.lookup(encoding)
        return encoding
    except (LookupError, UnicodeError) as exc:
        raise ParseError("The Shapefile .cpg declares an unknown character encoding.") from exc


def _validate_shapefile_layout(shp: bytes, shx: bytes, dbf: bytes, max_features: int) -> int:
    """Check record boundaries before PyShp can silently truncate mismatched files."""
    for data in (shp, shx):
        if (
            len(data) < 100
            or struct.unpack_from(">i", data, 0)[0] != 9994
            or struct.unpack_from("<i", data, 28)[0] != 1000
            or struct.unpack_from(">i", data, 24)[0] * 2 != len(data)
        ):
            raise ParseError("The Shapefile contains a malformed .shp or .shx header.")
    if (len(shx) - 100) % 8 or shp[32:36] != shx[32:36]:
        raise ParseError("The Shapefile .shx index is malformed or mismatches the .shp file.")
    count = (len(shx) - 100) // 8
    if count > max_features:
        raise ParseError(f"The file exceeds the {max_features:,}-feature limit.")
    if len(dbf) < 33:
        raise ParseError("The Shapefile contains a malformed .dbf header.")
    records, header_size, record_size = struct.unpack_from("<IHH", dbf, 4)
    if records != count:
        raise ParseError("The Shapefile geometry and attribute record counts do not match.")
    if header_size < 33 or record_size < 1 or header_size + count * record_size > len(dbf):
        raise ParseError("The Shapefile .dbf attributes are truncated or malformed.")
    expected_offset = 100
    for index in range(count):
        offset_words, content_words = struct.unpack_from(">ii", shx, 100 + 8 * index)
        offset, size = offset_words * 2, content_words * 2
        if offset != expected_offset or size < 4 or offset + 8 + size > len(shp):
            raise ParseError("The Shapefile .shx index contains malformed record boundaries.")
        if struct.unpack_from(">i", shp, offset + 4)[0] != content_words:
            raise ParseError("The Shapefile .shp record size does not match the .shx index.")
        expected_offset = offset + 8 + size
    if expected_offset != len(shp):
        raise ParseError("The Shapefile contains geometry records missing from its .shx index.")
    return count


def _parse_shapefile(content: bytes, max_features: int) -> ParsedDataset:
    members = _read_archive(content)
    shapes = [name for name in members if name.endswith(".shp")]
    if len(shapes) != 1:
        raise ParseError("The ZIP must contain exactly one .shp dataset.")
    stem = shapes[0][:-4]
    required = {suffix: stem + suffix for suffix in (".shp", ".shx", ".dbf", ".prj")}
    missing = [suffix for suffix, name in required.items() if name not in members]
    if missing:
        raise ParseError(
            "The Shapefile is missing matching "
            + ", ".join(missing)
            + " files. Include a .prj so its coordinate system is explicit."
        )
    try:
        crs = CRS.from_wkt(members[required[".prj"]].decode("utf-8-sig").strip())
    except (CRSError, UnicodeError, ValueError) as exc:
        raise ParseError("The Shapefile .prj does not contain a valid coordinate system.") from exc

    count = _validate_shapefile_layout(
        members[required[".shp"]],
        members[required[".shx"]],
        members[required[".dbf"]],
        max_features,
    )
    encoding = _shapefile_encoding(members.get(stem + ".cpg"))
    features: list[ParsedFeature] = []
    budget = _CoordinateBudget()
    try:
        with shapefile.Reader(
            shp=BytesIO(members[required[".shp"]]),
            shx=BytesIO(members[required[".shx"]]),
            dbf=BytesIO(members[required[".dbf"]]),
            encoding=encoding,
            encodingErrors="strict",
        ) as reader:
            for index in range(count):
                record = reader.record(index)
                if record is None:  # A deleted DBF row has no active feature.
                    continue
                properties = {
                    key: value.isoformat() if isinstance(value, (date, datetime)) else value
                    for key, value in record.as_dict().items()
                }
                if any(
                    isinstance(value, float) and not math.isfinite(value)
                    for value in properties.values()
                ):
                    raise ParseError(f"Feature {index} contains a non-finite numeric attribute.")
                source_shape = reader.shape(index)
                unsupported = None
                budget.add(len(source_shape.points))
                if any(
                    not math.isfinite(value) for point in source_shape.points for value in point
                ):
                    raise ParseError(f"Feature {index} contains non-finite coordinates.")
                if any(not math.isfinite(value) for value in getattr(source_shape, "z", [])):
                    raise ParseError(f"Feature {index} contains non-finite altitude coordinates.")
                if source_shape.shapeType == shapefile.NULL:
                    geometry = None
                elif source_shape.shapeType == shapefile.MULTIPATCH:
                    geometry = None
                    unsupported = "MultiPatch"
                else:
                    geometry = shape(source_shape.__geo_interface__)
                features.append(ParsedFeature(str(index), geometry, properties, unsupported))
    except ParseError:
        raise
    except UnicodeError as exc:
        raise ParseError(
            "Shapefile attributes cannot be decoded; include a valid .cpg file."
        ) from exc
    except (
        shapefile.ShapefileException,
        GEOSException,
        struct.error,
        ValueError,
        TypeError,
        IndexError,
        KeyError,
        OSError,
        EOFError,
    ) as exc:
        raise ParseError(
            "The Shapefile contains malformed geometry, index, or attribute data."
        ) from exc
    return ParsedDataset(crs, features)


def _tag(element: Any) -> str:
    tag = element.tag
    if not isinstance(tag, str):
        return ""
    if tag.startswith("{"):
        namespace, local_name = tag[1:].split("}", 1)
        return local_name if namespace in KML_NAMESPACES or namespace == GX_NAMESPACE else ""
    return tag


def _children(element: Any, name: str) -> list[Any]:
    return [child for child in element if _tag(child) == name]


def _single_child(element: Any, name: str) -> Any:
    children = _children(element, name)
    if len(children) != 1:
        raise ParseError(f"KML {_tag(element)} must contain exactly one {name} element.")
    return children[0]


def _coordinates(element: Any, budget: _CoordinateBudget) -> list[tuple[float, float]]:
    coordinate_element = _single_child(element, "coordinates")
    text = coordinate_element.text or ""
    tokens = text.split()
    budget.add(len(tokens))
    result = []
    for token in tokens:
        parts = token.split(",")
        if len(parts) not in {2, 3} or any(not part for part in parts):
            raise ParseError("KML coordinates must use longitude,latitude[,altitude] tuples.")
        try:
            values = [float(part) for part in parts]
        except ValueError as exc:
            raise ParseError("KML contains a non-numeric coordinate.") from exc
        if not all(math.isfinite(value) for value in values):
            raise ParseError("KML contains a non-finite coordinate.")
        longitude, latitude = values[:2]
        if not (-180 <= longitude <= 180 and -90 <= latitude <= 90):
            raise ParseError("KML longitude/latitude coordinates are outside WGS84 bounds.")
        result.append((longitude, latitude))
    return result


def _ring(element: Any, budget: _CoordinateBudget) -> list[tuple[float, float]]:
    coordinates = _coordinates(element, budget)
    if len(coordinates) < 4 or coordinates[0] != coordinates[-1]:
        raise ParseError("KML LinearRing requires at least four coordinates and a closed ring.")
    return coordinates


def _geometry(
    element: Any, budget: _CoordinateBudget, unsupported: list[str]
) -> BaseGeometry | None:
    kind = _tag(element)
    if kind in UNSUPPORTED_KML_GEOMETRIES:
        unsupported.append(kind)
        return None
    if kind == "Point":
        coordinates = _coordinates(element, budget)
        if len(coordinates) != 1:
            raise ParseError("KML Point requires exactly one coordinate.")
        return Point(coordinates[0])
    if kind == "LineString":
        coordinates = _coordinates(element, budget)
        if len(coordinates) < 2:
            raise ParseError("KML LineString requires at least two coordinates.")
        return LineString(coordinates)
    if kind == "LinearRing":
        return LineString(_ring(element, budget))
    if kind == "Polygon":
        outer = _single_child(_single_child(element, "outerBoundaryIs"), "LinearRing")
        shell = _ring(outer, budget)
        holes = [
            _ring(_single_child(boundary, "LinearRing"), budget)
            for boundary in _children(element, "innerBoundaryIs")
        ]
        return Polygon(shell, holes)
    if kind == "MultiGeometry":
        parts = []
        for child in element:
            child_kind = _tag(child)
            if child_kind not in SUPPORTED_KML_GEOMETRIES | UNSUPPORTED_KML_GEOMETRIES:
                raise ParseError("KML MultiGeometry contains an unrecognized geometry element.")
            part = _geometry(child, budget, unsupported)
            if part is not None:
                parts.append(part)
        if not parts:
            return None
        kinds = {part.geom_type for part in parts}
        if kinds == {"Point"}:
            return MultiPoint(parts)
        if kinds == {"LineString"}:
            return MultiLineString(parts)
        if kinds == {"Polygon"}:
            return MultiPolygon(parts)
        return GeometryCollection(parts)
    raise ParseError("KML contains an unrecognized geometry element.")


def _properties(placemark: Any) -> dict[str, str]:
    result: dict[str, str] = {}
    for child in placemark:
        if _tag(child) in {"name", "description"}:
            result[_tag(child)] = "".join(child.itertext()).strip()
        elif _tag(child) == "ExtendedData":
            for item in child.iter():
                name = item.attrib.get("name")
                if name and _tag(item) == "Data":
                    values = _children(item, "value")
                    result[name] = "".join(values[0].itertext()).strip() if values else ""
                elif name and _tag(item) == "SimpleData":
                    result[name] = "".join(item.itertext()).strip()
    return result


def _parse_kml(content: bytes, max_features: int) -> ParsedDataset:
    try:
        root = ElementTree.fromstring(
            content, forbid_dtd=True, forbid_entities=True, forbid_external=True
        )
    except (ElementTree.ParseError, DefusedXmlException, ValueError, LookupError) as exc:
        raise ParseError(
            "The KML is invalid XML or contains prohibited DTD/entity declarations."
        ) from exc
    if _tag(root) != "kml":
        raise ParseError("The document must have a KML root element and supported KML namespace.")
    stack = [(root, 1)]
    element_count = 0
    placemarks = []
    while stack:
        element, depth = stack.pop()
        element_count += 1
        if depth > MAX_XML_DEPTH or element_count > MAX_XML_ELEMENTS:
            raise ParseError("KML XML structure exceeds the depth or element-count limit.")
        if _tag(element) == "Placemark":
            placemarks.append(element)
            if len(placemarks) > max_features:
                raise ParseError(f"The file exceeds the {max_features:,}-feature limit.")
        stack.extend((child, depth + 1) for child in reversed(list(element)))
    if not placemarks:
        raise ParseError("The KML contains no Placemark features.")
    features = []
    budget = _CoordinateBudget()
    for index, placemark in enumerate(placemarks):
        properties = _properties(placemark)
        candidates = [
            child
            for child in placemark
            if _tag(child) in SUPPORTED_KML_GEOMETRIES | UNSUPPORTED_KML_GEOMETRIES
        ]
        if len(candidates) > 1:
            raise ParseError("A KML Placemark with several geometries must use MultiGeometry.")
        unsupported: list[str] = []
        try:
            geometry = _geometry(candidates[0], budget, unsupported) if candidates else None
        except (GEOSException, ValueError, TypeError) as exc:
            if isinstance(exc, ParseError):
                raise
            raise ParseError(f"KML Placemark {index} has malformed geometry.") from exc
        unsupported_geometry = ", ".join(dict.fromkeys(unsupported)) if unsupported else None
        features.append(
            ParsedFeature(
                placemark.attrib.get("id", str(index)), geometry, properties, unsupported_geometry
            )
        )
    return ParsedDataset(CRS.from_epsg(4326), features)
