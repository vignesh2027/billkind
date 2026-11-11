/* Billkind landing hero, the real flow as one scene: a bill drops onto a glass reader, a beam of light reads it
   and the fields it found slide out beside it, a sponsor's PayPal payment travels from the sponsor's card to the
   biller, and the bill is stamped PAID. Glass and light only. Loaded lazily from home.html when WebGL works;
   the page shows a still image until the first frame. */
import * as THREE from '/static/vendor/three-0.160.0/three.module.min.js';
import {makeBill, canvasTex, rr, FONT, NAVY, BLUE} from '/static/billscene.js?v=2';

const LOOP = 10.0;
const ease = t => t < .5 ? 4 * t * t * t : 1 - Math.pow(-2 * t + 2, 3) / 2;
const easeOut = t => 1 - Math.pow(1 - t, 3);
const backOut = t => { const c = 1.4; return 1 + (c + 1) * Math.pow(t - 1, 3) + c * Math.pow(t - 1, 2); };
const clamp01 = t => Math.max(0, Math.min(1, t));
const seg = (t, a, b) => clamp01((t - a) / (b - a));
const bell = (t, a, b) => Math.sin(Math.PI * seg(t, a, b));

/* A soft studio for reflections: a pale blue room with three light panels, baked once into an environment map. */
function studio(renderer) {
  const room = new THREE.Scene();
  const wall = canvasTex(64, 256, (g, w, h) => {
    const gr = g.createLinearGradient(0, 0, 0, h); gr.addColorStop(0, '#FFFFFF'); gr.addColorStop(.55, '#DCE9FF'); gr.addColorStop(1, '#9DBCF5');
    g.fillStyle = gr; g.fillRect(0, 0, w, h);
  });
  room.add(new THREE.Mesh(new THREE.BoxGeometry(20, 14, 20), new THREE.MeshBasicMaterial({map: wall, side: THREE.BackSide})));
  const panel = (x, y, z, w, h, c, ry) => {
    const m = new THREE.Mesh(new THREE.PlaneGeometry(w, h), new THREE.MeshBasicMaterial({color: c}));
    m.position.set(x, y, z); m.lookAt(0, 0, 0); if (ry) m.rotation.z = ry; room.add(m);
  };
  panel(0, 6.5, 2, 9, 3, 0xffffff);
  panel(-9, 1, 3, 4, 7, 0xE8F4FF);
  panel(8, 2, -4, 3, 6, 0x9FE6FF);
  const pm = new THREE.PMREMGenerator(renderer);
  const env = pm.fromScene(room, .03).texture;
  pm.dispose();
  return env;
}

function glassMat(opts = {}) {
  return new THREE.MeshPhysicalMaterial(Object.assign({
    color: 0xF2F7FF, metalness: 0, roughness: .14, transmission: 1, thickness: .35, ior: 1.42,
    clearcoat: 1, clearcoatRoughness: .08, attenuationColor: new THREE.Color(0xBFD8FF), attenuationDistance: 2.5,
    envMapIntensity: 1.1, specularIntensity: 1}, opts));
}

function roundedSlab(w, h, d, r) {
  const s = new THREE.Shape(), x = -w / 2, y = -h / 2;
  s.moveTo(x + r, y); s.lineTo(x + w - r, y); s.quadraticCurveTo(x + w, y, x + w, y + r); s.lineTo(x + w, y + h - r);
  s.quadraticCurveTo(x + w, y + h, x + w - r, y + h); s.lineTo(x + r, y + h); s.quadraticCurveTo(x, y + h, x, y + h - r);
  s.lineTo(x, y + r); s.quadraticCurveTo(x, y, x + r, y);
  const g = new THREE.ExtrudeGeometry(s, {depth: d, bevelEnabled: true, bevelThickness: d * .35, bevelSize: Math.min(r * .5, d * .4), bevelSegments: 4, curveSegments: 10});
  g.translate(0, 0, -d / 2);
  return g;
}

