import * as THREE from 'three';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';

const $ = (id) => document.getElementById(id);
const state = {
  source: null,
  sourceMesh: null,
  sourceGeometry: null,
  result: null,
  side: null,
  mode: 'mirror',
  picking: false,
  busy: false,
  revision: 0,
  marker: null,
  arrow: null,
};
const viewer = $('viewer');
const scene = new THREE.Scene();
const camera = new THREE.PerspectiveCamera(35, 1, 0.001, 10000);
const renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true });
renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
renderer.setClearColor(0, 0);
renderer.localClippingEnabled = true;
renderer.outputColorSpace = THREE.SRGBColorSpace;
viewer.prepend(renderer.domElement);
const controls = new OrbitControls(camera, renderer.domElement);
controls.enableDamping = true;
controls.dampingFactor = 0.08;
scene.add(new THREE.HemisphereLight(0xffffff, 0x7e8b66, 2.2));
const key = new THREE.DirectionalLight(0xffffff, 3.0);
key.position.set(20, 30, 40);
scene.add(key);
const fill = new THREE.DirectionalLight(0xfff0da, 1.0);
fill.position.set(-20, 0, -10);
scene.add(fill);
const boneMaterial = new THREE.MeshStandardMaterial({
  color: 0xdfcca8,
  roughness: 0.75,
  metalness: 0,
  side: THREE.DoubleSide,
});
const backingMaterial = new THREE.MeshStandardMaterial({
  color: 0x629a8d,
  roughness: 0.67,
  metalness: 0,
  side: THREE.DoubleSide,
});
const rimMaterial = new THREE.MeshStandardMaterial({
  color: 0x8eb3a2,
  roughness: 0.7,
  side: THREE.DoubleSide,
});
const collisionMaterial = new THREE.MeshBasicMaterial({
  color: 0xd34f32,
  side: THREE.DoubleSide,
  polygonOffset: true,
  polygonOffsetFactor: -2,
  depthTest: true,
});
const clipped = [boneMaterial, backingMaterial, rimMaterial, collisionMaterial];
const raycaster = new THREE.Raycaster();
const pointer = new THREE.Vector2();
let model = new THREE.Group();
scene.add(model);
let edgeLines = null;
let bounds = null;

