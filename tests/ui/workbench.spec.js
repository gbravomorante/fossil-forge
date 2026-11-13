const { test, expect } = require('@playwright/test');
const fs = require('fs');
test('load real fossil, choose cortex, validate shell, export, invalidate stale preview', async ({
  page,
}) => {
  const errors = [];
  page.on('pageerror', (error) => errors.push(error.message));
  await page.goto('/');
  await expect(page.locator('#export')).toBeDisabled();
  await page.locator('#example').click();
  await expect(page.locator('#filename')).toHaveText('Example.ply', { timeout: 30000 });
  await expect(page.locator('#generate')).toBeDisabled();
  await page.locator('#curve').click();
  await page.locator('#pick').click();
  // Select a visible triangle by projecting real mesh coordinates into the canvas.
  const point = await page.evaluate(() => {
    const { state, camera, renderer } = window.fossilForge;
    const positions = state.sourceGeometry.attributes.position;
    const indices = state.sourceGeometry.index.array;
    const face = Math.floor(indices.length / 6);
    const THREEVector = state.sourceMesh.position.constructor;
    const v = new THREEVector();
    for (let j = 0; j < 3; j++)
      v.add(new THREEVector().fromBufferAttribute(positions, indices[face * 3 + j]));
    v.multiplyScalar(1 / 3).project(camera);
    const box = renderer.domElement.getBoundingClientRect();
    return { x: box.left + ((v.x + 1) * box.width) / 2, y: box.top + ((1 - v.y) * box.height) / 2 };
  });
  await page.mouse.click(point.x, point.y);
  await expect(page.locator('#side-info')).toContainText('Cortical side selected');
  await page.locator('#generate').click();
  await expect(page.locator('#status-title')).toHaveText('Closed shell · geometry checks passed', {
    timeout: 150000,
  });
  await expect(page.locator('#export')).toBeEnabled();
  const report = await page.evaluate(() => window.fossilForge.state.result.report);
  expect(report.source_surface_preserved).toBe(true);
  expect(report.self_intersection_faces).toBe(0);
  await page.locator('#cutaway').fill('48');
  await page.screenshot({ path: 'outputs/workbench.png', fullPage: true });
  await page.locator('#cutaway').fill('0');
  await page.screenshot({ path: 'outputs/workbench-solid.png', fullPage: true });
  await page.locator('#export-format').selectOption('bundle');
  const downloadPromise = page.waitForEvent('download');
  await page.locator('#export').click();
  const download = await downloadPromise;
  await download.saveAs('outputs/Example_backing_project.zip');
  expect(fs.statSync('outputs/Example_backing_project.zip').size).toBeGreaterThan(1000000);
  await page.locator('#thickness').fill('3');
  await expect(page.locator('#export')).toBeDisabled();
  await expect(page.locator('#status-title')).toHaveText('Preview needs rebuilding');
  expect(errors).toEqual([]);
});

test('failed closed source never permits export', async ({ page }) => {
  await page.goto('/');
  await page
    .locator('#file')
    .setInputFiles({
      name: 'closed.obj',
      mimeType: 'text/plain',
      buffer: Buffer.from(
        'v 0 0 0\nv 1 0 0\nv 0 1 0\nv 0 0 1\nf 1 3 2\nf 1 2 4\nf 2 3 4\nf 3 1 4\n',
      ),
    });
  await expect(page.locator('#status-title')).toHaveText('The source is already closed', {
    timeout: 30000,
  });
  await expect(page.locator('#export')).toBeDisabled();
});

