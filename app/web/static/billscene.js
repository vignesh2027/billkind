/* The stamped bill, shared by the landing hero (hero3d.js) and the AR model export (scripts/ar_export.html),
   so the bill you see on the page is the same one you place on your desk. Paths are relative so the module
   also loads outside the app. */
import * as THREE from './vendor/three-0.160.0/three.module.min.js';

export const NAVY = '#0B1E5B', BLUE = '#1463FF', CYAN = '#22C7F2', GREEN = '#0E7A4E';
export const FONT = '"Manrope", ui-sans-serif, system-ui, -apple-system, "Segoe UI", sans-serif';

export function canvasTex(w, h, draw) {
  const c = document.createElement('canvas'); c.width = w; c.height = h;
  draw(c.getContext('2d'), w, h);
  const t = new THREE.CanvasTexture(c);
  t.colorSpace = THREE.SRGBColorSpace; t.anisotropy = 4;
  return t;
}

export function rr(g, x, y, w, h, r) {
  g.beginPath(); g.moveTo(x + r, y); g.arcTo(x + w, y, x + w, y + h, r); g.arcTo(x + w, y + h, x, y + h, r);
  g.arcTo(x, y + h, x, y, r); g.arcTo(x, y, x + w, y, r); g.closePath();
}

/* A fictional electricity bill. */
export function billFace() {
  const t = canvasTex(1024, 1400, (g, w, h) => {
    g.fillStyle = '#ffffff'; g.fillRect(0, 0, w, h);
    const head = g.createLinearGradient(0, 0, w, 0); head.addColorStop(0, NAVY); head.addColorStop(.6, BLUE); head.addColorStop(1, CYAN);
    g.fillStyle = head; g.fillRect(0, 0, w, 200);
    g.fillStyle = '#fff'; g.font = `800 58px ${FONT}`; g.fillText('Lakeside Power & Light', 70, 112);
    g.font = `600 32px ${FONT}`; g.globalAlpha = .85; g.fillText('Electricity  ·  Detroit, US', 72, 164); g.globalAlpha = 1;
    g.fillStyle = '#4A5A85'; g.font = `700 34px ${FONT}`; g.fillText('AMOUNT DUE', 72, 300);
    g.fillStyle = NAVY; g.font = `800 150px ${FONT}`; g.fillText('$84.20', 64, 450);
    g.fillStyle = '#E6F0FF'; rr(g, 72, 500, 360, 64, 32); g.fill();
    g.fillStyle = BLUE; g.font = `800 32px ${FONT}`; g.fillText('Due Nov 18', 104, 543);
    g.fillStyle = '#EDF3FF'; rr(g, 72, 610, w - 144, 150, 28); g.fill();
    g.fillStyle = '#4A5A85'; g.font = `800 26px ${FONT}`; g.fillText('PAY TO', 104, 664);
    g.fillStyle = NAVY; g.font = `800 40px ${FONT}`; g.fillText('Lakeside Power & Light', 104, 722);
    g.fillStyle = '#D5E2F5';
    [[72, 830, 620], [72, 890, 760], [72, 950, 520]].forEach(([x, y, ww]) => { rr(g, x, y, ww, 24, 12); g.fill(); });
    g.fillStyle = '#4A5A85'; g.font = `700 32px ${FONT}`; g.fillText('Account  •••• 7788', 72, 1080);
    g.strokeStyle = '#B3C7E6'; g.setLineDash([18, 14]); g.lineWidth = 4; g.beginPath(); g.moveTo(40, 1150); g.lineTo(w - 40, 1150); g.stroke();
    g.setLineDash([]); g.fillStyle = BLUE; g.font = `800 30px ${FONT}`; g.fillText('Paid to the biller, never to the person', 72, 1230);
    g.fillStyle = '#8A98BD'; g.font = `600 24px ${FONT}`; g.fillText('Fictional sample bill', 72, 1300);
  });
  t.userData.mimeType = 'image/jpeg';
  return t;
}