function status(title, detail, kind = '') {
  $('status-title').textContent = title;
  $('status-detail').textContent = detail;
  $('status').className = 'status ' + kind;
}
function busy(
  value,
  title = 'Building & checking the shell…',
  detail = 'This can take a minute for a detailed scan.',
) {
  state.busy = value;
  $('busy').hidden = !value;
  $('busy-title').textContent = title;
  $('busy-detail').textContent = detail;
  for (const id of ['example', 'empty-example', 'pick', 'generate', 'reverse', 'export'])
    $(id).disabled = value;
  $('file').disabled = value;
  $('units').disabled = value;
  $('generate').disabled = value || !state.source || state.side === null;
  $('pick').disabled = value || !state.source;
  $('reverse').disabled = value || state.side === null;
  $('export').disabled = value || !state.result?.report.valid_geometry;
}
async function api(url, options = {}) {
  const response = await fetch(url, options);
  if (!response.ok) {
    let message;
    try {
      const error = await response.json();
      message = typeof error.detail === 'string' ? error.detail : JSON.stringify(error.detail);
    } catch {
      message = response.statusText;
    }
    throw new Error(message);
  }
  return response;
}
function decode(buffer) {
  const header = new DataView(buffer),
    nv = header.getUint32(0, true),
    nf = header.getUint32(4, true);
  const geometry = new THREE.BufferGeometry();
  geometry.setAttribute(
    'position',
    new THREE.BufferAttribute(new Float32Array(buffer, 8, nv * 3), 3),
  );
  geometry.setIndex(new THREE.BufferAttribute(new Uint32Array(buffer, 8 + nv * 12, nf * 3), 1));
  geometry.computeVertexNormals();
  geometry.computeBoundingBox();
  geometry.computeBoundingSphere();
  return geometry;
}
function disposeModel() {
  model.traverse((o) => {
    if (o.geometry && o.geometry !== state.sourceGeometry) o.geometry.dispose();
  });
  scene.remove(model);
  model = new THREE.Group();
  scene.add(model);
  edgeLines = null;
}
function showSource() {
  disposeModel();
  const mesh = new THREE.Mesh(state.sourceGeometry, boneMaterial);
  state.sourceMesh = mesh;
  model.add(mesh);
  bounds = state.sourceGeometry.boundingBox.clone();
  updateClipping();
  updateEdges();
}
function fit() {
  if (!bounds) return;
  const center = bounds.getCenter(new THREE.Vector3());
  const size = bounds.getSize(new THREE.Vector3());
  const outward = new THREE.Vector3(...state.source.suggested_direction).negate();
  const distance =
    ((Math.max(...size.toArray()) / (2 * Math.tan(THREE.MathUtils.degToRad(camera.fov) / 2))) *
      1.55) /
    Math.min(camera.aspect, 1);
  camera.position.copy(center).addScaledVector(outward, distance);
  // Keep the long dimension visually upright without imposing scanner axes.
  const longAxis = new THREE.Vector3(0, 1, 0);
  longAxis.addScaledVector(outward, -longAxis.dot(outward));
  if (longAxis.length() < 0.1) longAxis.set(1, 0, 0);
  camera.up.copy(longAxis.normalize());
  camera.near = Math.max(size.length() / 10000, 0.0001);
  camera.far = size.length() * 1000;
  camera.updateProjectionMatrix();
  controls.target.copy(center);
  controls.update();
  updateScale();
}
function updateScale() {
  if (!bounds) return;
  const size = bounds.getSize(new THREE.Vector3()).length();
  let mm = size > 100 ? 20 : size > 30 ? 10 : size > 8 ? 5 : 1;
  const vertical =
    2 *
    Math.tan(THREE.MathUtils.degToRad(camera.fov) / 2) *
    camera.position.distanceTo(controls.target);
  const px = (mm / vertical) * viewer.clientHeight;
  $('scale-bar').style.width = px + 'px';
  $('scale-label').textContent = mm + ' mm';
}
function updateClipping() {
  const value = Number($('cutaway').value);
  if (!bounds || value === 0) {
    for (const material of clipped) material.clippingPlanes = [];
    return;
  }
  const normal = new THREE.Vector3();
  camera.getWorldDirection(normal);
  const center = bounds.getCenter(new THREE.Vector3()),
    radius = bounds.getSize(new THREE.Vector3()).length() / 2;
  const origin = center.clone().addScaledVector(normal, -radius + (2 * radius * value) / 100);
  const plane = new THREE.Plane().setFromNormalAndCoplanarPoint(normal, origin);
  for (const material of clipped) material.clippingPlanes = [plane];
}
function updateEdges() {
  if (edgeLines) {
    model.remove(edgeLines);
    edgeLines.geometry.dispose();
    edgeLines.material.dispose();
    edgeLines = null;
  }
  if (!$('show-edges').checked) return;
  const source = state.result?.geometry || state.sourceGeometry;
  if (!source) return;
  edgeLines = new THREE.LineSegments(
    new THREE.WireframeGeometry(source),
    new THREE.LineBasicMaterial({ color: 0x526848, transparent: true, opacity: 0.13 }),
  );
  model.add(edgeLines);
}
function invalidate() {
  state.revision++;
  if (state.result) {
    state.result = null;
    $('metrics').hidden = true;
    showSource();
    status(
      'Preview needs rebuilding',
      'Your settings changed. Build the backing to check the new geometry.',
      'warning',
    );
  }
  $('export').disabled = true;
  $('collision-legend').hidden = true;
  updateArrow();
}
function clearSelection() {
  state.side = null;
  state.picking = false;
  if (state.marker) {
    scene.remove(state.marker);
    state.marker.geometry.dispose();
    state.marker.material.dispose();
    state.marker = null;
  }
  if (state.arrow) {
    scene.remove(state.arrow);
    state.arrow.dispose();
    state.arrow = null;
  }
  $('pick-banner').hidden = true;
  $('side-info').textContent = 'No side selected';
  $('side-info').className = 'side-info';
  $('pick').textContent = 'Choose cortical side';
  $('build-note').textContent = 'Choose the cortical side to begin.';
}
function updateArrow() {
  if (state.arrow) {
    scene.remove(state.arrow);
    state.arrow.dispose();
    state.arrow = null;
  }
  if (!state.source || state.side === null) return;
  let vector;
  if (state.mode !== 'curve')
    vector = new THREE.Vector3(Number($('dx').value), Number($('dy').value), Number($('dz').value));
  else vector = state.selectionNormal.clone().multiplyScalar(state.side);
  if (vector.length() < 1e-8) return;
  const size = bounds.getSize(new THREE.Vector3()).length();
  state.arrow = new THREE.ArrowHelper(
    vector.normalize(),
    state.selectionPoint,
    size * 0.2,
    0x3e816c,
    size * 0.048,
    size * 0.025,
  );
  scene.add(state.arrow);
}
async function load(example = false, file = null) {
  if (state.busy) return;
  busy(true, 'Inspecting the cortical surface…', 'Reading triangles, boundaries and scan defects.');
  try {
    let response;
    if (example) response = await api('/api/example', { method: 'POST' });
    else {
      const form = new FormData();
      form.append('file', file);
      form.append('units', $('units').value);
      response = await api('/api/upload', { method: 'POST', body: form });
    }
    const info = await response.json();
    const geometry = decode(
      await (await api(`/api/surfaces/${info.token}/geometry`)).arrayBuffer(),
    );
    disposeModel();
    state.sourceGeometry?.dispose();
    state.source = info;
    state.sourceGeometry = geometry;
    state.result = null;
    state.revision++;
    clearSelection();
    showSource();
    fit();
    ['empty', 'metrics'].forEach((id) => ($(id).hidden = true));
    ['fileinfo', 'viewer-tools', 'legend', 'scale'].forEach((id) => ($(id).hidden = false));
    $('filename').textContent = info.filename;
    $('units').value = info.units;
    $('dimensions').textContent = info.size_mm.map((x) => x.toFixed(2)).join(' × ') + ' mm';
    $('triangles').textContent = info.faces.toLocaleString() + ' source triangles';
    $('speck-note').textContent = info.speck_faces
      ? `${info.speck_faces} detached speck triangle(s), ${info.speck_area_mm2.toExponential(2)} mm². Exclusion is recorded.`
      : 'No detached scan specks found.';
    const dir = info.suggested_direction;
    ['dx', 'dy', 'dz'].forEach((id, i) => ($(id).value = dir[i].toFixed(5)));
    $('cutaway').value = 0;
    updateClipping();
    $('collision-legend').hidden = true;
    if (info.closed)
      status(
        'The source is already closed',
        'Export the open cortical surface before Artec fills the holes.',
        'warning',
      );
    else if (info.source_intersections || info.nonmanifold_edges)
      status(
        'The source needs repair',
        `${info.source_intersections} intersecting faces; ${info.nonmanifold_edges} non-manifold edges. Repair the scan before adding a backing.`,
        'warning',
      );
    else
      status(
        'Surface loaded · choose the cortical side',
        `An open surface with ${info.boundary_edges.toLocaleString()} boundary edges. Rotate it and click the good bone.`,
      );
  } catch (error) {
    status('Could not open the fragment', error.message, 'error');
  } finally {
    busy(false);
  }
}
function setMode(mode) {
  if (state.busy) return;
  state.mode = mode;
  for (const name of ['mirror', 'curve', 'direction']) {
    $(name).classList.toggle('active', name === mode);
    $(name).setAttribute('aria-pressed', String(name === mode));
  }
  $('arrow-options').hidden = mode === 'curve';
  $('smooth-row').hidden = mode !== 'curve';
  $('mirror-options').hidden = mode !== 'mirror';
  $('mirror-controls').hidden = mode !== 'mirror';
  $('offset-controls').hidden = mode === 'mirror';
  $('thickness-label').textContent = mode === 'mirror' ? 'Minimum skin gap' : 'Target thickness';
  $('thickness-help').textContent =
    mode === 'mirror'
      ? 'Gap is measured along the arrow. Curvature also contributes to total depth.'
      : 'Thickness sets growth distance. Tapered areas can have thinner walls.';
  $('mode-help').textContent =
    mode === 'mirror'
      ? 'A smaller reflected skin faces outward on the opposite side. Matching borders form a tapered sandwich.'
      : mode === 'curve'
        ? 'Growth follows the changing curvature of the fragment.'
        : 'The arrow gives a single growth direction. Tangential folds can produce collisions.';
  updateBackingLabels();
  invalidate();
}
function updateBackingLabels() {
  const flat = $('backing-shape').value === 'flat';
  $('copy-size-label').textContent = flat ? 'Back outline size' : 'Copy size';
  $('copy-size-help').textContent = flat
    ? 'A smaller flat version of the smoothed cut outline, perpendicular to the arrow.'
    : '90% means 10% smaller in all three dimensions.';
  $('copy-smoothing-controls').hidden = flat;
  if (state.mode === 'mirror')
    $('mode-help').textContent = flat
      ? 'A flat back follows a smaller, smoother outline of your fragment. A tapered bridge joins it to the cortex.'
      : 'A smaller reflected skin faces outward on the opposite side. Bridge smoothing softens the connecting wall.';
}
function beginPick() {
  if (!state.source || state.busy) return;
  $('cutaway').value = 0;
  updateClipping();
  if (state.result) invalidate();
  state.picking = true;
  $('pick-banner').hidden = false;
  viewer.style.cursor = 'crosshair';
}
let down = null;
renderer.domElement.addEventListener('pointerdown', (event) => {
  down = { x: event.clientX, y: event.clientY };
});
renderer.domElement.addEventListener('pointerup', (event) => {
  if (!state.picking || !down || Math.hypot(event.clientX - down.x, event.clientY - down.y) > 5)
    return;
  const box = renderer.domElement.getBoundingClientRect();
  pointer.set(
    ((event.clientX - box.left) / box.width) * 2 - 1,
    (-(event.clientY - box.top) / box.height) * 2 + 1,
  );
  raycaster.setFromCamera(pointer, camera);
  const hit = raycaster.intersectObject(state.sourceMesh)[0];
  if (!hit) return;
  const normal = new THREE.Vector3(...state.source.face_normals[hit.faceIndex]);
  state.side = normal.dot(camera.position.clone().sub(hit.point)) >= 0 ? -1 : 1;
  state.selectionNormal = normal;
  state.selectionPoint = hit.point.clone();
  state.selectionFace = hit.faceIndex;
  state.picking = false;
  viewer.style.cursor = '';
  $('pick-banner').hidden = true;
  if (state.marker) {
    scene.remove(state.marker);
    state.marker.geometry.dispose();
    state.marker.material.dispose();
  }
  const r = bounds.getSize(new THREE.Vector3()).length() * 0.009;
  state.marker = new THREE.Mesh(
    new THREE.SphereGeometry(r, 16, 12),
    new THREE.MeshBasicMaterial({ color: 0x426f50, depthTest: false }),
  );
  state.marker.position.copy(hit.point);
  scene.add(state.marker);
  $('side-info').textContent = 'Cortical side selected · growth goes behind it';
  $('side-info').className = 'side-info selected';
  $('pick').textContent = 'Choose side again';
  $('build-note').textContent = 'Core cortex stays fixed; cut-edge cleanup follows your allowance.';
  if (state.mode === 'direction') {
    const d = normal.clone().multiplyScalar(state.side);
    ['dx', 'dy', 'dz'].forEach((id, i) => ($(id).value = d.toArray()[i].toFixed(5)));
  }
  if (state.mode === 'mirror') {
    const d = new THREE.Vector3(...state.source.suggested_direction);
    if (d.dot(normal.clone().multiplyScalar(state.side)) < 0) d.negate();
    ['dx', 'dy', 'dz'].forEach((id, i) => ($(id).value = d.toArray()[i].toFixed(5)));
  }
  invalidate();
  busy(false);
  status(
    'Cortex protected · ready to build',
    'The arrow points toward the added material. Set thickness and build the backing.',
  );
});
async function generate() {
  if (state.busy || !state.source || state.side === null) return;
  state.picking = false;
  viewer.style.cursor = '';
  $('pick-banner').hidden = true;
  const revision = state.revision;
  const settings = {
    token: state.source.token,
    thickness: Number($('thickness').value),
    side: state.side,
    mode: state.mode,
    smoothing: Number($('smoothing').value),
    adaptive: $('adaptive').checked,
    minimum: Number($('minimum').value),
    direction: ['dx', 'dy', 'dz'].map((id) => Number($(id).value)),
    exclude_specks: $('specks').checked,
  };
  Object.assign(settings, {
    copy_percent: Number($('copy-percent').value),
    copy_smoothing: Number($('copy-smoothing').value),
    edge_rounding: Number($('edge-rounding').value),
    selected_face: state.selectionFace,
    bridge_smoothing: Number($('bridge-smoothing').value),
    backing_shape: $('backing-shape').value,
  });
  busy(
    true,
    'Building & checking the shell…',
    'Checking for intersections and tapering tight areas, if enabled.',
  );
  state.result = null;
  $('metrics').hidden = true;
  try {
    const result = await (
      await api('/api/preview', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(settings),
      })
    ).json();
    const geometry = decode(
      await (await api(`/api/results/${result.token}/geometry`)).arrayBuffer(),
    );
    if (revision !== state.revision) {
      geometry.dispose();
      status(
        'Settings changed during the build',
        'Build again to preview the current settings.',
        'warning',
      );
      return;
    }
    result.geometry = geometry;
    state.result = result;
    disposeModel();
    geometry.clearGroups();
    geometry.addGroup(0, result.source_faces * 3, 0);
    geometry.addGroup(result.source_faces * 3, result.backing_faces * 3, 1);
    geometry.addGroup(
      (result.source_faces + result.backing_faces) * 3,
      geometry.index.count - (result.source_faces + result.backing_faces) * 3,
      2,
    );
    const mesh = new THREE.Mesh(geometry, [boneMaterial, backingMaterial, rimMaterial]);
    model.add(mesh);
    bounds = geometry.boundingBox.clone();
    if (result.collision_faces.length) {
      const idx = geometry.index.array,
        indices = new Uint32Array(result.collision_faces.length * 3);
      result.collision_faces.forEach((face, i) =>
        indices.set(idx.subarray(face * 3, face * 3 + 3), i * 3),
      );
      const errorGeom = new THREE.BufferGeometry();
      errorGeom.setAttribute('position', geometry.attributes.position);
      errorGeom.setIndex(new THREE.BufferAttribute(indices, 1));
      model.add(new THREE.Mesh(errorGeom, collisionMaterial));
    }
    updateClipping();
    updateEdges();
    updateBackingVisibility();
    const report = result.report;
    $('metrics').hidden = false;
    $('movement').textContent = report.changed_edge_faces
      ? `${report.changed_edge_faces} edge faces`
      : 'None';
    $('geometry-status').textContent = report.valid_geometry
      ? 'Closed · checks passed'
      : 'Needs adjustment';
    $('growth-range').textContent =
      state.mode === 'mirror'
        ? `${report.mirrored_copy_percent}% ${report.backing_shape === 'flat' ? 'flat back' : 'copy'} · ${report.total_axial_depth_mm.toFixed(2)} mm depth`
        : report.displacement_min_mm.toFixed(3) +
          '–' +
          report.target_displacement_mm.toFixed(1) +
          ' mm';
    $('volume').textContent = report.volume_mm3.toFixed(1) + ' mm³';
    $('collision-legend').hidden = !result.collision_faces.length;
    if (report.valid_geometry) {
      const taper =
        report.tapered_vertex_percent > 0.01
          ? ` Backing tapered at ${report.tapered_vertex_percent.toFixed(1)}% of vertices; median growth ${report.displacement_median_mm.toFixed(2)} mm.`
          : '';
      const specks = report.excluded_speck_faces
        ? ` Excluded ${report.excluded_speck_faces} microscopic scan speck(s).`
        : '';
      const mirror =
        state.mode === 'mirror'
          ? ` ${report.backing_shape === 'flat' ? 'Flat back at' : 'Reflected skin at'} ${report.mirrored_copy_percent}%; bridge smoothing ${report.bridge_smoothing_mm} mm. ${report.changed_edge_faces} original edge triangles reworked. Core cortex stays fixed.`
          : '';
      status(
        'Closed shell · geometry checks passed',
        `No detected intersections. The ${report.preserved_faces.toLocaleString()} retained cortical triangles stay fixed.${taper}${specks}${mirror}`,
        'valid',
      );
    } else {
      const issues = [];
      if (report.self_intersection_faces)
        issues.push(
          `${report.self_intersection_faces.toLocaleString()} intersecting faces shown in red`,
        );
      if (!report.watertight) issues.push('open boundaries');
      if (report.degenerate_faces) issues.push(`${report.degenerate_faces} collapsed triangles`);
      if (report.nonmanifold_vertices)
        issues.push(`${report.nonmanifold_vertices} non-manifold vertices`);
      if (report.volume_mm3 <= 0) issues.push('no positive enclosed volume');
      if (report.core_surface_preserved === false)
        issues.push('source changes outside the allowed edge band');
      const help =
        state.mode === 'mirror'
          ? 'Try another arrow direction, a different copy size or gap, or adjust edge cleanup.'
          : 'Try more growth smoothing, lower thickness, a lower taper limit, or a different direction.';
      status('Adjust the backing · export is blocked', `${issues.join('; ')}. ${help}`, 'error');
    }
    $('build-note').textContent =
      state.mode === 'mirror'
        ? report.backing_shape === 'flat'
          ? 'Flat back with a smoothed bridge. Check the attachment edge and overhangs in your slicer.'
          : 'Mirrored skin with a smoothed bridge. No thickness taper on the copy.'
        : report.tapered_vertex_percent > 0.01
          ? `Thin areas reach ${report.displacement_min_mm.toFixed(3)} mm displacement. Check these details in your slicer.`
          : 'Target growth applied without tapering.';
  } catch (error) {
    showSource();
    status('Backing needs adjustment', error.message, 'error');
  } finally {
    busy(false);
  }
}
function updateBackingVisibility() {
  backingMaterial.visible = $('show-backing').checked;
  rimMaterial.visible = $('show-backing').checked;
}
$('example').onclick = () => load(true);
$('empty-example').onclick = () => load(true);
$('dropzone').onclick = () => {
  if (!state.busy) $('file').click();
};
$('dropzone').onkeydown = (event) => {
  if (event.key === 'Enter' || event.key === ' ') {
    event.preventDefault();
    if (!state.busy) $('file').click();
  }
};
$('file').onchange = (event) => {
  const file = event.target.files[0];
  if (file) load(false, file);
  event.target.value = '';
};
['dragenter', 'dragover'].forEach((type) =>
  $('dropzone').addEventListener(type, (event) => {
    event.preventDefault();
    $('dropzone').classList.add('dragover');
  }),
);
['dragleave', 'drop'].forEach((type) =>
  $('dropzone').addEventListener(type, (event) => {
    event.preventDefault();
    $('dropzone').classList.remove('dragover');
  }),
);
$('dropzone').addEventListener('drop', (event) => {
  const file = event.dataTransfer.files[0];
  if (file && !state.busy) load(false, file);
});
$('units').onchange = () => {
  if (state.source)
    status(
      'Units apply to the next file',
      'Reopen the source to change its units. The currently loaded mesh remains in millimetres.',
      'warning',
    );
};
$('pick').onclick = beginPick;
$('cancel-pick').onclick = () => {
  state.picking = false;
  viewer.style.cursor = '';
  $('pick-banner').hidden = true;
};
$('reverse').onclick = () => {
  state.side *= -1;
  if (state.mode !== 'curve')
    for (const id of ['dx', 'dy', 'dz']) $(id).value = -Number($(id).value);
  invalidate();
  status(
    'Growth side reversed',
    'The arrow shows the new growth side. Build the backing to check it.',
  );
};
$('mirror').onclick = () => setMode('mirror');
$('curve').onclick = () => setMode('curve');
$('direction').onclick = () => setMode('direction');
$('thickness-range').oninput = () => {
  $('thickness').value = $('thickness-range').value;
  invalidate();
};
$('thickness').oninput = () => {
  $('thickness-range').value = $('thickness').value;
  invalidate();
};
for (const id of [
  'smoothing',
  'minimum',
  'dx',
  'dy',
  'dz',
  'copy-percent',
  'copy-smoothing',
  'edge-rounding',
  'bridge-smoothing',
])
  $(id).oninput = invalidate;
