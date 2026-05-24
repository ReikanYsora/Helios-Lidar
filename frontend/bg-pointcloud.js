//Ambient LiDAR-style pointcloud + wireframe rendered behind the
//entire site. Dark grey on black, slow rotation, deliberately
//subtle so it sits as texture rather than focus.
//
//Geometry: a 160 x 160 lattice covered by a multi-octave height
//function that blends rolling hills (low frequency), ridges (mid
//frequency), and small bumps (high frequency), plus a handful of
//Gaussian "buildings" stamped on top. Three.js fog matched to the
//background colour fades the lattice into the void so the user
//never sees the grid edges, the scene feels infinite.
//
//Same Points + LineSegments share a single BufferGeometry the way
//the result-view viewer does, so points and wireframe stay
//perfectly aligned.
//
//Loaded as `<script type="module">` on every page. The canvas
//sits at z-index -1 with pointer-events: none so it never
//intercepts clicks or scroll. Renders pause on tab hidden and
//`prefers-reduced-motion` disables the rotation loop entirely.

import * as THREE from 'https://esm.sh/three@0.169.0';

const GRID = 160;
const CELL = 1.6;
const POINT_COLOR = 0x3a3f46;
const POINT_SIZE_PX = 1.2;
const POINT_OPACITY = 0.55;
const WIRE_COLOR = 0x2b3036;
const WIRE_OPACITY = 0.30;
const BG_COLOR = 0x0b0d10;
const FOG_NEAR = 60;
const FOG_FAR  = 180;
const ROTATION_SPEED_RAD_PER_SEC = 0.035;

//Slow ambient breathing of the terrain heights. We layer a low-
//frequency sine over the static landscape so the lattice gently
//rises + falls over time without ever leaving its quiet baseline.
//Frequencies are picked so the wave reads as "the ground is
//exhaling" rather than as motion: WAVE_AMPLITUDE stays well under
//the natural relief, WAVE_TIME_FREQ_HZ is sub-Hz so a full breath
//takes ~25-30 s, and the spatial frequencies are low enough that
//neighbouring points move almost together, no buzz.
const WAVE_AMPLITUDE      = 0.55;
const WAVE_TIME_FREQ_HZ   = 0.035;
const WAVE_SPACE_FREQ_X   = 0.024;
const WAVE_SPACE_FREQ_Z   = 0.031;
const WAVE_TIME_TWO_HZ    = 0.018;
const WAVE_AMPLITUDE_TWO  = 0.30;

function init()
{
    const canvas = document.createElement('canvas');
    canvas.className = 'bg-pointcloud';
    document.body.insertBefore(canvas, document.body.firstChild);

    const renderer = new THREE.WebGLRenderer({
        canvas,
        antialias: false,
        alpha: false,
        powerPreference: 'low-power',
    });
    renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 1.5));
    renderer.setClearColor(BG_COLOR, 1);

    const scene = new THREE.Scene();
    scene.fog = new THREE.Fog(BG_COLOR, FOG_NEAR, FOG_FAR);

    //Pulled in tighter than the previous grid: the camera now sits
    //inside the lattice, the fog hides the far edge entirely, so
    //the user only ever sees the central topography.
    const camera = new THREE.PerspectiveCamera(42, 1, 0.1, 500);
    camera.position.set(45, 30, 45);
    camera.lookAt(0, 0, 0);

    const root = new THREE.Group();
    scene.add(root);

    const { geometry, baseHeights } = buildSyntheticTerrain();
    const points = new THREE.Points(
        geometry,
        new THREE.PointsMaterial({
            color: POINT_COLOR,
            size: POINT_SIZE_PX,
            sizeAttenuation: false,
            transparent: true,
            opacity: POINT_OPACITY,
            depthWrite: false,
            fog: true,
        }),
    );
    const wireframe = new THREE.LineSegments(
        buildWireframeFromGrid(geometry, GRID),
        new THREE.LineBasicMaterial({
            color: WIRE_COLOR,
            transparent: true,
            opacity: WIRE_OPACITY,
            depthWrite: false,
            fog: true,
        }),
    );
    //Mark the position attribute as dynamic so the slow height
    //breathing in tick() doesn't fight a static-draw upload hint.
    geometry.attributes.position.setUsage(THREE.DynamicDrawUsage);
    root.add(points);
    root.add(wireframe);

    function resize()
    {
        const w = window.innerWidth;
        const h = window.innerHeight;
        renderer.setSize(w, h, false);
        camera.aspect = w / Math.max(1, h);
        camera.updateProjectionMatrix();
    }
    resize();
    window.addEventListener('resize', resize, { passive: true });

    const reducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
    if (reducedMotion)
    {
        renderer.render(scene, camera);
        return;
    }

    const N = GRID + 1;
    const positionAttr = geometry.attributes.position;
    const positions = positionAttr.array;
    const startTime = performance.now();
    let last = startTime;
    let running = true;

    document.addEventListener('visibilitychange', () =>
    {
        running = !document.hidden;
        if (running)
        {
            last = performance.now();
            requestAnimationFrame(tick);
        }
    });

    function tick(now)
    {
        if (!running) return;
        const dt = (now - last) / 1000;
        last = now;
        const tSec = (now - startTime) / 1000;
        applyHeightWave(positions, baseHeights, N, tSec);
        positionAttr.needsUpdate = true;
        root.rotation.y += ROTATION_SPEED_RAD_PER_SEC * dt;
        renderer.render(scene, camera);
        requestAnimationFrame(tick);
    }
    requestAnimationFrame(tick);
}

