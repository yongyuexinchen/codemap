/* CodeMap detached micro window renderer.
 *
 * Renders one module's class diagram (same buildMicro as render.js) and
 * exposes window.__codemap_reload(), which Bridge.pop_micro() calls after
 * pointing MicroBridge at a new module. Wheel zooms at the cursor, left-drag
 * pans; all edges are orthogonal (curve:'step'), light theme only.
 */
(function () {
  'use strict';

  var graphEl = document.getElementById('graph');
  var titleEl = document.getElementById('micro-title');
  var statusEl = document.getElementById('status');
  var viewCountEl = document.getElementById('view-count');
  var zoomInBtn = document.getElementById('zoom-in');
  var zoomOutBtn = document.getElementById('zoom-out');
  var zoomResetBtn = document.getElementById('zoom-reset');

  var state = { zoom: 1, fitW: 0, fitH: 0, data: null };
  var renderSeq = 0;
  var MIN_ZOOM = 0.5, MAX_ZOOM = 20, ZOOM_STEP = 1.2, FIT_PAD = 32;

  var PALETTE = {
    classFill: '#8E5FD7',
    baseFill: '#D64545',
    fileFill: '#23272E',
    line: '#111111',
    label: '#8a8f98'
  };

  function esc(s) {
    return String(s == null ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;');
  }
  function sanitizeId(s) {
    return String(s).replace(/[^A-Za-z0-9_]/g, '_');
  }
  function api() { return window.pywebview && window.pywebview.api; }
  function setStatus(msg) { statusEl.textContent = msg; }

  // "新闻资讯（原 新闻模块）" when the LLM renamed the module; the original
  // name comes from the bridge (orig_name) and is kept visible.
  function moduleDisplayName(m) {
    if (!m) { return ''; }
    var orig = m.orig_name || m.name || '';
    return (orig && orig !== m.name) ? m.name + '（原 ' + orig + '）' : (m.name || orig);
  }

  mermaid.initialize({
    startOnLoad: false,
    securityLevel: 'loose',
    theme: 'base',
    themeVariables: {
      background: '#ffffff',
      primaryColor: '#ffffff',
      primaryBorderColor: '#ffffff',
      primaryTextColor: '#1a1a1a',
      secondaryColor: '#ffffff',
      tertiaryColor: '#ffffff',
      lineColor: PALETTE.line,
      textColor: '#1a1a1a',
      edgeLabelBackground: '#ffffff',
      clusterBkg: '#ffffff',
      clusterBorder: '#ffffff',
      fontFamily: '"Segoe UI", "Microsoft YaHei", system-ui, sans-serif',
      fontSize: '13px'
    },
    flowchart: {
      curve: 'step',
      htmlLabels: true,
      nodeSpacing: 40,
      rankSpacing: 60,
      padding: 10,
      useMaxWidth: false
    }
  });

  // Same change-set highlights as the main window: added = green outline,
  // modified = yellow fill + CSS blink, removed/added functions = dashed
  // ghost badges (functions are folded into classes, not drawn as nodes).
  function buildMicro(data) {
    var mod = data.module;
    var L = ['%%{init: {"flowchart": {"curve": "step"}}}%%',
      'flowchart TB', '  %% CodeMap 微观：' + mod.name + ' 类图',
      '  classDef addedClass fill:#ffffff,stroke:#2ecc71,stroke-width:2.5,color:#1a1a1a',
      '  classDef modifiedClass fill:#fff8dc,stroke:#f1c40f,stroke-width:2.5,color:#1a1a1a',
      '  classDef removedClass fill:#ffffff,stroke:#e74c3c,stroke-width:2,color:#e74c3c'];
    var clsId = {};
    data.classes.forEach(function (c) {
      var nid = 'C' + mod.key + '_' + sanitizeId(c.name);
      clsId[c.name] = nid;
      var label = '<b>' + esc(c.name) + '</b><br/>' +
        '<span style="font-size:11px;color:#e9e2f9">' + esc(c.file) + '</span>';
      if (c.methods.length) {
        label += '<br/><span style="font-size:11px;color:#efe9ff">' +
          esc(c.methods.slice(0, 5).join(' · ')) + '</span>';
      }
      L.push('  ' + nid + '["' + label + '"]');
      L.push('  style ' + nid + ' fill:' +
        (c.is_base ? PALETTE.baseFill : PALETTE.classFill) +
        ',stroke:#000,color:#fff');
      if (c.status === 'added') { L.push('  class ' + nid + ' addedClass'); }
      else if (c.status === 'modified') { L.push('  class ' + nid + ' modifiedClass'); }
    });
    var fileId = {};
    data.files.forEach(function (f) {
      var nid = 'F' + mod.key + '_' + sanitizeId(f.rel);
      fileId[f.rel] = nid;
      L.push('  ' + nid + '["📄 ' + esc(f.rel) + '"]');
      L.push('  style ' + nid + ' fill:' + PALETTE.fileFill +
        ',stroke:#000,color:#fff');
      if (f.status === 'added') { L.push('  class ' + nid + ' addedClass'); }
      else if (f.status === 'modified') { L.push('  class ' + nid + ' modifiedClass'); }
    });
    if (data.change) {
      (data.change.added_funcs || []).forEach(function (fn, i) {
        var nid = 'A' + mod.key + '_add_' + sanitizeId(fn.name) + '_' + i;
        L.push('  ' + nid + '["<span style="font-size:11px;color:#27ae60">+ 新增</span><br/><b>' +
          esc(fn.name) + '</b><br/><span style="font-size:10px;color:#27ae60">' +
          esc(fn.file) + '</span>"]');
        L.push('  style ' + nid +
          ' fill:#ffffff,stroke:#2ecc71,stroke-width:2,stroke-dasharray:5 3,color:#27ae60');
        L.push('  class ' + nid + ' addedClass');
      });
      (data.change.removed || []).forEach(function (r, i) {
        var nid = 'R' + mod.key + '_rem_' + sanitizeId(r.name) + '_' + i;
        L.push('  ' + nid + '["<span style="font-size:11px;color:#e74c3c">- 已删除</span><br/>' +
          '<span style="text-decoration:line-through"><b>' + esc(r.name) + '</b></span>' +
          '<br/><span style="font-size:10px;color:#e74c3c">' + esc(r.file) + '</span>"]');
        L.push('  style ' + nid +
          ' fill:#ffffff,stroke:#e74c3c,stroke-width:2,stroke-dasharray:5 3,color:#e74c3c');
        L.push('  class ' + nid + ' removedClass');
      });
    }
    data.inherits.forEach(function (e) {
      L.push('  ' + clsId[e.from] + ' -->|继承| ' + clsId[e.to]);
    });
    data.refs.forEach(function (r) {
      var srcId = r.from_file ? fileId[r.from_file] : clsId[r.from_class];
      if (!srcId || !clsId[r.to_class]) { return; }
      L.push('  ' + srcId + ' -.->|' + r.count + '| ' + clsId[r.to_class]);
    });
    return L.join('\n');
  }

  function applyZoom() {
    var svg = graphEl.querySelector('svg');
    var holder = svg ? svg.parentElement : null;
    if (!holder || !holder.classList.contains('mermaid')) { return; }
    var natW = parseFloat(svg.getAttribute('width'));
    var natH = parseFloat(svg.getAttribute('height'));
    if (!natW || !natH) { return; }
    var cw = graphEl.clientWidth || 1;
    var ch = graphEl.clientHeight || 1;
    var s = Math.min((cw - FIT_PAD) / natW, (ch - FIT_PAD) / natH);
    if (!isFinite(s) || s <= 0) { s = 1; }
    state.fitW = natW * s;
    state.fitH = natH * s;
    holder.style.width = (state.fitW * state.zoom) + 'px';
    holder.style.height = (state.fitH * state.zoom) + 'px';
    svg.style.width = '100%';
    svg.style.height = '100%';
    window.__codemap_zoom = state.zoom;
  }

  function setZoom(z, cx, cy) {
    var oldZ = state.zoom;
    z = Math.max(MIN_ZOOM, Math.min(MAX_ZOOM, z));
    var svg = graphEl.querySelector('svg');
    if (z === oldZ || !svg) { return; }
    state.zoom = z;
    var rect = graphEl.getBoundingClientRect();
    var px = (typeof cx === 'number' ? cx : rect.left + rect.width / 2);
    var py = (typeof cy === 'number' ? cy : rect.top + rect.height / 2);
    var holder = svg.parentElement;
    var hr = holder.getBoundingClientRect();
    var contentX = (px - hr.left) / oldZ;
    var contentY = (py - hr.top) / oldZ;
    applyZoom();
    var hr2 = holder.getBoundingClientRect();
    graphEl.scrollLeft += hr2.left - (px - contentX * z);
    graphEl.scrollTop += hr2.top - (py - contentY * z);
  }

  function updateZoomLabel() {
    zoomResetBtn.textContent = Math.round(state.zoom * 100) + '%';
  }

  // Orthogonal arrowheads (same fix as the main window): mermaid ends some
  // step-curved edges with duplicate/zero-length final points, where
  // `orient="auto"` has no defined direction — the arrow then renders
  // sideways off the line. Measure each edge's last real segment and assign
  // a direction-specific marker (r/l/u/d) with a baked-in triangle.
  var ARROW_MARKERS = {
    r: { d: 'M 0 0 L 10 5 L 0 10 z', refX: 8.5, refY: 5 },  // ->
    l: { d: 'M 10 0 L 0 5 L 10 10 z', refX: 1.5, refY: 5 }, // <-
    d: { d: 'M 0 0 L 5 10 L 10 0 z', refX: 5, refY: 8.5 },  // down
    u: { d: 'M 0 10 L 5 0 L 10 10 z', refX: 5, refY: 1.5 }  // up
  };

  function finalSegmentDir(path) {
    var re = /[ML]\s*([-\d.eE+]+)[,\s]+([-\d.eE+]+)/g;
    var m, pts = [];
    while ((m = re.exec(path.getAttribute('d') || ''))) {
      pts.push([+m[1], +m[2]]);
    }
    if (pts.length < 2) { return null; }
    var x = pts[pts.length - 1][0], y = pts[pts.length - 1][1];
    var dx = 0, dy = 0;
    for (var i = pts.length - 2; i >= 0; i--) {
      dx = x - pts[i][0];
      dy = y - pts[i][1];
      if (dx !== 0 || dy !== 0) { break; }
    }
    if (dx === 0 && dy === 0) { return null; }
    if (dx !== 0 && dy !== 0) { return null; }
    if (dx > 0) { return 'r'; }
    if (dx < 0) { return 'l'; }
    return dy > 0 ? 'd' : 'u';
  }

  function installArrowMarkers(svg) {
    if (!svg || svg.querySelector('marker#codemap-arrow')) { return; }
    var NS = 'http://www.w3.org/2000/svg';
    var defs = svg.querySelector('defs');
    if (!defs) {
      defs = document.createElementNS(NS, 'defs');
      svg.insertBefore(defs, svg.firstChild);
    }
    function addMarker(id, spec) {
      var marker = document.createElementNS(NS, 'marker');
      marker.setAttribute('id', id);
      marker.setAttribute('viewBox', '0 0 10 10');
      marker.setAttribute('refX', String(spec.refX));
      marker.setAttribute('refY', String(spec.refY));
      marker.setAttribute('markerWidth', '7');
      marker.setAttribute('markerHeight', '7');
      var arrow = document.createElementNS(NS, 'path');
      arrow.setAttribute('d', spec.d);
      arrow.setAttribute('fill', '#111111');
      marker.appendChild(arrow);
      defs.appendChild(marker);
    }
    addMarker('codemap-arrow', { d: 'M 0 0 L 10 5 L 0 10 z',
      refX: 8.5, refY: 5 });
    Object.keys(ARROW_MARKERS).forEach(function (k) {
      addMarker('codemap-arrow-' + k, ARROW_MARKERS[k]);
    });
    svg.querySelectorAll('path.flowchart-link').forEach(function (p) {
      var dir = finalSegmentDir(p);
      if (dir) {
        p.style.setProperty('marker-end',
          'url(#codemap-arrow-' + dir + ')', 'important');
      }
    });
  }

  function render() {
    if (!state.data) { return; }
    var d = state.data;
    var dname = moduleDisplayName(d.module);
    titleEl.textContent = dname;
    viewCountEl.innerHTML = '类 <b>' + d.stats.classes + '</b>' +
      ' · 文件 <b>' + d.stats.files + '</b> · 边 <b>' + d.stats.edges + '</b>';
    setStatus('微观：' + dname + ' 类图' +
      (d.module.desc ? ' · ' + d.module.desc : '') +
      ' · 滚轮缩放 · 拖动平移');
    var id = 'codemap-micro-' + (++renderSeq);
    mermaid.render(id, buildMicro(d)).then(function (res) {
      var holder = document.createElement('div');
      holder.className = 'mermaid';
      holder.innerHTML = res.svg.replace(/<script[\s\S]*?<\/script>/gi, '');
      installArrowMarkers(holder.querySelector('svg'));
      graphEl.innerHTML = '';
      graphEl.appendChild(holder);
      state.zoom = 1; state.fitW = 0; state.fitH = 0;
      applyZoom();
      updateZoomLabel();
    }).catch(function (err) { setStatus('渲染失败: ' + err); });
  }

  // Called by the main window's Bridge.pop_micro() after the bridge has been
  // pointed at a new module.
  window.__codemap_reload = function () {
    setStatus('读取类图…');
    api().get_micro().then(function (data) {
      if (data.error) { setStatus('读取失败: ' + data.error); return; }
      state.data = data;
      render();
    }).catch(function (err) { setStatus('读取失败: ' + err); });
  };

  // ---- boot ---------------------------------------------------------------
  function boot() {
    if (api()) { window.__codemap_reload(); }
    else { setStatus('pywebview 桥接不可用'); }
  }

  window.addEventListener('pywebviewready', boot);
  if (window.pywebview) { boot(); }

  graphEl.addEventListener('wheel', function (e) {
    e.preventDefault();
    setZoom(state.zoom * (e.deltaY < 0 ? ZOOM_STEP : 1 / ZOOM_STEP),
      e.clientX, e.clientY);
  }, { passive: false });
  zoomInBtn.addEventListener('click', function () { setZoom(state.zoom * ZOOM_STEP); updateZoomLabel(); });
  zoomOutBtn.addEventListener('click', function () { setZoom(state.zoom / ZOOM_STEP); updateZoomLabel(); });
  zoomResetBtn.addEventListener('click', function () { setZoom(1); updateZoomLabel(); });

  var pan = null;
  var panMoved = 0;
  graphEl.addEventListener('pointerdown', function (e) {
    if (e.button !== 0) { return; }
    pan = { x: e.clientX, y: e.clientY, sl: graphEl.scrollLeft, st: graphEl.scrollTop };
    panMoved = 0;
    try { e.target.setPointerCapture(e.pointerId); } catch (err) { /* synthetic events */ }
    graphEl.classList.add('panning');
    e.preventDefault();
    e.stopPropagation();
  });
  graphEl.addEventListener('pointermove', function (e) {
    if (!pan) { return; }
    var dx = e.clientX - pan.x;
    var dy = e.clientY - pan.y;
    if (Math.abs(dx) + Math.abs(dy) > 4) { panMoved = 1; }
    graphEl.scrollLeft = pan.sl - dx;
    graphEl.scrollTop = pan.st - dy;
    e.preventDefault();
  });
  function endPan(e) {
    if (!pan) { return; }
    pan = null;
    graphEl.classList.remove('panning');
    try { e.target.releasePointerCapture(e.pointerId); } catch (err) { /* synthetic events */ }
    e.preventDefault();
    setTimeout(function () { panMoved = 0; }, 300);
  }
  graphEl.addEventListener('pointerup', endPan);
  graphEl.addEventListener('pointercancel', endPan);

  function closeWindow() {
    if (api()) { api().close(); }  // MicroBridge.close(): hide the window
  }
  document.getElementById('close').addEventListener('click', closeWindow);
  document.getElementById('minimize').addEventListener('click', function () {
    if (api()) { api().minimize(); }
  });
  window.addEventListener('keydown', function (e) {
    if (e.key === 'Escape') { closeWindow(); return; }
    if (e.ctrlKey && (e.key === '=' || e.key === '+')) {
      e.preventDefault(); setZoom(state.zoom * ZOOM_STEP); updateZoomLabel();
    } else if (e.ctrlKey && e.key === '-') {
      e.preventDefault(); setZoom(state.zoom / ZOOM_STEP); updateZoomLabel();
    } else if (e.ctrlKey && e.key === '0') {
      e.preventDefault(); setZoom(1); updateZoomLabel();
    }
  });

  // ---- visible border resize -------------------------------------------
  var handles = document.querySelectorAll('.rh');
  var drag = null;

  function onHandleDown(e) {
    e.preventDefault();
    e.stopPropagation();
    drag = { edges: e.target.getAttribute('data-edges'), sx: e.screenX, sy: e.screenY };
    if (e.target.setPointerCapture) { e.target.setPointerCapture(e.pointerId); }
  }

  function onHandleMove(e) {
    if (!drag || !window.pywebview || !window.pywebview.api) { return; }
    var dx = e.screenX - drag.sx;
    var dy = e.screenY - drag.sy;
    window.pywebview.api.resize(drag.edges, dx, dy);
  }

  function onHandleUp(e) {
    if (!drag) { return; }
    drag = null;
    if (e.target.releasePointerCapture) { e.target.releasePointerCapture(e.pointerId); }
  }

  handles.forEach(function (h) {
    h.addEventListener('pointerdown', onHandleDown);
    h.addEventListener('pointermove', onHandleMove);
    h.addEventListener('pointerup', onHandleUp);
    h.addEventListener('pointercancel', onHandleUp);
    h.addEventListener('mousedown', function (e) { e.stopPropagation(); });
  });
})();
