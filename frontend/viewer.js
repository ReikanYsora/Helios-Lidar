//3D preview of the generated nDSM, modelled 1:1 on Helios's LiDAR
//View overlay. Two THREE.BufferGeometry meshes share the same
//vertex buffer:
//
//* a THREE.Points pass with PointsMaterial size 1, white @ 0.3
//  opacity, matching DEFAULT_LIDAR_VIEW_POINT_OPACITY in Helios.
//* a THREE.LineSegments pass over an index buffer that connects
//  each finite cell to its right and down neighbours, matching the
//  wireframe topology Helios builds in src/engine/lidar-view-layer.ts.
//  Material is #d0d0d0 @ 0.25, also Helios's
//  DEFAULT_LIDAR_VIEW_WIREFRAME_OPACITY.
//
//No ground plane, no grid: in the card the MapLibre basemap reads
//through the points / wireframe; on the preview the dark canvas
//background plays the same role and adding a plate would diverge
//from what the user is going to see in Helios proper.
//
//COG decoding via GeoTIFF.js range-fetches against the COG tile
//layout instead of pulling the whole file. Decimation kicks in past
//200 000 vertices so a 1000x1000 nDSM stays at 60 fps.

import * as THREE from 'https://esm.sh/three@0.169.0';
import { OrbitControls } from 'https://esm.sh/three@0.169.0/examples/jsm/controls/OrbitControls.js';
import { fromUrl } from 'https://esm.sh/geotiff@2.1.3';

//Match Helios's DEFAULT_LIDAR_VIEW_POINT_* + DEFAULT_LIDAR_VIEW_WIREFRAME_*
//constants from src/helios-config.ts. Two divergences from Helios:
//
//* WIRE_OPACITY is 0.5 here vs 0.25 in the card, because the card
//  paints the wireframe on top of the MapLibre basemap (medium-tone
//  blue-grey) where 0.25 reads cleanly, while the preview's near-
//  black canvas eats anything below ~0.4 alpha.
//* The background is a slight blue-grey (#181f25) rather than the
//  page's pure black (#0b0d10), again to give the white points and
//  grey wireframe enough contrast with their backdrop. Mimics the
//  Helios dark basemap tone without pulling in MapLibre + tiles.
const POINT_COLOR     = 0xffffff;
const POINT_OPACITY   = 0.3;
const POINT_SIZE_PX   = 1.0;
const WIRE_COLOR      = 0xd0d0d0;
const WIRE_OPACITY    = 0.5;
const BG_COLOR        = 0x181f25;

//No threshold: Helios's LiDAR View shows every finite cell, ground
//and canopy alike. Without ground cells the buildings appear to
//float in the void; drawing the ground as a dense carpet at h ~ 0
//reproduces the topology look the card paints.
const MAX_VERTICES = 1_000_000;

let currentSession = null;

export async function mountViewer(cogUrl, opts = {})
{
    if (currentSession)
    {
        currentSession.dispose();
        currentSession = null;
    }

    const canvas = document.getElementById(opts.canvasId || 'viewer-canvas');
    const loading = document.getElementById(opts.loadingId || 'viewer-loading');
    const stats = document.getElementById(opts.statsId || 'viewer-stats');
    if (!canvas)
    {
        console.warn('[helios-lidar] viewer canvas not found');
        return;
    }

    if (loading)
    {
        loading.textContent = 'Loading preview...';
        loading.style.display = 'flex';
    }
    if (stats) stats.textContent = '';

    try
    {
        currentSession = await buildSession(canvas, cogUrl, loading, stats);
    }
    catch (err)
    {
        if (loading)
        {
            loading.textContent = `Preview failed: ${err && err.message ? err.message : err}`;
        }
        throw err;
    }
}

