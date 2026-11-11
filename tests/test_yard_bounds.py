"""The yard's outline leaves out a shape far from the rest of the map (a stray, e.g. one whose own ref is the empty
0, 0): the map opens on the yard, imagery is picked for where the yard is, and Map Studio says which shape it was.
Synthetic map around the made-up Annapolis yard."""
import asyncio
from types import SimpleNamespace

from app import geo, main

REF = (39.0, -76.5)
R = {"latitude": REF[0], "longitude": REF[1]}
EMPTY = {"latitude": 0.0, "longitude": 0.0}


def pts(*xy):
    return [{"x": x, "y": y, "phi": 0} for x, y in xy]


def yard(dock_ref=R):
    return {
        "areas": [{"id": 1, "name": "front", "ref": R, "range": pts((0, 0), (20, 0), (20, 20), (0, 20))},
                  {"id": 11, "name": "back", "ref": R, "range": pts((30, 0), (40, 0), (40, 10), (30, 10))}],
        "pathways": [{"id": 52, "name": "", "ref": R, "range": pts((20, 5), (30, 5))}],
        "allchargingData": [{"id": 2, "name": "Dock", "chargingPoint": {"x": 1, "y": 1}, "ref": dock_ref}],
    }


def near_yard(bbox):
    lon, lat = (bbox[0] + bbox[2]) / 2, (bbox[1] + bbox[3]) / 2
    return geo.ground_m((lon, lat), (REF[1], REF[0])) < 100 and bbox[2] - bbox[0] < 0.01 and bbox[3] - bbox[1] < 0.01


def test_a_normal_yard_keeps_every_shape():
    fc = geo.map_to_geojson(yard(), REF)
    bbox, far = geo.yard_bounds(fc, REF)
    assert far == [] and bbox == geo.bounds(fc)


def test_a_shape_with_an_empty_ref_is_left_out_and_named():
    fc = geo.map_to_geojson(yard(dock_ref=EMPTY), REF)
    assert not near_yard(geo.bounds(fc))                  # the plain outline reaches the Gulf of Guinea
    bbox, far = geo.yard_bounds(fc, REF)
    assert near_yard(bbox)
    assert [(f["kind"], f["name"]) for f in far] == [("charging", "Dock")] and far[0]["km"] > 5000


def test_without_a_usable_map_origin_the_yard_is_found_from_its_shapes():
    fc = geo.map_to_geojson(yard(dock_ref=EMPTY), REF)
    for ref in (None, (0.0, 0.0), (float("nan"), 1.0), (95.0, 0.0)):
        bbox, far = geo.yard_bounds(fc, ref)
        assert near_yard(bbox) and len(far) == 1, ref


def test_a_map_origin_away_from_every_shape_does_not_hide_the_yard():
    fc = geo.map_to_geojson(yard(), REF)
    bbox, far = geo.yard_bounds(fc, (REF[0] + 1, REF[1]))  # 111 km north of every shape
    assert near_yard(bbox) and far == []


def test_the_map_route_opens_on_the_yard_and_lists_the_stray():
    fc = geo.map_to_geojson(yard(dock_ref=EMPTY), REF)
    r = SimpleNamespace(map_geojson=fc, gps_ref=REF, map_fetched=1.0, store=SimpleNamespace(versions=lambda kind: []))
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(rec=r)))
    out = asyncio.run(main.map_geojson(request))
    assert near_yard(out["bounds"]) and [f["kind"] for f in out["far"]] == ["charging"]
    assert near_yard(main._yard_box(r))
    lon, lat = main._map_centre(SimpleNamespace(state=SimpleNamespace(imagery=None, rec=r)))
    assert geo.ground_m((lon, lat), (REF[1], REF[0])) < 100


def test_map_studio_warns_about_the_stray():
    issues = asyncio.run(main._map_issues(yard(dock_ref=EMPTY), REF))
    far = [i for i in issues if i["code"] == "far"]
    assert len(far) == 1 and far[0]["level"] == "warning" and "km from the yard" in far[0]["msg"]
    assert not [i for i in asyncio.run(main._map_issues(yard(), REF)) if i["code"] == "far"]
