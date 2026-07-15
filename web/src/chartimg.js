// Rasterize a chart's SVG to a PNG data URL so it can be pasted as an <img>
// into Word/Outlook/Docs — those choke on Recharts' live SVG + wrapper divs.
// The SVG is self-contained (colors passed as attributes, no external fonts or
// images), so drawing it to a canvas doesn't taint it.
export async function svgToPngDataUrl(svg, { scale = 2, background = "#ffffff" } = {}) {
  const rect = svg.getBoundingClientRect();
  const w = Math.round(svg.clientWidth || rect.width);
  const h = Math.round(svg.clientHeight || rect.height);
  if (!w || !h) return null;

  const clone = svg.cloneNode(true);
  clone.setAttribute("xmlns", "http://www.w3.org/2000/svg");
  clone.setAttribute("width", String(w));
  clone.setAttribute("height", String(h));
  const xml = new XMLSerializer().serializeToString(clone);
  const src = "data:image/svg+xml;charset=utf-8," + encodeURIComponent(xml);

  const img = new Image();
  img.width = w;
  img.height = h;
  await new Promise((resolve, reject) => {
    img.onload = resolve;
    img.onerror = reject;
    img.src = src;
  });

  const canvas = document.createElement("canvas");
  canvas.width = w * scale;
  canvas.height = h * scale;
  const ctx = canvas.getContext("2d");
  ctx.scale(scale, scale);
  ctx.fillStyle = background; // solid bg so it doesn't paste on transparent
  ctx.fillRect(0, 0, w, h);
  ctx.drawImage(img, 0, 0, w, h);
  return { url: canvas.toDataURL("image/png"), w, h };
}
