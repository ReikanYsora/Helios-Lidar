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
const POINT_SIZE_PX = 1.2;
const POINT_OPACITY = 0.55;
const WIRE_OPACITY = 0.30;
const BG_COLOR = 0x0b0d10;
const FOG_NEAR = 60;
const FOG_FAR  = 180;
const ROTATION_SPEED_RAD_PER_SEC = 0.035;

//Per-vertex colour ramp from "low ground" to "high apex". Low end
//matches the existing dark-grey wash so valley floors stay subtle;
//high end is the page's --accent yellow so ridges and rooftops
//catch the eye in the same colour as the text headings. Pre-
//multiplied as 0..1 RGB triples to avoid per-vertex conversion in
//the lerp loop.
const COLOR_LOW  = { r: 0x3a / 255, g: 0x3f / 255, b: 0x46 / 255 };  /* 0x3a3f46 */
const COLOR_HIGH = { r: 0xf5 / 255, g: 0xa6 / 255, b: 0x23 / 255 };  /* 0xf5a623, matches --accent */
//Height bounds for the colour normalisation. terrainHeight() ranges
//~[-13, +13]; buildings stamp up to ~+20 on top of that. We clamp
//inside [HEIGHT_LOW, HEIGHT_HIGH] so the gradient hits its endpoint
//colours on the tallest stacked features rather than averaging out.
const HEIGHT_LOW  = -10;
const HEIGHT_HIGH =  18;

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

    const geometry = buildSyntheticTerrain();
    const points = new THREE.Points(
        geometry,
        new THREE.PointsMaterial({
            //White base lets the per-vertex colour come through pure;
            //the multiply happens at the shader, so any non-white
            //base would dim the warm highs we want to read clearly.
            color: 0xffffff,
            size: POINT_SIZE_PX,
            sizeAttenuation: false,
            transparent: true,
            opacity: POINT_OPACITY,
            depthWrite: false,
            vertexColors: true,
            fog: true,
        }),
    );
    const wireframe = new THREE.LineSegments(
        buildWireframeFromGrid(geometry, GRID),
        new THREE.LineBasicMaterial({
            color: 0xffffff,
            transparent: true,
            opacity: WIRE_OPACITY,
            depthWrite: false,
            vertexColors: true,
            fog: true,
        }),
    );
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

    let last = performance.now();
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
    //Parallel colour buffer, one RGB triple per vertex, painted by
    //lerping COLOR_LOW -> COLOR_HIGH based on the vertex's height.
    //Building rooftops and ridge crests land on the warm end; valley
    //floors stay in the dark base tone.
    const colors    = new Float32Array(N * N * 3);
    const half = (GRID * CELL) / 2;
    const heightSpan = HEIGHT_HIGH - HEIGHT_LOW;

    let i = 0;
    let c = 0;
    for (let gz = 0; gz < N; gz++)
    {
        const z = gz * CELL - half;
        for (let gx = 0; gx < N; gx++)
        {
            const x = gx * CELL - half;
            let h = terrainHeight(x, z);
            for (const b of buildings)
            {
                const dx = x - b.x;
                const dz = z - b.z;
                h += b.h * Math.exp(-(dx * dx + dz * dz) / (2 * b.sigma * b.sigma));
            }
            positions[i++] = x;
            positions[i++] = h;
            positions[i++] = z;

            let t = (h - HEIGHT_LOW) / heightSpan;
            if (t < 0) t = 0;
            else if (t > 1) t = 1;
            colors[c++] = COLOR_LOW.r + (COLOR_HIGH.r - COLOR_LOW.r) * t;
            colors[c++] = COLOR_LOW.g + (COLOR_HIGH.g - COLOR_LOW.g) * t;
            colors[c++] = COLOR_LOW.b + (COLOR_HIGH.b - COLOR_LOW.b) * t;
        }
    }
    const g = new THREE.BufferGeometry();
    g.setAttribute('position', new THREE.BufferAttribute(positions, 3));
    g.setAttribute('color',    new THREE.BufferAttribute(colors,    3));
    return g;
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
    //Share the per-vertex colour ramp with the wireframe so a line
    //segment crossing a hill blends naturally from dark base to warm
    //top instead of staying a flat grey.
    g.setAttribute('color',    pointsGeometry.getAttribute('color'));
    g.setIndex(new THREE.BufferAttribute(indices, 1));
    return g;
}

if (document.readyState === 'loading')
{
    document.addEventListener('DOMContentLoaded', init, { once: true });
}
else
{
    init();
}