$('backing-shape').onchange = () => {
  updateBackingLabels();
  invalidate();
};
for (const id of ['adaptive', 'specks']) $(id).onchange = invalidate;
$('from-view').onclick = () => {
  const vector = new THREE.Vector3();
  camera.getWorldDirection(vector);
  ['dx', 'dy', 'dz'].forEach((id, i) => ($(id).value = vector.toArray()[i].toFixed(5)));
  invalidate();
};
$('generate').onclick = generate;
$('fit').onclick = fit;
$('show-backing').onchange = updateBackingVisibility;
$('show-edges').onchange = updateEdges;
$('cutaway').oninput = updateClipping;
$('export').onclick = () => {
  if (!state.result?.report.valid_geometry || state.busy) return;
  const link = document.createElement('a');
  link.href = `/api/results/${state.result.token}/export/${$('export-format').value}`;
  link.download = '';
  document.body.append(link);
  link.click();
  link.remove();
};
new ResizeObserver(() => {
  const w = viewer.clientWidth,
    h = viewer.clientHeight;
  renderer.setSize(w, h);
  camera.aspect = w / h;
  camera.updateProjectionMatrix();
  updateScale();
}).observe(viewer);
controls.addEventListener('change', () => {
  updateScale();
  if (Number($('cutaway').value)) updateClipping();
});
renderer.setAnimationLoop(() => {
  controls.update();
  renderer.render(scene, camera);
});
// Read-only debug access makes the scientific workflow reproducible in UI tests.
window.fossilForge = { state, scene, camera, controls, renderer };
if (new URLSearchParams(location.search).get('example') === '1') load(true);