export function stampTex() {
  return canvasTex(640, 260, (g, w, h) => {
    g.strokeStyle = GREEN; g.lineWidth = 18; rr(g, 14, 14, w - 28, h - 28, 34); g.stroke();
    g.fillStyle = 'rgba(255,255,255,.55)'; rr(g, 24, 24, w - 48, h - 48, 28); g.fill();
    g.fillStyle = GREEN; g.font = `800 150px ${FONT}`; g.textAlign = 'center'; g.textBaseline = 'middle';
    g.fillText('PAID', w / 2 + 8, h / 2 + 8);
  });
}

/* Bill outline: a folded top-right corner and a torn, toothed bottom edge. */
export function billShape(W, H) {
  const s = new THREE.Shape(), x0 = -W / 2, y0 = -H / 2, fold = W * .17, teeth = 7, tw = W / teeth, td = H * .036;
  s.moveTo(x0, H / 2);
  s.lineTo(W / 2 - fold, H / 2);
  s.lineTo(W / 2, H / 2 - fold);
  s.lineTo(W / 2, y0 + td);
  for (let i = 0; i < teeth; i++) {
    const xr = W / 2 - i * tw;
    s.lineTo(xr - tw / 2, y0);
    s.lineTo(xr - tw, y0 + td);
  }
  s.lineTo(x0, H / 2);
  return s;
}

function remapUV(geo, W, H) {
  const p = geo.attributes.position, uv = geo.attributes.uv;
  for (let i = 0; i < p.count; i++) uv.setXY(i, (p.getX(i) + W / 2) / W, (p.getY(i) + H / 2) / H);
  uv.needsUpdate = true;
}

/* Build the bill. W and H in scene units; depth is the paper thickness. Returns {group, faceMat, stamp}.
   The stamp is a MeshStandardMaterial with an alpha map so it exports to glTF and USDZ alike. */
export function makeBill({W = 2.45, H = 3.35, depth = .05, stamped = false} = {}) {
  const shape = billShape(W, H);
  const group = new THREE.Group(); group.name = 'Bill';
  const body = new THREE.Mesh(new THREE.ExtrudeGeometry(shape, {depth, bevelEnabled: false}),
    new THREE.MeshStandardMaterial({color: 0xF4F8FF, roughness: .7, metalness: 0}));
  body.name = 'Paper'; body.position.z = -depth;
  const faceGeo = new THREE.ShapeGeometry(shape); remapUV(faceGeo, W, H);
  const faceMat = new THREE.MeshStandardMaterial({map: billFace(), roughness: .62, metalness: 0, emissive: new THREE.Color(0x1463FF), emissiveIntensity: 0});
  const face = new THREE.Mesh(faceGeo, faceMat); face.name = 'Face'; face.position.z = depth * .02 + .001;
  const fold = W * .17;
  const flapGeo = new THREE.BufferGeometry().setFromPoints([new THREE.Vector3(W / 2 - fold, H / 2, 0), new THREE.Vector3(W / 2 - fold, H / 2 - fold, 0), new THREE.Vector3(W / 2, H / 2 - fold, 0)]);
  flapGeo.computeVertexNormals();
  const flap = new THREE.Mesh(flapGeo, new THREE.MeshStandardMaterial({color: 0x9EE3FA, roughness: .5, side: THREE.DoubleSide}));
  flap.name = 'Fold'; flap.position.z = depth * .3 + .002;
  group.add(body, face, flap);
  const st = stampTex();
  const stamp = new THREE.Mesh(new THREE.PlaneGeometry(W * .78, W * .78 * 260 / 640),
    new THREE.MeshStandardMaterial({map: st, transparent: true, roughness: .8, opacity: stamped ? 1 : 0, depthWrite: false}));
  stamp.name = 'PaidStamp';
  stamp.position.set(W * .14, H * .1, depth * .6 + .004); stamp.rotation.z = .2;
  group.add(stamp);
  return {group, faceMat, stamp};
}
