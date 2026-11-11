/* Billkind landing hero: a bill floats in soft blue light, a small agent orb carries the payment from the sponsor
   to the bill, the bill is stamped PAID, and the orb routes the money on to the biller. It leans gently toward the
   cursor. Loaded lazily from home.html only when WebGL works; the page shows a still image until the first frame. */
import * as THREE from '/static/vendor/three-0.160.0/three.module.min.js';

const NAVY = '#0B1E5B', BLUE = '#1463FF', CYAN = '#22C7F2', GREEN = '#0E7A4E';
const LOOP = 8.0;
const SX = -2.6, SY = -1.75, BX = 2.6, BY = -1.85;   // sponsor and biller node centres

function canvasTex(w, h, draw) {
  const c = document.createElement('canvas'); c.width = w; c.height = h;
  draw(c.getContext('2d'), w, h);
  const t = new THREE.CanvasTexture(c);
  t.colorSpace = THREE.SRGBColorSpace; t.anisotropy = 4;
  return t;
}

function rr(g, x, y, w, h, r) {
  g.beginPath(); g.moveTo(x + r, y); g.arcTo(x + w, y, x + w, y + h, r); g.arcTo(x + w, y + h, x, y + h, r);
  g.arcTo(x, y + h, x, y, r); g.arcTo(x, y, x + w, y, r); g.closePath();
}

const FONT = '"Manrope", ui-sans-serif, system-ui, -apple-system, "Segoe UI", sans-serif';

function billFace() {
  return canvasTex(1024, 1400, (g, w, h) => {
    g.fillStyle = '#ffffff'; g.fillRect(0, 0, w, h);
    const head = g.createLinearGradient(0, 0, w, 0); head.addColorStop(0, NAVY); head.addColorStop(.6, BLUE); head.addColorStop(1, CYAN);
    g.fillStyle = head; g.fillRect(0, 0, w, 200);
    g.fillStyle = '#fff'; g.font = `800 58px ${FONT}`; g.fillText('Lakeside Power & Light', 70, 112);
    g.font = `600 32px ${FONT}`; g.globalAlpha = .85; g.fillText('Electricity  ·  Detroit, US', 72, 164); g.globalAlpha = 1;
    g.fillStyle = '#4A5A85'; g.font = `700 34px ${FONT}`; g.fillText('AMOUNT DUE', 72, 300);
    g.fillStyle = NAVY; g.font = `800 150px ${FONT}`; g.fillText('$84.20', 64, 450);
    g.fillStyle = '#E6F0FF'; rr(g, 72, 500, 360, 64, 32); g.fill();
    g.fillStyle = BLUE; g.font = `800 32px ${FONT}`; g.fillText('Due in 3 days', 104, 543);
    g.fillStyle = '#EDF3FF'; rr(g, 72, 610, w - 144, 150, 28); g.fill();
    g.fillStyle = '#4A5A85'; g.font = `800 26px ${FONT}`; g.fillText('PAID DIRECTLY TO', 104, 664);
    g.fillStyle = NAVY; g.font = `800 40px ${FONT}`; g.fillText('Lakeside Power & Light', 104, 722);
    g.fillStyle = '#D5E2F5';
    [[72, 830, 620], [72, 890, 760], [72, 950, 520]].forEach(([x, y, ww]) => { rr(g, x, y, ww, 24, 12); g.fill(); });
    g.fillStyle = '#4A5A85'; g.font = `700 32px ${FONT}`; g.fillText('Account  •••• 7788', 72, 1080);
    g.strokeStyle = '#B3C7E6'; g.setLineDash([18, 14]); g.lineWidth = 4; g.beginPath(); g.moveTo(40, 1150); g.lineTo(w - 40, 1150); g.stroke();
    g.setLineDash([]); g.fillStyle = BLUE; g.font = `800 30px ${FONT}`; g.fillText('Paid to the biller, never to the person', 72, 1230);
  });
}

function stampTex() {
  return canvasTex(640, 260, (g, w, h) => {
    g.strokeStyle = GREEN; g.lineWidth = 18; rr(g, 14, 14, w - 28, h - 28, 34); g.stroke();
    g.fillStyle = 'rgba(255,255,255,.55)'; rr(g, 24, 24, w - 48, h - 48, 28); g.fill();
    g.fillStyle = GREEN; g.font = `800 150px ${FONT}`; g.textAlign = 'center'; g.textBaseline = 'middle';
    g.fillText('PAID', w / 2 + 8, h / 2 + 8);
  });
}