test('mirrored sandwich on real fossil preserves core and exports', async ({ page }) => {
  const errors = [];
  page.on('pageerror', (error) => errors.push(error.message));
  await page.goto('/?example=1');
  await expect(page.locator('#filename')).toHaveText('Example.ply', { timeout: 30000 });
  await expect(page.locator('#mirror')).toHaveAttribute('aria-pressed', 'true');
  await expect(page.locator('#copy-percent')).toHaveValue('90');
  await page.locator('#bridge-smoothing').fill('0');
  await page.locator('#pick').click();
  const point = await page.evaluate(() => {
    const { state, camera, renderer } = window.fossilForge;
    const positions = state.sourceGeometry.attributes.position,
      indices = state.sourceGeometry.index.array;
    const Vector = state.sourceMesh.position.constructor,
      v = new Vector();
    const face = Math.floor(indices.length / 6);
    for (let j = 0; j < 3; j++)
      v.add(new Vector().fromBufferAttribute(positions, indices[face * 3 + j]));
    v.multiplyScalar(1 / 3).project(camera);
    const box = renderer.domElement.getBoundingClientRect();
    return { x: box.left + ((v.x + 1) * box.width) / 2, y: box.top + ((1 - v.y) * box.height) / 2 };
  });
  await page.mouse.click(point.x, point.y);
  await expect(page.locator('#side-info')).toContainText('Cortical side selected');
  await page.locator('#generate').click();
  await expect(page.locator('#status-title')).toHaveText('Closed shell · geometry checks passed', {
    timeout: 150000,
  });
  await expect(page.locator('#export')).toBeEnabled();
  const report = await page.evaluate(() => window.fossilForge.state.result.report);
  expect(report.core_surface_preserved).toBe(true);
  expect(report.changed_edge_faces).toBeGreaterThan(0);
  expect(report.changed_edge_faces).toBeLessThan(50);
  expect(report.self_intersection_faces).toBe(0);
  expect(report.mirrored_copy_percent).toBe(90);
  expect(report.total_axial_depth_mm).toBeGreaterThan(report.minimum_skin_gap_mm);
  // Inspect both outward faces and the tapered wall from an oblique angle.
  await page.evaluate(() => {
    const { camera, controls, state } = window.fossilForge;
    const Vector = camera.position.constructor,
      d = new Vector(...state.result.report.mirror_direction);
    const axis = new Vector(0, 1, 0).cross(d).normalize();
    const offset = camera.position.clone().sub(controls.target);
    offset.applyAxisAngle(axis, Math.PI * 0.6);
    camera.position.copy(controls.target).add(offset);
    camera.lookAt(controls.target);
    controls.update();
  });
  await page.screenshot({ path: 'outputs/mirror-workbench.png', fullPage: true });
  await page.locator('#export-format').selectOption('stl');
  const pending = page.waitForEvent('download');
  await page.locator('#export').click();
  await (await pending).saveAs('outputs/Example_mirror_ui.stl');
  await page.locator('#copy-percent').fill('80');
  await expect(page.locator('#export')).toBeDisabled();
  await expect(page.locator('#status-title')).toHaveText('Preview needs rebuilding');
  expect(errors).toEqual([]);
});

test('smooth bridge and flat back on real fossil', async ({ page }) => {
  test.setTimeout(300000);
  const errors = [];
  page.on('pageerror', (error) => errors.push(error.message));
  await page.goto('/?example=1');
  await expect(page.locator('#filename')).toHaveText('Example.ply', { timeout: 30000 });
  await expect(page.locator('#bridge-smoothing')).toHaveValue('1');
  await page.locator('#pick').click();
  const point = await page.evaluate(() => {
    const { state, camera, renderer } = window.fossilForge;
    const positions = state.sourceGeometry.attributes.position,
      indices = state.sourceGeometry.index.array;
    const Vector = state.sourceMesh.position.constructor,
      v = new Vector(),
      face = Math.floor(indices.length / 6);
    for (let j = 0; j < 3; j++)
      v.add(new Vector().fromBufferAttribute(positions, indices[face * 3 + j]));
    v.multiplyScalar(1 / 3).project(camera);
    const box = renderer.domElement.getBoundingClientRect();
    return { x: box.left + ((v.x + 1) * box.width) / 2, y: box.top + ((1 - v.y) * box.height) / 2 };
  });
  await page.mouse.click(point.x, point.y);
  await expect(page.locator('#side-info')).toContainText('Cortical side selected');
  for (const shape of ['mirror', 'flat']) {
    await page.locator('#backing-shape').selectOption(shape);
    await page.locator('#generate').click();
    await expect(page.locator('#status-title')).toHaveText(
      'Closed shell · geometry checks passed',
      { timeout: 150000 },
    );
    await expect(page.locator('#export')).toBeEnabled();
    const report = await page.evaluate(() => window.fossilForge.state.result.report);
    expect(report.backing_shape).toBe(shape);
    expect(report.bridge_smoothing_mm).toBe(1);
    expect(report.core_surface_preserved).toBe(true);
    expect(report.changed_edge_band_max_mm).toBeLessThanOrEqual(0.4);
    expect(report.self_intersection_faces).toBe(0);
    if (shape === 'mirror') expect(report.source_surface_preserved).toBe(true);
    if (shape === 'mirror')
      await page.evaluate(() => {
        const { camera, controls, state } = window.fossilForge;
        const offset = camera.position.clone().sub(controls.target);
        offset.applyAxisAngle(camera.up.clone().normalize(), Math.PI * 0.38);
        const box = state.result.geometry.boundingBox;
        box.getCenter(controls.target);
        camera.position.copy(controls.target).add(offset.multiplyScalar(0.85));
        camera.lookAt(controls.target);
        controls.update();
      });
    await page.screenshot({ path: `outputs/${shape}-smooth-workbench.png`, fullPage: true });
    await page.locator('#export-format').selectOption('bundle');
    const pending = page.waitForEvent('download');
    await page.locator('#export').click();
    await (await pending).saveAs(`outputs/${shape}-smooth-ui-project.zip`);
  }
  await page.locator('#bridge-smoothing').fill('1.5');
  await expect(page.locator('#export')).toBeDisabled();
  expect(errors).toEqual([]);
});
