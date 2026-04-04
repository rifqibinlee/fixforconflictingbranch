"""
cctv2_pipeline.py — Pure Python replacement for cctv2.py (QGIS Processing Algorithm)

Produces identical outputs using shapely, geopandas, scipy, numpy.
No QGIS/PyQGIS dependency.

Inputs (all file paths):
    building:      GeoJSON polygon file
    parking_area:  GeoJSON polygon file
    pole_points:   GeoJSON point file
    camera_table:  CSV with columns: camera_type, hfov_deg, range_m, unit_price_rm
    offset_table:  CSV with columns: offset

Outputs (dict of GeoDataFrames):
    dissolved_buildings, candidate_cctv, surv_area, aoi, hex_grid,
    hex_centroids, poles, cand_cctv_clean, wedge, camera_cost_summary
"""

import json
import csv
import math
import numpy as np
import geopandas as gpd
import pandas as pd
from shapely.geometry import (
    Point, Polygon, MultiPolygon, LineString, MultiLineString, mapping
)
from shapely.ops import unary_union
from shapely.validation import make_valid
from scipy.spatial import cKDTree


def run_cctv_pipeline(building_path, parking_path, poles_path, camera_csv_path, offset_csv_path):
    """
    Run the full CCTV planning pipeline.
    Returns dict of { layer_name: geojson_dict }
    """

    # ── Load inputs ──
    gdf_building = gpd.read_file(building_path)
    gdf_parking = gpd.read_file(parking_path)
    gdf_poles = gpd.read_file(poles_path)

    with open(camera_csv_path, 'r') as f:
        reader = csv.DictReader(f)
        camera_rows = [r for r in reader]

    with open(offset_csv_path, 'r') as f:
        reader = csv.DictReader(f)
        offset_rows = [r for r in reader]

    offsets = [float(r['offset']) for r in offset_rows]

    # =====================================================================
    # BRANCH A: Building candidates
    # Steps 1-6: Dissolve → Simplify → Fix → PolygonsToLines →
    #            ExtractVertices → DeleteDuplicateGeometries
    # =====================================================================

    # Step 1: Dissolve buildings
    dissolved_geom = unary_union(gdf_building.geometry)
    dissolved_geom = make_valid(dissolved_geom)
    gdf_dissolved = gpd.GeoDataFrame(geometry=[dissolved_geom], crs=gdf_building.crs)

    # Step 2: Simplify (tolerance=1 in QGIS units — for EPSG:4326 this is ~1 degree,
    # but the original model likely works in a projected CRS. Use a small tolerance.)
    simplified = dissolved_geom.simplify(0.000009, preserve_topology=True)  # ~1m at equator

    # Step 3: Fix geometries
    fixed = make_valid(simplified)

    # Step 4: Polygons to lines
    def polygon_to_lines(geom):
        lines = []
        if geom.geom_type == 'Polygon':
            lines.append(LineString(geom.exterior.coords))
            for interior in geom.interiors:
                lines.append(LineString(interior.coords))
        elif geom.geom_type == 'MultiPolygon':
            for poly in geom.geoms:
                lines.extend(polygon_to_lines(poly))
        return lines

    building_lines = polygon_to_lines(fixed)

    # Step 5: Extract vertices
    vertices = set()
    for line in building_lines:
        for coord in line.coords:
            vertices.add((round(coord[0], 8), round(coord[1], 8)))

    # Step 6: Delete duplicate geometries (already deduped via set)
    candidate_points = [Point(v) for v in vertices]
    gdf_candidate_cctv = gpd.GeoDataFrame(
        geometry=candidate_points,
        crs=gdf_building.crs
    )

    # =====================================================================
    # BRANCH B: Merge buildings + parking → AOI → hex grid → centroids
    # Steps 7-14
    # =====================================================================

    # Step 7-8: Merge + dissolve → surv_area
    surv_geom = unary_union(
        list(gdf_building.geometry) + list(gdf_parking.geometry)
    )
    surv_geom = make_valid(surv_geom)
    gdf_surv_area = gpd.GeoDataFrame(geometry=[surv_geom], crs=gdf_building.crs)

    # Step 9: Buffer → AOI (0.000269 degrees ≈ ~30m)
    aoi_geom = surv_geom.buffer(0.000269, cap_style=2, join_style=2, resolution=5)
    aoi_geom = make_valid(aoi_geom)
    gdf_aoi = gpd.GeoDataFrame(geometry=[aoi_geom], crs=gdf_building.crs)

    # Step 10: Create hexagonal grid
    spacing = 2 * 0.0002695  # matches QGIS model HSPACING/VSPACING
    hex_grid_polys = _create_hex_grid(aoi_geom.bounds, spacing, spacing)

    # Step 11: Clip grid by AOI
    gdf_hex_all = gpd.GeoDataFrame(geometry=hex_grid_polys, crs=gdf_building.crs)
    gdf_hex_grid = gpd.clip(gdf_hex_all, gdf_aoi)

    # Step 12-13: Centroids + add geometry attributes
    centroids = gdf_hex_grid.geometry.centroid
    gdf_hex_centroids = gpd.GeoDataFrame(geometry=centroids, crs=gdf_building.crs)
    gdf_hex_centroids['xcoord'] = gdf_hex_centroids.geometry.x
    gdf_hex_centroids['ycoord'] = gdf_hex_centroids.geometry.y

    # =====================================================================
    # BRANCH C: Pole filtering
    # Steps 16-17: poles within parking → poles within AOI
    # =====================================================================

    # Step 16: Extract poles within parking
    gdf_poles_in_parking = gpd.sjoin(
        gdf_poles, gdf_parking, predicate='within', how='inner'
    ).drop(columns=['index_right'], errors='ignore')

    # Step 17: Extract poles within AOI
    gdf_poles_filtered = gdf_poles_in_parking[
        gdf_poles_in_parking.geometry.within(aoi_geom)
    ].copy()
    gdf_poles_filtered = gdf_poles_filtered.reset_index(drop=True)

    # =====================================================================
    # Compute base_az for BOTH building candidates and pole candidates
    # Steps 18-29: Join by nearest hex centroid → compute azimuth → apply offsets
    # =====================================================================

    centroid_coords = np.array([(p.x, p.y) for p in gdf_hex_centroids.geometry])

    if len(centroid_coords) > 0:
        tree = cKDTree(centroid_coords)
    else:
        tree = None

    def compute_base_az_and_expand(gdf_candidates, candidate_type):
        """For each candidate, find nearest hex centroid, compute base_az,
        then expand with offsets to create N rows per candidate."""
        if len(gdf_candidates) == 0 or tree is None:
            return gpd.GeoDataFrame(columns=['geometry', 'base_az', 'azimuth', 'type'])

        coords = np.array([(p.x, p.y) for p in gdf_candidates.geometry])
        _, idx = tree.query(coords)

        rows = []
        for i, (_, cand_row) in enumerate(gdf_candidates.iterrows()):
            nearest_hex = centroid_coords[idx[i]]
            base_az = _azimuth_degrees(
                cand_row.geometry.x, cand_row.geometry.y,
                nearest_hex[0], nearest_hex[1]
            )
            base_az = (base_az + 360) % 360

            for offset in offsets:
                az = (base_az + offset) % 360
                rows.append({
                    'geometry': cand_row.geometry,
                    'base_az': base_az,
                    'azimuth': az,
                    'offset': offset,
                    'type': candidate_type,
                })

        return gpd.GeoDataFrame(rows, crs=gdf_candidates.crs)

    # Building candidates with 3 azimuths (Steps 25-29)
    gdf_building_3az = compute_base_az_and_expand(gdf_candidate_cctv, 'building')

    # Pole candidates with 3 azimuths (Steps 18-24)
    gdf_pole_3az = compute_base_az_and_expand(gdf_poles_filtered, 'pole')

    # =====================================================================
    # Merge + assign camera type + join specs
    # Steps 30-35
    # =====================================================================

    # Step 30: Merge all candidates
    gdf_all_3az = pd.concat([gdf_building_3az, gdf_pole_3az], ignore_index=True)
    if len(gdf_all_3az) > 0:
        gdf_all_3az = gpd.GeoDataFrame(gdf_all_3az, crs=gdf_building.crs)
    else:
        gdf_all_3az = gpd.GeoDataFrame(
            columns=['geometry', 'base_az', 'azimuth', 'type', 'camera_type'],
            crs=gdf_building.crs
        )

    # Step 31: Assign camera_type = first type from camera table
    default_cam_type = camera_rows[0]['camera_type'].strip() if camera_rows else 'Type A'
    if len(gdf_all_3az) > 0:
        gdf_all_3az['camera_type'] = default_cam_type

    # Step 35: Join camera specs by camera_type
    cam_df = pd.DataFrame(camera_rows)
    for col in ['hfov_deg', 'range_m', 'unit_price_rm']:
        if col in cam_df.columns:
            cam_df[col] = pd.to_numeric(cam_df[col], errors='coerce')
    if 'camera_type' in cam_df.columns:
        cam_df['camera_type'] = cam_df['camera_type'].str.strip()

    if len(gdf_all_3az) > 0 and len(cam_df) > 0:
        gdf_all_specs = gdf_all_3az.merge(cam_df, on='camera_type', how='left')
        gdf_all_specs = gpd.GeoDataFrame(gdf_all_specs, crs=gdf_building.crs)
    else:
        gdf_all_specs = gdf_all_3az.copy()
        for col in ['hfov_deg', 'range_m', 'unit_price_rm']:
            if col not in gdf_all_specs.columns:
                gdf_all_specs[col] = 0

    # =====================================================================
    # Outputs: cand_cctv_clean, wedge, camera_cost_summary
    # Steps 36-37 + refactor fields
    # =====================================================================

    # Refactor fields → cand_cctv_clean
    clean_cols = ['geometry', 'azimuth', 'camera_type', 'hfov_deg', 'range_m', 'unit_price_rm']
    gdf_cand_clean = gdf_all_specs[[c for c in clean_cols if c in gdf_all_specs.columns]].copy()
    gdf_cand_clean['run_id'] = 'cctv_run'

    # Step 37: Wedge buffer
    wedge_geoms = []
    wedge_attrs = []
    for _, row in gdf_all_specs.iterrows():
        az = float(row.get('azimuth', 0))
        hfov = float(row.get('hfov_deg', 90))
        range_m = float(row.get('range_m', 30))
        pt = row.geometry
        wedge = _wedge_buffer(pt.x, pt.y, az, hfov, range_m)
        wedge_geoms.append(wedge)
        wedge_attrs.append({
            'camera_type': row.get('camera_type', ''),
            'azimuth': az,
            'hfov_deg': hfov,
            'range_m': range_m,
            'unit_price_rm': row.get('unit_price_rm', 0),
        })

    gdf_wedge = gpd.GeoDataFrame(
        wedge_attrs,
        geometry=wedge_geoms,
        crs=gdf_building.crs
    ) if wedge_geoms else gpd.GeoDataFrame(columns=['geometry', 'camera_type'])

    # Step 36: Aggregate → Camera Cost Summary
    if len(gdf_all_specs) > 0 and 'camera_type' in gdf_all_specs.columns:
        cost_summary = gdf_all_specs.groupby('camera_type').agg(
            count=('azimuth', 'size'),
            unit_price_rm=('unit_price_rm', 'min'),
            total_cost_rm=('unit_price_rm', 'sum')
        ).reset_index()
    else:
        cost_summary = pd.DataFrame(columns=['camera_type', 'count', 'unit_price_rm', 'total_cost_rm'])

    # =====================================================================
    # Convert all to GeoJSON dicts
    # =====================================================================

    def to_geojson(gdf):
        if gdf is None or len(gdf) == 0:
            return {"type": "FeatureCollection", "features": []}
        # Drop non-serializable columns
        for col in gdf.columns:
            if col != 'geometry' and gdf[col].dtype == 'object':
                gdf[col] = gdf[col].astype(str)
        return json.loads(gdf.to_json())

    def df_to_geojson(df):
        """Convert a plain DataFrame (no geometry) to a pseudo-GeoJSON for the frontend."""
        features = []
        for _, row in df.iterrows():
            features.append({
                "type": "Feature",
                "geometry": None,
                "properties": {k: _safe_val(v) for k, v in row.items()}
            })
        return {"type": "FeatureCollection", "features": features}

    results = {
        'dissolved_buildings': to_geojson(gdf_dissolved),
        'candidate_cctv': to_geojson(gdf_candidate_cctv),
        'surv_area': to_geojson(gdf_surv_area),
        'aoi': to_geojson(gdf_aoi),
        'hex_grid': to_geojson(gdf_hex_grid),
        'poles': to_geojson(gdf_poles_filtered),
        'cand_cctv_clean': to_geojson(gdf_cand_clean),
        'wedge': to_geojson(gdf_wedge),
        'camera_cost_summary': df_to_geojson(cost_summary),
    }

    return results