function labelTex(text, sub) {
  return canvasTex(512, 160, (g, w, h) => {
    g.shadowColor = 'rgba(20,99,255,.25)'; g.shadowBlur = 24; g.shadowOffsetY = 8;
    g.fillStyle = 'rgba(255,255,255,.96)'; rr(g, 16, 16, w - 32, h - 40, 52); g.fill();
    g.shadowColor = 'transparent';
    g.fillStyle = NAVY; g.font = `800 50px ${FONT}`; g.textAlign = 'center'; g.fillText(text, w / 2, sub ? 78 : 96);
    if (sub) { g.fillStyle = '#4A5A85'; g.font = `700 30px ${FONT}`; g.fillText(sub, w / 2, 118); }
  });
}

function glowTex(color) {
  return canvasTex(256, 256, (g, w) => {
    const r = g.createRadialGradient(w / 2, w / 2, 0, w / 2, w / 2, w / 2);
    r.addColorStop(0, color); r.addColorStop(.35, color.replace(/,\s*[\d.]+\)$/, ',.35)')); r.addColorStop(1, 'rgba(0,0,0,0)');
    g.fillStyle = r; g.fillRect(0, 0, w, w);
  });
}

function billShape(W, H) {
  const s = new THREE.Shape(), x0 = -W / 2, y0 = -H / 2, fold = .42, teeth = 7, tw = W / teeth, td = .12;
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

const ease = t => t < .5 ? 4 * t * t * t : 1 - Math.pow(-2 * t + 2, 3) / 2;
const clamp01 = t => Math.max(0, Math.min(1, t));
const seg = (t, a, b) => clamp01((t - a) / (b - a));

export function mount(host, opts = {}) {
  const reduce = window.matchMedia('(prefers-reduced-motion: reduce)').matches || opts.still;
  const renderer = new THREE.WebGLRenderer({antialias: true, alpha: true, powerPreference: 'low-power', preserveDrawingBuffer: !!opts.still});
  renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, window.innerWidth < 700 ? 1.5 : 2));
  renderer.outputColorSpace = THREE.SRGBColorSpace;
  renderer.setClearColor(0x000000, 0);
  const canvas = renderer.domElement;
  canvas.className = 'hero3d-canvas'; canvas.setAttribute('aria-hidden', 'true');
  host.appendChild(canvas);

  const scene = new THREE.Scene();
  const camera = new THREE.PerspectiveCamera(32, 1, .1, 100);
  camera.position.set(0, .2, 11.5);

  scene.add(new THREE.HemisphereLight(0xEAF2FF, 0xFFFFFF, 1.6));
  const key = new THREE.DirectionalLight(0xffffff, 1.4); key.position.set(3, 5, 8); scene.add(key);
  const rim = new THREE.PointLight(0x22C7F2, 6, 12, 1.6); rim.position.set(-2, 1, 2.5); scene.add(rim);

  const world = new THREE.Group(); scene.add(world);

  // Soft blue light behind the bill.
  const halo = new THREE.Mesh(new THREE.PlaneGeometry(9, 9), new THREE.MeshBasicMaterial({
    map: glowTex('rgba(34,199,242,.55)'), transparent: true, depthWrite: false, blending: THREE.AdditiveBlending, opacity: .55}));
  halo.position.set(0, .2, -1.6); world.add(halo);
  const halo2 = new THREE.Mesh(new THREE.PlaneGeometry(7, 7), new THREE.MeshBasicMaterial({
    map: glowTex('rgba(20,99,255,.6)'), transparent: true, depthWrite: false, opacity: .22}));
  halo2.position.set(.8, -.6, -1.8); world.add(halo2);

  // The bill.
  const W = 2.45, H = 3.35;
  const shape = billShape(W, H);
  const bill = new THREE.Group();
  const body = new THREE.Mesh(new THREE.ExtrudeGeometry(shape, {depth: .05, bevelEnabled: true, bevelThickness: .015, bevelSize: .015, bevelSegments: 2}),
    new THREE.MeshStandardMaterial({color: 0xF4F8FF, roughness: .55, metalness: 0}));
  body.position.z = -.05;
  const faceGeo = new THREE.ShapeGeometry(shape); remapUV(faceGeo, W, H);
  const faceMat = new THREE.MeshStandardMaterial({map: billFace(), roughness: .6, emissive: new THREE.Color(0x1463FF), emissiveIntensity: 0});
  const face = new THREE.Mesh(faceGeo, faceMat); face.position.z = .017;
  const flap = new THREE.Mesh(new THREE.ShapeGeometry(new THREE.Shape([new THREE.Vector2(W / 2 - .42, H / 2), new THREE.Vector2(W / 2 - .42, H / 2 - .42), new THREE.Vector2(W / 2, H / 2 - .42)])),
    new THREE.MeshStandardMaterial({color: 0x9EE3FA, roughness: .5, side: THREE.DoubleSide}));
  flap.position.z = .03;
  bill.add(body, face, flap);
  const stamp = new THREE.Mesh(new THREE.PlaneGeometry(1.9, .77), new THREE.MeshBasicMaterial({map: stampTex(), transparent: true, opacity: 0, depthWrite: false}));
  stamp.position.set(.35, .35, .06); stamp.rotation.z = .2; bill.add(stamp);
  bill.position.set(0, .45, 0); bill.rotation.set(-.08, -.22, .05);
  world.add(bill);

  // Sponsor and biller nodes.
  function node(x, y, color, text, sub) {
    const g = new THREE.Group();
    const disc = new THREE.Mesh(new THREE.CylinderGeometry(.42, .42, .14, 48), new THREE.MeshStandardMaterial({color: 0xffffff, roughness: .35, emissive: new THREE.Color(color), emissiveIntensity: .08}));
    disc.rotation.x = Math.PI / 2;
    const ring = new THREE.Mesh(new THREE.TorusGeometry(.5, .035, 12, 64), new THREE.MeshStandardMaterial({color, emissive: new THREE.Color(color), emissiveIntensity: .6, roughness: .3}));
    const glow = new THREE.Sprite(new THREE.SpriteMaterial({map: glowTex('rgba(34,199,242,.9)'), transparent: true, opacity: .0, depthWrite: false, blending: THREE.AdditiveBlending}));
    glow.scale.set(2.4, 2.4, 1);
    const label = new THREE.Sprite(new THREE.SpriteMaterial({map: labelTex(text, sub), transparent: true, depthWrite: false}));
    label.scale.set(1.6, .5, 1); label.position.set(0, -.92, 0); label.renderOrder = 10; label.material.depthTest = false;
    g.add(glow, disc, ring, label); g.position.set(x, y, .3);
    g.userData = {disc, ring, glow};
    world.add(g);
    return g;
  }
  const sponsor = node(SX, SY, 0x1463FF, 'Sponsor', 'pays with PayPal');
  const biller = node(BX, BY, 0x22C7F2, 'Biller', 'receives the payout');
  // Icons on the nodes: a heart for the sponsor, a small building for the biller.
  const iconMat = new THREE.MeshStandardMaterial({color: 0x1463FF, roughness: .4});
  const heart = new THREE.Shape(); heart.moveTo(0, -.16); heart.bezierCurveTo(-.3, .02, -.16, .26, 0, .1); heart.bezierCurveTo(.16, .26, .3, .02, 0, -.16);
  const heartMesh = new THREE.Mesh(new THREE.ExtrudeGeometry(heart, {depth: .04, bevelEnabled: false}), iconMat); heartMesh.position.z = .08; sponsor.add(heartMesh);
  const bmat = new THREE.MeshStandardMaterial({color: 0x0B1E5B, roughness: .4});
  [[-.12, .12], [0, .2], [.12, .12]].forEach(([x, h]) => { const m = new THREE.Mesh(new THREE.BoxGeometry(.09, h, .05), bmat); m.position.set(x, -.12 + h / 2, .1); biller.add(m); });
  const roof = new THREE.Mesh(new THREE.BoxGeometry(.38, .04, .05), bmat); roof.position.set(0, .14, .1); biller.add(roof);

  // Payment routes: sponsor to bill, bill to biller.
  const A = new THREE.QuadraticBezierCurve3(new THREE.Vector3(SX, SY + .4, .4), new THREE.Vector3(-2.6, 1.7, 1.2), new THREE.Vector3(-.25, 1.1, .5));
  const B = new THREE.QuadraticBezierCurve3(new THREE.Vector3(.4, -.1, .5), new THREE.Vector3(2.7, 1.0, 1.3), new THREE.Vector3(BX, BY + .4, .4));
  const routeMat = new THREE.MeshBasicMaterial({color: 0x22C7F2, transparent: true, opacity: .35, depthWrite: false});
  [A, B].forEach(c => world.add(new THREE.Mesh(new THREE.TubeGeometry(c, 64, .012, 6, false), routeMat)));
  const dots = new THREE.Group(); world.add(dots);
  const dotMat = new THREE.MeshBasicMaterial({color: 0x1463FF, transparent: true, opacity: .55});
  const dotGeo = new THREE.SphereGeometry(.03, 8, 8);
  for (let i = 0; i < 14; i++) dots.add(new THREE.Mesh(dotGeo, dotMat));

  // The agent orb.
  const orb = new THREE.Group();
  const core = new THREE.Mesh(new THREE.SphereGeometry(.2, 32, 32), new THREE.MeshStandardMaterial({color: 0xffffff, emissive: new THREE.Color(0x22C7F2), emissiveIntensity: 1.1, roughness: .2}));
  const shell = new THREE.Mesh(new THREE.TorusGeometry(.34, .022, 10, 64), new THREE.MeshStandardMaterial({color: 0x1463FF, emissive: new THREE.Color(0x1463FF), emissiveIntensity: .7}));
  const orbGlow = new THREE.Sprite(new THREE.SpriteMaterial({map: glowTex('rgba(34,199,242,.95)'), transparent: true, depthWrite: false, blending: THREE.AdditiveBlending, opacity: .9}));
  orbGlow.scale.set(1.6, 1.6, 1);
  const orbLabel = new THREE.Sprite(new THREE.SpriteMaterial({map: labelTex('Giving agent'), transparent: true, depthWrite: false, opacity: .95}));
  orbLabel.scale.set(1.35, .42, 1); orbLabel.position.set(0, .62, 0); orbLabel.renderOrder = 11; orbLabel.material.depthTest = false;
  orb.add(orbGlow, core, shell, orbLabel); world.add(orb);
  const orbLight = new THREE.PointLight(0x22C7F2, 4, 4, 2); orb.add(orbLight);

  // Floating specks of light.
  const N = 90, pos = new Float32Array(N * 3), seeds = [];
  for (let i = 0; i < N; i++) { pos[i * 3] = (Math.random() - .5) * 11; pos[i * 3 + 1] = (Math.random() - .5) * 6; pos[i * 3 + 2] = (Math.random() - .5) * 3 - .5; seeds.push(Math.random() * 6.28); }
  const specGeo = new THREE.BufferGeometry(); specGeo.setAttribute('position', new THREE.BufferAttribute(pos, 3));
  const specks = new THREE.Points(specGeo, new THREE.PointsMaterial({size: .05, color: 0x5B8CFF, transparent: true, opacity: .55, depthWrite: false}));
  world.add(specks);

  // Pointer: lean gently toward the cursor.
  const target = {x: 0, y: 0}, lean = {x: 0, y: 0};
  function onMove(e) {
    const r = host.getBoundingClientRect();
    target.x = ((e.clientX - r.left) / r.width - .5) * 2; target.y = ((e.clientY - r.top) / r.height - .5) * 2;
  }
  if (!reduce) window.addEventListener('pointermove', onMove, {passive: true});

  function resize() {
    const w = host.clientWidth, h = host.clientHeight;
    if (!w || !h) return;
    renderer.setSize(w, h, false);
    camera.aspect = w / h;
    // Keep the whole scene in frame on narrow screens.
    // Fit the whole scene (about 3.5 units each side of centre, 2.9 up and down) at any aspect ratio.
    const tan = Math.tan(THREE.MathUtils.degToRad(camera.fov / 2));
    camera.position.z = Math.max(2.95 / tan, 3.85 / (tan * camera.aspect));
    camera.updateProjectionMatrix();
  }
  resize();
  const ro = new ResizeObserver(resize); ro.observe(host);

  const tmp = new THREE.Vector3();
  function frame(t) {
    const p = t % LOOP;
    // Bill float.
    bill.position.y = .45 + Math.sin(t * 1.1) * .08;
    bill.rotation.z = .05 + Math.sin(t * .7) * .02;
    // Orb path: idle at sponsor, fly to the bill, wait for the stamp, fly to the biller, rest, return.
    const toBill = ease(seg(p, 1.0, 2.5)), toBiller = ease(seg(p, 3.4, 4.9)), back = ease(seg(p, 7.0, 7.9));
    if (p < 1.0 || p >= 7.9) {
      orb.position.set(SX + Math.cos(t * 2) * .5, SY + .95 + Math.sin(t * 2) * .18, .6);
    } else if (p < 2.5) A.getPoint(toBill, orb.position);
    else if (p < 3.4) { A.getPoint(1, orb.position); orb.position.y += Math.sin((p - 2.5) * 7) * .05; }
    else if (p < 4.9) B.getPoint(toBiller, orb.position);
    else if (p < 7.0) { orb.position.set(BX + Math.cos(t * 2) * .5, BY + .95 + Math.sin(t * 2) * .18, .6); }
    else { tmp.set(BX, BY + .95, .6).lerp(new THREE.Vector3(SX, SY + .95, .6), back); orb.position.copy(tmp); orb.position.y += Math.sin(back * Math.PI) * 1.6; }
    shell.rotation.x = t * 1.6; shell.rotation.y = t * 1.1;
    // Bill lights up while the agent is at it; the stamp lands at 3.0s and lifts away near the end.
    faceMat.emissiveIntensity = .14 * Math.sin(Math.PI * seg(p, 2.3, 3.4));
    const sIn = seg(p, 2.95, 3.25), sOut = seg(p, 7.0, 7.6);
    stamp.material.opacity = sIn * (1 - sOut);
    const k = 1 + (1 - ease(sIn)) * 1.4;
    stamp.scale.set(k, k, 1);
    bill.position.z = p > 3.05 && p < 3.35 ? -Math.sin((p - 3.05) / .3 * Math.PI) * .12 : 0;
    // Nodes glow when money leaves (sponsor) and arrives (biller).
    sponsor.userData.glow.material.opacity = .55 * Math.sin(Math.PI * seg(p, .6, 1.6));
    biller.userData.glow.material.opacity = .75 * Math.sin(Math.PI * seg(p, 4.6, 6.6));
    biller.userData.disc.material.emissiveIntensity = .08 + .5 * Math.sin(Math.PI * seg(p, 4.6, 6.6));
    // Dots flow along the active route.
    dots.children.forEach((d, i) => {
      const u = ((t * .35) + i / dots.children.length) % 1;
      (i % 2 ? B : A).getPoint(u, d.position);
    });
    const arr = specGeo.attributes.position.array;
    for (let i = 0; i < N; i++) arr[i * 3 + 1] += Math.sin(t * .6 + seeds[i]) * .0015;
    specGeo.attributes.position.needsUpdate = true;
    // Lean.
    lean.x += (target.x - lean.x) * .05; lean.y += (target.y - lean.y) * .05;
    world.rotation.y = lean.x * .16; world.rotation.x = lean.y * .08;
    bill.rotation.y = -.22 + lean.x * .18;
    renderer.render(scene, camera);
  }

  if (reduce) {
    frame(5.6);
    host.classList.add('ready');
    return {stop() {}};
  }

  let running = false, raf = 0, last = performance.now(), clock = 0, first = true;
  function loop(now) {
    if (!running) return;
    clock += Math.min(.05, (now - last) / 1000); last = now;
    frame(clock);
    if (first) { first = false; host.classList.add('ready'); }
    raf = requestAnimationFrame(loop);
  }
  function play() { if (running) return; running = true; last = performance.now(); raf = requestAnimationFrame(loop); }
  function pause() { running = false; cancelAnimationFrame(raf); }
  const io = new IntersectionObserver(es => es.forEach(e => (e.isIntersecting && !document.hidden ? play() : pause())), {threshold: .05});
  io.observe(host);
  document.addEventListener('visibilitychange', () => (document.hidden ? pause() : play()));
  return {stop() { pause(); io.disconnect(); ro.disconnect(); }};
}