async function buildSession(canvas, cogUrl, loading, stats)
{
    if (loading) loading.textContent = 'Decoding COG...';
    const tiff = await fromUrl(cogUrl);
    const image = await tiff.getImage();

    const width = image.getWidth();
    const height = image.getHeight();
    const resolution = image.getResolution();
    const cellSizeX = Math.abs(resolution[0]);
    const cellSizeY = Math.abs(resolution[1]);
    const fileDirectory = image.getFileDirectory();
    const gdalNoData = fileDirectory.GDAL_NODATA !== undefined
        ? Number.parseFloat(fileDirectory.GDAL_NODATA)
        : -9999.0;

    if (loading) loading.textContent = 'Reading pixels...';
    const rasters = await image.readRasters();
    const heights = rasters[0];

    //First pass: count valid (non-NoData, finite) cells so we can
    //pick a stride (integer >= 1) that keeps the final vertex count
    //under MAX_VERTICES.
    let valid = 0;
    for (let i = 0; i < heights.length; i++)
    {
        const h = heights[i];
        if (h !== gdalNoData && Number.isFinite(h))
        {
            valid++;
        }
    }
    const stride = valid > MAX_VERTICES ? Math.ceil(valid / MAX_VERTICES) : 1;
    const drawCount = Math.ceil(valid / stride);

    if (loading) loading.textContent = 'Building 3D scene...';

    //Second pass: write XYZ for each kept cell into a Float32Array
    //and remember which (row, col) -> vertex index we wrote, so the
    //wireframe pass can look up right/down neighbours by grid
    //coordinates (NOT by linear index, which the stride scrambles).
    const positions = new Float32Array(drawCount * 3);
    const vertexIndexByCell = new Int32Array(width * height);
    vertexIndexByCell.fill(-1);

    let visited = 0;
    let vi = 0;
    let minHeight = Infinity;
    let maxHeight = -Infinity;
    let sumHeight = 0;
    let countHeight = 0;

    //Centre the field on (0,0,0) in scene space so OrbitControls
    //orbits around the centre of the data, not the (0,0) corner.
    const xMid = (width - 1) * cellSizeX * 0.5;
    const zMid = (height - 1) * cellSizeY * 0.5;

    for (let row = 0; row < height; row++)
    {
        for (let col = 0; col < width; col++)
        {
            const h = heights[row * width + col];
            if (h === gdalNoData || !Number.isFinite(h)) continue;
            visited++;
            if ((visited - 1) % stride !== 0) continue;
            if (vi >= drawCount) break;

            positions[vi * 3 + 0] = col * cellSizeX - xMid;
            positions[vi * 3 + 1] = h;
            positions[vi * 3 + 2] = row * cellSizeY - zMid;
            vertexIndexByCell[row * width + col] = vi;
            vi++;

            if (h < minHeight) minHeight = h;
            if (h > maxHeight) maxHeight = h;
            sumHeight += h;
            countHeight++;
        }
    }
    const finalVertexCount = vi;

    //Wireframe topology: each kept cell connects to its right and
    //down neighbour if that neighbour is also kept. Builds
    //horizontal + vertical edges; the diagonal lines that emerge are
    //the natural by-product of dense fields, not separate primitives.
    //Matches the lineIdx pass in lidar-view-layer.ts.
    const tmpIdx = new Uint32Array(finalVertexCount * 4);
    let li = 0;
    for (let row = 0; row < height; row++)
    {
        for (let col = 0; col < width; col++)
        {
            const v = vertexIndexByCell[row * width + col];
            if (v < 0) continue;
            if (col + 1 < width)
            {
                const vR = vertexIndexByCell[row * width + col + 1];
                if (vR >= 0) { tmpIdx[li++] = v; tmpIdx[li++] = vR; }
            }
            if (row + 1 < height)
            {
                const vD = vertexIndexByCell[(row + 1) * width + col];
                if (vD >= 0) { tmpIdx[li++] = v; tmpIdx[li++] = vD; }
            }
        }
    }
    const lineIdx = li > 0 ? tmpIdx.subarray(0, li) : new Uint32Array(0);

    //Three.js scene setup.
    const renderer = new THREE.WebGLRenderer({ canvas, antialias: true, alpha: false });
    renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));

    const scene = new THREE.Scene();
    scene.background = new THREE.Color(BG_COLOR);

    const aspect = canvas.clientWidth / Math.max(1, canvas.clientHeight);
    const camera = new THREE.PerspectiveCamera(55, aspect, 0.1, 50_000);

    const controls = new OrbitControls(camera, canvas);
    controls.enableDamping = true;
    controls.dampingFactor = 0.08;

    //Shared BufferGeometry: same XYZ array drives the Points draw
    //and the LineSegments draw, so the wireframe sits exactly on
    //the points.
    const geom = new THREE.BufferGeometry();
    geom.setAttribute('position', new THREE.BufferAttribute(positions.subarray(0, finalVertexCount * 3), 3));
    geom.setIndex(new THREE.BufferAttribute(lineIdx, 1));
    geom.computeBoundingSphere();

    const pointsMat = new THREE.PointsMaterial({
        color: POINT_COLOR,
        size: POINT_SIZE_PX,
        sizeAttenuation: false,
        transparent: true,
        opacity: POINT_OPACITY,
        depthWrite: false,
    });
    //Points pass uses the geometry without honouring the index
    //buffer (PointsMaterial draws every vertex once, ignores index).
    const points = new THREE.Points(geom, pointsMat);
    scene.add(points);

    const wireMat = new THREE.LineBasicMaterial({
        color: WIRE_COLOR,
        transparent: true,
        opacity: WIRE_OPACITY,
        depthWrite: false,
    });
    const wireframe = new THREE.LineSegments(geom, wireMat);
    scene.add(wireframe);

    //Camera framing: top-down-ish (camera elevation > horizontal
    //distance) so the layout reads as a map, matching the angle
    //Helios's LiDAR View overlay uses by default. Pull back enough
    //that the home + immediate surroundings fill the preview tile,
    //rather than the whole footprint floating in dead space.
    //OrbitControls lets the user pull further out and orbit if
    //they want.
    const fitDistance = Math.max(width * cellSizeX, height * cellSizeY) * 0.65;
    camera.position.set(fitDistance * 0.55, fitDistance * 1.2, fitDistance * 0.55);
    controls.target.set(0, Math.max(0.1, maxHeight * 0.2), 0);
    controls.minDistance = Math.max(width, height) * cellSizeX * 0.05;
    controls.maxDistance = Math.max(width, height) * cellSizeX * 5;
    controls.update();

    const handleResize = () =>
    {
        const w = canvas.clientWidth;
        const h = canvas.clientHeight;
        if (w === 0 || h === 0) return;
        renderer.setSize(w, h, false);
        camera.aspect = w / Math.max(1, h);
        camera.updateProjectionMatrix();
    };
    const resizeObserver = new ResizeObserver(handleResize);
    resizeObserver.observe(canvas);
    handleResize();

    let alive = true;
    const tick = () =>
    {
        if (!alive) return;
        controls.update();
        renderer.render(scene, camera);
        requestAnimationFrame(tick);
    };
    tick();

    if (loading) loading.style.display = 'none';
    if (stats)
    {
        const mean = countHeight > 0 ? sumHeight / countHeight : 0;
        const decim = stride > 1 ? `, decimated 1 / ${stride}` : '';
        stats.textContent = (
            `${width} x ${height} cells / ${valid.toLocaleString()} valid / ` +
            `min ${minHeight.toFixed(1)} m / max ${maxHeight.toFixed(1)} m / ` +
            `mean ${mean.toFixed(1)} m${decim}`
        );
    }

    return {
        dispose()
        {
            alive = false;
            resizeObserver.disconnect();
            controls.dispose();
            geom.dispose();
            pointsMat.dispose();
            wireMat.dispose();
            renderer.dispose();
        },
    };
}