function terrainHeight(x, z)
{
    //Multi-octave synthetic terrain. Each line is a different
    //spatial scale, summed they give a credibly varied landscape.
    //Coefficients tuned by eye: bigger amplitudes for the low
    //frequencies (broad hills), smaller for high frequencies
    //(surface texture).
    let h = 0;
    h += 7.0 * Math.sin(x * 0.045) * Math.cos(z * 0.052);
    h += 3.6 * Math.sin(x * 0.115 + 0.7) * Math.cos(z * 0.128 - 0.4);
    h += 1.6 * Math.sin(x * 0.28 - 1.1) * Math.cos(z * 0.31 + 0.3);
    h += 0.7 * Math.sin(x * 0.62) * Math.cos(z * 0.71);
    return h;
}

function buildSyntheticTerrain()
{
    //Five Gaussian bumps standing in for "buildings" placed across
    //the lattice. Coordinates deterministic so the scene looks the
    //same on every page load.
    const buildings = [
        { x: -10.0, z:  -6.0, sigma: 2.8, h: 8.5 },
        { x:   6.0, z: -14.0, sigma: 2.2, h: 6.0 },
        { x:  18.0, z:   9.0, sigma: 3.4, h: 7.5 },
        { x:  -5.0, z:  15.0, sigma: 1.8, h: 4.5 },
        { x: -20.0, z:   2.0, sigma: 2.4, h: 5.5 },
    ];

    const N = GRID + 1;
    const positions = new Float32Array(N * N * 3);
    //Snapshot of the static "natural" height per vertex. The tick
    //loop reads this each frame, adds the slow breathing wave, and
    //writes the result back into positions, so the static landscape
    //never drifts away from its sculpted form.
    const baseHeights = new Float32Array(N * N);
    const half = (GRID * CELL) / 2;

    let i = 0;
    let h = 0;
    for (let gz = 0; gz < N; gz++)
    {
        const z = gz * CELL - half;
        for (let gx = 0; gx < N; gx++)
        {
            const x = gx * CELL - half;
            h = terrainHeight(x, z);
            for (const b of buildings)
            {
                const dx = x - b.x;
                const dz = z - b.z;
                h += b.h * Math.exp(-(dx * dx + dz * dz) / (2 * b.sigma * b.sigma));
            }
            positions[i++] = x;
            positions[i++] = h;
            positions[i++] = z;
            baseHeights[gz * N + gx] = h;
        }
    }
    const g = new THREE.BufferGeometry();
    g.setAttribute('position', new THREE.BufferAttribute(positions, 3));
    return { geometry: g, baseHeights };
}

function buildWireframeFromGrid(pointsGeometry, gridN)
{
    //Pair indices into right-neighbour and down-neighbour line
    //segments. Same topology the result-view viewer uses, so
    //points and wireframe agree visually.
    const N = gridN + 1;
    const rightEdges = gridN * N;
    const downEdges = N * gridN;
    const indices = new Uint32Array((rightEdges + downEdges) * 2);
    let k = 0;
    for (let gz = 0; gz < N; gz++)
    {
        for (let gx = 0; gx < gridN; gx++)
        {
            const a = gz * N + gx;
            indices[k++] = a;
            indices[k++] = a + 1;
        }
    }
    for (let gz = 0; gz < gridN; gz++)
    {
        for (let gx = 0; gx < N; gx++)
        {
            const a = gz * N + gx;
            indices[k++] = a;
            indices[k++] = a + N;
        }
    }
    const g = new THREE.BufferGeometry();
    g.setAttribute('position', pointsGeometry.getAttribute('position'));
    g.setIndex(new THREE.BufferAttribute(indices, 1));
    return g;
}

//Per-frame height update: walk every vertex, add a slow two-octave
//sine wave on top of its baseline elevation, write back the new y.
//Both Points and LineSegments share the position attribute so a
//single needsUpdate refreshes both passes. The whole loop is ~26k
//iterations on the 161x161 lattice; well under a millisecond on
//any current CPU. positions and baseHeights are captured by the
//closure in init().
function applyHeightWave(positions, baseHeights, N, tSec)
{
    const tau1 = tSec * (2 * Math.PI) * WAVE_TIME_FREQ_HZ;
    const tau2 = tSec * (2 * Math.PI) * WAVE_TIME_TWO_HZ;
    for (let gz = 0; gz < N; gz++)
    {
        for (let gx = 0; gx < N; gx++)
        {
            const baseY = baseHeights[gz * N + gx];
            //x and z in world space are already in positions[]; read
            //them back rather than recomputing from grid indices to
            //avoid storing the half offset twice.
            const off = (gz * N + gx) * 3;
            const x = positions[off];
            const z = positions[off + 2];
            const w1 = Math.sin(tau1 + x * WAVE_SPACE_FREQ_X + z * WAVE_SPACE_FREQ_Z) * WAVE_AMPLITUDE;
            const w2 = Math.sin(tau2 - x * WAVE_SPACE_FREQ_Z + z * WAVE_SPACE_FREQ_X) * WAVE_AMPLITUDE_TWO;
            positions[off + 1] = baseY + w1 + w2;
        }
    }
}

if (document.readyState === 'loading')
{
    document.addEventListener('DOMContentLoaded', init, { once: true });
}
else
{
    init();
}