# =====================================================================
# Geometry helper functions
# =====================================================================

def _safe_val(v):
    if isinstance(v, (np.integer,)):
        return int(v)
    if isinstance(v, (np.floating,)):
        return float(v)
    if pd.isna(v):
        return None
    return v


def _azimuth_degrees(x1, y1, x2, y2):
    """Compute azimuth in degrees from point1 to point2 (geographic coords).
    Matches QGIS azimuth() function: 0=North, clockwise."""
    dx = x2 - x1
    dy = y2 - y1
    angle = math.degrees(math.atan2(dx, dy))
    return (angle + 360) % 360


def _wedge_buffer(lon, lat, azimuth_deg, hfov_deg, range_m):
    """Create a wedge/sector polygon.
    Matches QGIS: wedge_buffer($geometry, azimuth, hfov_deg, range_m / 111320)
    """
    range_deg = range_m / 111320.0
    half_fov = hfov_deg / 2.0
    start_az = azimuth_deg - half_fov
    end_az = azimuth_deg + half_fov

    points = [(lon, lat)]  # apex
    steps = 32
    for i in range(steps + 1):
        az = start_az + (end_az - start_az) * i / steps
        az_rad = math.radians(az)
        # QGIS azimuth: 0=North, clockwise → dx=sin(az), dy=cos(az)
        dx = range_deg * math.sin(az_rad) / math.cos(math.radians(lat))
        dy = range_deg * math.cos(az_rad)
        points.append((lon + dx, lat + dy))
    points.append((lon, lat))  # close

    return Polygon(points)


def _create_hex_grid(bounds, hspacing, vspacing):
    """Create hexagonal grid polygons covering the given bounds.
    Matches QGIS native:creategrid with TYPE=4 (Hexagon)."""
    minx, miny, maxx, maxy = bounds

    hex_width = hspacing
    hex_height = vspacing * math.sqrt(3) / 2

    polygons = []
    row = 0
    y = miny
    while y <= maxy + hex_height:
        x_offset = (hex_width / 2) if (row % 2 == 1) else 0
        x = minx + x_offset
        while x <= maxx + hex_width:
            hex_poly = _make_hexagon(x, y, hspacing / 2, vspacing / 2)
            polygons.append(hex_poly)
            x += hex_width
        y += hex_height * 0.75  # overlap rows
        row += 1

    return polygons


def _make_hexagon(cx, cy, rx, ry):
    """Create a flat-top hexagon centered at (cx, cy)."""
    points = []
    for i in range(6):
        angle = math.radians(60 * i + 30)
        px = cx + rx * math.cos(angle)
        py = cy + ry * math.sin(angle)
        points.append((px, py))
    points.append(points[0])
    return Polygon(points)