/* Frosted label: title and an optional second line, drawn at 2x for crisp text. */
function chipTex(title, sub, accent) {
  return canvasTex(720, sub ? 200 : 140, (g, w, h) => {
    g.shadowColor = 'rgba(20,99,255,.18)'; g.shadowBlur = 26; g.shadowOffsetY = 8;
    g.fillStyle = 'rgba(255,255,255,.93)'; rr(g, 20, 16, w - 40, h - 40, (h - 40) / 2.4); g.fill();
    g.shadowColor = 'transparent';
    g.strokeStyle = 'rgba(20,99,255,.18)'; g.lineWidth = 2; rr(g, 20, 16, w - 40, h - 40, (h - 40) / 2.4); g.stroke();
    g.fillStyle = accent || BLUE; g.beginPath(); g.arc(66, sub ? 82 : 76, 10, 0, 7); g.fill();
    g.fillStyle = NAVY; g.font = `800 44px ${FONT}`; g.fillText(title, 96, sub ? 96 : 92);
    if (sub) { g.fillStyle = '#4A5A85'; g.font = `600 32px ${FONT}`; g.fillText(sub, 96, 144); }
  });
}

function glowTex(color) {
  return canvasTex(256, 256, (g, w) => {
    const r = g.createRadialGradient(w / 2, w / 2, 0, w / 2, w / 2, w / 2);
    r.addColorStop(0, color); r.addColorStop(.4, color.replace(/,\s*[\d.]+\)$/, ',.3)')); r.addColorStop(1, color.replace(/,\s*[\d.]+\)$/, ',0)'));
    g.fillStyle = r; g.fillRect(0, 0, w, w);
  });
}

function sprite(tex, w, h) {
  const s = new THREE.Sprite(new THREE.SpriteMaterial({map: tex, transparent: true, depthWrite: false, depthTest: false, toneMapped: false}));
  s.scale.set(w, h, 1); s.renderOrder = 20;
  return s;
}

