/* Keeps a pan/zoom camera from losing its content: shared by the pipeline diagram and the dependency (UML) dialog.
   A camera is { x, y, z } (translate in viewport px, then scale); content is the box { x0, y0, x1, y1 } in content px. */

export const PAN_MARGIN = 80; // px of empty space the content may be panned away from a viewport edge (content bigger than the viewport)

const clamp = (value, low, high) => Math.max(low, Math.min(high, value));

/** One axis, content [low, high] at zoom `zoom` in a viewport of `size` px: smaller than the viewport = centered,
    bigger = free to pan but each content edge stays at most PAN_MARGIN px inside the viewport edge. */
export function clampAxis(position, low, high, zoom, size, margin = PAN_MARGIN) {
  const extent = (high - low) * zoom;
  if (extent <= size) return (size - extent) / 2 - low * zoom;
  return clamp(position, size - margin - high * zoom, margin - low * zoom);
}

export const LOOSE_VISIBLE = 160; // px of content that must stay on screen on the loose (horizontal) axis

/** Loose axis: the content moves freely (it is not re-centered), as long as LOOSE_VISIBLE px of it stay inside the viewport. */
export function looseAxis(position, low, high, zoom, size, visible = LOOSE_VISIBLE) {
  const keep = Math.min(visible, (high - low) * zoom, size);
  return clamp(position, keep - high * zoom, size - keep - low * zoom);
}

/** The camera with the content kept in view: free sideways (loose horizontal axis), held and centered vertically.
    Unchanged while the viewport or the content is not measurable. */
export function clampCameraTo(camera, content, width, height) {
  if (!content || !width || !height) return camera;
  return { z: camera.z, x: looseAxis(camera.x, content.x0, content.x1, camera.z, width), y: clampAxis(camera.y, content.y0, content.y1, camera.z, height) };
}

/** Camera that shows all the content, centered on both axes, at the zoom that fits (between minZoom and maxZoom). */
export function fitCameraTo(content, width, height, { minZoom, maxZoom, marginX, marginY }) {
  const contentWidth = content.x1 - content.x0, contentHeight = content.y1 - content.y0;
  const zoom = clamp(Math.min((width - marginX) / contentWidth, (height - marginY) / contentHeight), minZoom, maxZoom);
  return { z: zoom, x: (width - contentWidth * zoom) / 2 - content.x0 * zoom, y: (height - contentHeight * zoom) / 2 - content.y0 * zoom };
}

export const sameCamera = (a, b) => Math.abs(a.x - b.x) < 0.5 && Math.abs(a.y - b.y) < 0.5 && Math.abs(a.z - b.z) < 0.001;