export function mount(host, opts = {}) {
  const reduce = window.matchMedia('(prefers-reduced-motion: reduce)').matches || opts.still;
  const small = window.innerWidth < 700;
  const renderer = new THREE.WebGLRenderer({antialias: true, alpha: true, powerPreference: 'high-performance', preserveDrawingBuffer: !!opts.still});
  renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, small ? 1.5 : 2));
  renderer.outputColorSpace = THREE.SRGBColorSpace;
  renderer.toneMapping = THREE.NoToneMapping;
  renderer.setClearColor(0x000000, 0);
  const canvas = renderer.domElement;
  canvas.className = 'hero3d-canvas'; canvas.setAttribute('aria-hidden', 'true');
  host.appendChild(canvas);

  const scene = new THREE.Scene();
  scene.environment = studio(renderer);
  const camera = new THREE.PerspectiveCamera(30, 1, .1, 100);
  camera.position.set(0, .3, 12);

  scene.add(new THREE.HemisphereLight(0xF3F8FF, 0xC9DBFF, .75));
  const key = new THREE.DirectionalLight(0xffffff, 1.15); key.position.set(3, 6, 8); scene.add(key);
  const rim = new THREE.PointLight(0x22C7F2, 8, 10, 1.6); rim.position.set(-2.5, 1.5, 2); scene.add(rim);

  const world = new THREE.Group(); scene.add(world);

  // Light behind everything.
  const halo = new THREE.Mesh(new THREE.PlaneGeometry(11, 9), new THREE.MeshBasicMaterial({
    map: glowTex('rgba(80,170,255,.55)'), transparent: true, depthWrite: false, opacity: .5}));
  halo.position.set(.2, .2, -2.4); world.add(halo);

  // The glass reader the bill lands on.
  const reader = new THREE.Group(); world.add(reader);
  const slab = new THREE.Mesh(roundedSlab(3.15, 4.05, .14, .32), glassMat({color: 0xC9DDFF, roughness: .18, thickness: .6, attenuationColor: new THREE.Color(0x6E9BFF), attenuationDistance: 1.2}));
  reader.add(slab);
  const edge = new THREE.Mesh(new THREE.PlaneGeometry(3.0, .035), new THREE.MeshBasicMaterial({color: 0x7FD8F7, transparent: true, opacity: .8}));
  edge.position.set(0, -2.02, .12); reader.add(edge);
  reader.position.set(-.1, .35, -.42); reader.rotation.set(-.05, -.2, .03);

  // The bill.
  const W = 2.3, H = 3.15;
  const {group: bill, faceMat, stamp} = makeBill({W, H, depth: .035});
  faceMat.envMapIntensity = .35;
  const billHolder = new THREE.Group(); billHolder.add(bill); world.add(billHolder);
  billHolder.position.set(-.1, .35, -.2); billHolder.rotation.set(-.05, -.2, .03);

  // Scan beam: a bright line with a soft glow band, moving down the bill.
  const beam = new THREE.Group();
  const beamLine = new THREE.Mesh(new THREE.PlaneGeometry(W * 1.12, .03), new THREE.MeshBasicMaterial({color: 0xBDF3FF, transparent: true, depthWrite: false, blending: THREE.AdditiveBlending}));
  const beamGlow = new THREE.Mesh(new THREE.PlaneGeometry(W * 1.2, .55), new THREE.MeshBasicMaterial({map: canvasTex(8, 128, (g, w, h) => {
    const gr = g.createLinearGradient(0, 0, 0, h); gr.addColorStop(0, 'rgba(34,199,242,0)'); gr.addColorStop(.5, 'rgba(34,199,242,.75)'); gr.addColorStop(1, 'rgba(34,199,242,0)');
    g.fillStyle = gr; g.fillRect(0, 0, w, h); }), transparent: true, depthWrite: false, blending: THREE.AdditiveBlending}));
  beam.add(beamGlow, beamLine); beam.position.z = .06; bill.add(beam);

  // What the reader found, sliding out beside the bill.
  const chips = [
    chipTex('Biller verified', 'Lakeside Power & Light', '#0E7A4E'),
    chipTex('84.20 USD', 'two readings agree'),
    chipTex('Due Nov 18', 'account •••• 7788'),
  ].map((t, i) => { const s = sprite(t, 1.95, .54); s.userData.y = 1.5 - i * .66; world.add(s); return s; });

  // Sponsor: a glass card. Biller: a glass block.
  const card = new THREE.Group(); world.add(card);
  const cardGlass = new THREE.Mesh(roundedSlab(1.75, 1.1, .07, .14), glassMat({color: 0xE6F0FF, roughness: .1, attenuationColor: new THREE.Color(0x7FB0FF)}));
  const cardFace = new THREE.Mesh(new THREE.PlaneGeometry(1.75, 1.1), new THREE.MeshBasicMaterial({transparent: true, depthWrite: false, map: canvasTex(700, 440, (g, w, h) => {
    g.fillStyle = 'rgba(255,255,255,.0)'; g.fillRect(0, 0, w, h);
    g.fillStyle = 'rgba(11,30,91,.9)'; g.font = `800 52px ${FONT}`; g.fillText('Sponsor', 52, 110);
    g.fillStyle = 'rgba(74,90,133,.95)'; g.font = `700 32px ${FONT}`; g.fillText('Pays with PayPal', 52, 156);
    g.fillStyle = 'rgba(20,99,255,.85)'; rr(g, 52, 300, 150, 18, 9); g.fill(); rr(g, 220, 300, 90, 18, 9); g.fill();
    g.fillStyle = 'rgba(11,30,91,.25)'; rr(g, 52, 340, 260, 14, 7); g.fill();
  })}));
  cardFace.position.z = .07; card.add(cardGlass, cardFace);
  card.position.set(-2.75, -1.55, .55); card.rotation.set(-.12, .5, .08);
  const cardGlow = sprite(glowTex('rgba(34,199,242,.9)'), 3, 3); cardGlow.material.opacity = 0; cardGlow.material.blending = THREE.AdditiveBlending; cardGlow.renderOrder = 1; card.add(cardGlow);

  const biller = new THREE.Group(); world.add(biller);
  const block = new THREE.Mesh(roundedSlab(1.8, 1.25, .3, .16), glassMat({color: 0xDDF6FF, attenuationColor: new THREE.Color(0x6FD6F5), roughness: .12, thickness: .6}));
  const blockFace = new THREE.Mesh(new THREE.PlaneGeometry(1.8, 1.25), new THREE.MeshBasicMaterial({transparent: true, depthWrite: false, map: canvasTex(720, 500, (g, w, h) => {
    g.fillStyle = 'rgba(11,30,91,.9)'; g.font = `800 48px ${FONT}`; g.fillText('Lakeside Power', 52, 110); g.fillText('& Light', 52, 166);
    g.fillStyle = '#0E7A4E'; g.font = `800 32px ${FONT}`; g.fillText('Verified biller', 52, 230);
    g.fillStyle = 'rgba(74,90,133,.95)'; g.font = `700 30px ${FONT}`; g.fillText('Gets the PayPal payout', 52, 404);
  })}));
  blockFace.position.z = .27;
  const core = new THREE.Mesh(new THREE.PlaneGeometry(1.5, .035), new THREE.MeshBasicMaterial({color: 0x22C7F2, transparent: true, opacity: .5}));
  core.position.set(0, -.12, .28);
  biller.add(block, blockFace, core);
  biller.position.set(2.65, -1.8, .3); biller.rotation.set(-.08, -.5, -.05);
  const billerGlow = sprite(glowTex('rgba(34,199,242,.95)'), 3.4, 3.4); billerGlow.material.opacity = 0; billerGlow.material.blending = THREE.AdditiveBlending; billerGlow.renderOrder = 1;
  billerGlow.position.copy(biller.position); world.add(billerGlow);

  // The payment: a bright pill of light with a trail, sponsor card to biller, passing in front of the bill.
  const path = new THREE.CatmullRomCurve3([
    new THREE.Vector3(-2.55, -1.2, .9), new THREE.Vector3(-1.6, -.2, 1.7), new THREE.Vector3(0, -1.75, 2.0),
    new THREE.Vector3(1.6, -.6, 1.6), new THREE.Vector3(2.6, -1.25, 1.0)]);
  const SEGS = 160;
  const trailGeo = new THREE.TubeGeometry(path, SEGS, .018, 8, false);
  const trail = new THREE.Mesh(trailGeo, new THREE.MeshBasicMaterial({color: 0x5FD3FF, transparent: true, opacity: .9, depthWrite: false, blending: THREE.AdditiveBlending}));
  world.add(trail);
  const ghost = new THREE.Mesh(trailGeo, new THREE.MeshBasicMaterial({color: 0x1463FF, transparent: true, opacity: .12, depthWrite: false}));
  world.add(ghost);
  const pill = new THREE.Group(); world.add(pill);
  const pillCore = new THREE.Mesh(new THREE.CapsuleGeometry(.11, .26, 8, 24), new THREE.MeshStandardMaterial({color: 0xffffff, emissive: new THREE.Color(0x22C7F2), emissiveIntensity: 1.6, roughness: .2}));
  pillCore.rotation.z = Math.PI / 2;
  const pillShell = new THREE.Mesh(new THREE.CapsuleGeometry(.2, .36, 8, 24), glassMat({color: 0xE8FAFF, roughness: .05, thickness: .3}));
  pillShell.rotation.z = Math.PI / 2;
  const pillGlow = sprite(glowTex('rgba(34,199,242,.95)'), 1.5, 1.5); pillGlow.material.blending = THREE.AdditiveBlending; pillGlow.renderOrder = 2;
  const pillLabel = sprite(chipTex('84.20 USD', 'PayPal payment'), 1.55, .43); pillLabel.position.y = .5;
  pill.add(pillGlow, pillShell, pillCore, pillLabel);
  const pillLight = new THREE.PointLight(0x22C7F2, 5, 4, 2); pill.add(pillLight);

  const paidChip = sprite(chipTex('Paid to the biller', 'never to the person', '#0E7A4E'), 2.2, .61);
  paidChip.position.set(2.45, -.6, 1.3); world.add(paidChip);

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
    // Fit the scene (about 3.7 units each side of centre, 3.1 up and down) at any aspect ratio.
    const tan = Math.tan(THREE.MathUtils.degToRad(camera.fov / 2));
    camera.position.z = Math.max(3.15 / tan, 3.85 / (tan * camera.aspect));
    camera.updateProjectionMatrix();
  }
  resize();
  const ro = new ResizeObserver(resize); ro.observe(host);

  const tmp = new THREE.Vector3();
  function frame(t, still) {
    const p = still ? t : t % LOOP;
    const out = seg(p, 8.9, 9.9);         // the whole flow fades away before the loop restarts
    // 1. Drop: the bill falls onto the reader.
    const drop = backOut(seg(p, .1, 1.1));
    billHolder.position.y = .35 + (1 - drop) * 4.6 + out * 1.2 + Math.sin(t * 1.1) * .03;
    billHolder.position.z = -.2 + (1 - drop) * .8;
    billHolder.rotation.z = .03 + (1 - drop) * .35;
    faceMat.opacity = 1;
    bill.traverse(o => { if (o.material && o !== stamp && o.material !== beamLine.material && o.material !== beamGlow.material) { o.material.transparent = out > 0; o.material.opacity = 1 - out; } });
    // 2. Read: the beam sweeps down and the fields slide out.
    const sweep = seg(p, 1.15, 2.75);
    beam.visible = sweep > 0 && sweep < 1;
    beam.position.y = H / 2 - .1 - sweep * (H - .2);
    beamLine.material.opacity = beamGlow.material.opacity = bell(p, 1.15, 2.75) * .95;
    faceMat.emissiveIntensity = .1 * bell(p, 1.1, 2.9);
    chips.forEach((c, i) => {
      const k = easeOut(seg(p, 1.55 + i * .4, 2.05 + i * .4)) * (1 - out);
      c.material.opacity = k;
      c.position.set(.9 + k * 1.55, c.userData.y + (1 - k) * .1, .9);
    });
    // 3. Pay: the sponsor's card lights up, the payment travels to the biller.
    cardGlow.material.opacity = .6 * bell(p, 2.9, 4.0);
    const travel = ease(seg(p, 3.3, 5.4));
    const shown = p < 3.3 ? 0 : Math.max(1, Math.floor(travel * SEGS));
    trailGeo.setDrawRange(0, shown * 8 * 6);
    trail.material.opacity = .9 * (1 - out);
    ghost.material.opacity = .12 * (1 - out);
    pill.visible = p > 3.15 && p < 6.0;
    path.getPointAt(Math.min(.999, travel), tmp); pill.position.copy(tmp);
    const pillIn = seg(p, 3.15, 3.45), pillOut = seg(p, 5.45, 5.95);
    pill.scale.setScalar(Math.max(.001, pillIn * (1 - pillOut)));
    pillShell.rotation.x = t * 1.2;
    // 4. Paid: the biller lights up and the bill is stamped.
    const arrive = bell(p, 5.2, 7.2);
    billerGlow.material.opacity = .85 * arrive;
    core.material.opacity = .5 + .5 * arrive; core.scale.x = 1 + .08 * arrive;
    const sIn = seg(p, 5.85, 6.2);
    stamp.material.opacity = sIn * (1 - out);
    const k = 1 + (1 - easeOut(sIn)) * 1.2; stamp.scale.set(k, k, 1);
    billHolder.position.z += p > 6.05 && p < 6.35 ? -Math.sin((p - 6.05) / .3 * Math.PI) * .1 : 0;
    paidChip.material.opacity = easeOut(seg(p, 6.25, 6.8)) * (1 - out);
    paidChip.position.y = -.62 + (1 - seg(p, 6.25, 6.8)) * -.15;
    // Lean.
    lean.x += (target.x - lean.x) * .05; lean.y += (target.y - lean.y) * .05;
    world.rotation.y = lean.x * .12; world.rotation.x = lean.y * .06;
    renderer.render(scene, camera);
  }

  if (reduce) {
    frame(7.4, true);
    host.classList.add('ready');
    return {stop() {}, frame};
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
  return {stop() { pause(); io.disconnect(); ro.disconnect(); }, frame};
}
