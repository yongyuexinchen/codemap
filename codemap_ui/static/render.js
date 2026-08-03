/* CodeMap mermaid block-diagram renderer (light theme only).
 *
 * Macro view: business-module flowchart (flowchart TB, one yellow subgraph
 * per module listing its key files, cross-module dependency arrows with
 * aggregated counts). Micro view: the selected module's class diagram
 * (purple classes / red base classes / black file cubes; solid INHERITS
 * edges, dashed REFERENCES/CALLS edges with counts). All edges use
 * curve:'step' so lines are strictly horizontal/vertical.
 *
 * Navigation:
 *   - toolbar buttons 宏观 / 微观 switch views
 *   - module dropdown picks which module the micro view shows
 *   - click a module box -> micro view of that module
 *   - ⧉ 弹出 pops the micro view out into its own native window
 *   - wheel zooms at the cursor, left-drag pans
 */
(function () {
  'use strict';

  var graphEl = document.getElementById('graph');
  var statusEl = document.getElementById('status');
  var viewCountEl = document.getElementById('view-count');
  var zoomInBtn = document.getElementById('zoom-in');
  var zoomOutBtn = document.getElementById('zoom-out');
  var zoomResetBtn = document.getElementById('zoom-reset');
  var viewMacroBtn = document.getElementById('view-macro');
  var viewMicroBtn = document.getElementById('view-micro');
  var moduleSearch = document.getElementById('module-search');
  var moduleListEl = document.getElementById('module-list');
  var popMicroBtn = document.getElementById('pop-micro');

  var state = { view: 'macro', module: 'AUTH', macroData: null,
    microData: null, activeVp: null, lastChange: null };
  var renderSeq = 0;
  var MIN_ZOOM = 0.5, MAX_ZOOM = 4, ZOOM_STEP = 1.2, FIT_PAD = 32;

  // ---- in-app project loading ("📂 打开项目") ---------------------------
  // Lets the user point the app at any repo path from inside the window:
  // paste the path, pick a recent one, or drop the folder onto the window.
  var repoOverlayEl = document.getElementById('repo-overlay');
  var repoPathEl = document.getElementById('repo-path');
  var repoCurrentEl = document.getElementById('repo-current');
  var repoStatusEl = document.getElementById('repo-status');
  var repoRecentsEl = document.getElementById('repo-recents');
  var repoLoadBtn = document.getElementById('repo-load');
  var repoLoading = false;
  var RECENT_KEY = 'codemap_recent_repos';

  function getRecents() {
    try {
      var r = JSON.parse(localStorage.getItem(RECENT_KEY) || '[]');
      return Array.isArray(r) ? r : [];
    } catch (e) { return []; }
  }
  function saveRecents(list) {
    try { localStorage.setItem(RECENT_KEY, JSON.stringify(list)); }
    catch (e) { /* private mode / file:// - ignore */ }
  }
  function renderRecents() {
    repoRecentsEl.innerHTML = '';
    var list = getRecents();
    if (!list.length) { return; }
    var label = document.createElement('div');
    label.className = 'recent-label';
    label.textContent = '最近项目';
    repoRecentsEl.appendChild(label);
    list.forEach(function (p) {
      var item = document.createElement('div');
      item.className = 'recent-item';
      item.textContent = p;
      item.title = '加载 ' + p;
      item.addEventListener('mousedown', function (e) {
        e.preventDefault();
        loadRepo(p);
      });
      repoRecentsEl.appendChild(item);
    });
  }
  function addRecentRepo(path) {
    var list = getRecents().filter(function (p) { return p !== path; });
    list.unshift(path);
    saveRecents(list.slice(0, 5));
  }
  function openRepoPanel() {
    renderRecents();
    repoOverlayEl.hidden = false;
    repoStatusEl.textContent = '';
    repoPathEl.focus();
    repoPathEl.select();
  }
  function closeRepoPanel() {
    repoOverlayEl.hidden = true;
  }

  // Rebuild the module picker from a loaded repo's module list. Known
  // business modules (default repo) keep their Chinese/pinyin aliases so the
  // fuzzy search behaves exactly as before; derived repos search by
  // key + name + lowercased key.
  function applyModules(mods) {
    if (!mods || !mods.length) { return; }
    var KNOWN_HAY = {
      AUTH: 'auth 认证 renzheng login 登录 用户 鉴权 权限 账号',
      NEWS: 'news 新闻 xinwen article 资讯 文章 内容 频道',
      FAV: 'fav favorite 收藏 shoucang like 喜欢 关注',
      HIST: 'hist history 历史 lishi 浏览 足迹 记录',
      SUPPORT: 'support 支撑 zhicheng util 工具 公共 基础 组件 util'
    };
    MODULES = mods.map(function (m) {
      var key = String(m.key);
      return {
        key: key,
        icon: m.icon || '📁',
        name: m.name || key,
        hay: KNOWN_HAY[key] || (key + ' ' + (m.name || '') + ' ' + key.toLowerCase())
      };
    });
    MODULE_KEYS = MODULES.map(function (m) { return m.key; });
    if (MODULE_KEYS.indexOf(state.module) === -1) {
      state.module = MODULE_KEYS[0];
    }
  }

  function loadRepo(path) {
    if (!api()) { setStatus('pywebview 桥接不可用'); return; }
    repoLoading = true;
    repoLoadBtn.disabled = true;
    repoStatusEl.textContent = '正在加载项目（首次会构建图谱，可能需要几分钟）…';
    api().load_repo(path).then(function (r) {
      repoLoading = false;
      repoLoadBtn.disabled = false;
      if (!r || r.error || r.status !== 'ok') {
        repoStatusEl.textContent = '加载失败: ' + ((r && r.error) || '未知错误');
        return;
      }
      var mods = (r.modules && r.modules.length)
        ? r.modules : ((r.macro && r.macro.modules) || []);
      applyModules(mods);
      state.macroData = r.macro;
      state.view = 'macro';
      lastDbMtime = r.mtime;
      if (mods.length) { state.module = mods[0].key; }
      setPickerValue(state.module);
      repoCurrentEl.textContent = r.repo;
      addRecentRepo(r.repo);
      closeRepoPanel();
      setStatus('已加载项目：' + r.repo);
      setViewButtons();
      renderView();
    }).catch(function (err) {
      repoLoading = false;
      repoLoadBtn.disabled = false;
      repoStatusEl.textContent = '加载失败: ' + err;
    });
  }

  // ---- searchable module picker ----------------------------------------
  // Replaces the native <select>, which is unreliable inside the frameless
  // WebView2 window. Typing fuzzy-filters (subsequence match) against the
  // key, Chinese name and pinyin/English aliases; Arrow/Enter navigate;
  // selecting jumps straight to that module's micro view.
  var MODULES = [
    { key: 'AUTH', icon: '🔐', name: '认证模块',
      hay: 'auth 认证 renzheng login 登录 用户 鉴权 权限 账号' },
    { key: 'NEWS', icon: '📰', name: '新闻模块',
      hay: 'news 新闻 xinwen article 资讯 文章 内容 频道' },
    { key: 'FAV', icon: '⭐', name: '收藏模块',
      hay: 'fav favorite 收藏 shoucang like 喜欢 关注' },
    { key: 'HIST', icon: '🕘', name: '历史模块',
      hay: 'hist history 历史 lishi 浏览 足迹 记录' },
    { key: 'SUPPORT', icon: '🔧', name: '支撑模块',
      hay: 'support 支撑 zhicheng util 工具 公共 基础 组件 util' }
  ];
  var picker = { open: false, active: -1, filtered: MODULES.slice() };

  function moduleByKey(key) {
    for (var i = 0; i < MODULES.length; i++) {
      if (MODULES[i].key === key) { return MODULES[i]; }
    }
    return MODULES[0];
  }

  // True when the input still holds a picker display value we wrote, e.g.
  // "🔐 认证模块". While it does, the dropdown opens in pure-dropdown mode
  // (show every module); once the user types, the value no longer matches
  // and the dropdown switches to fuzzy-search mode.
  function isDisplayValue(q) {
    q = (q || '').trim();
    for (var i = 0; i < MODULES.length; i++) {
      if (q === MODULES[i].icon + ' ' + MODULES[i].name) { return true; }
    }
    return false;
  }

  // Drop a leading module icon from the query so a leftover display value
  // ("🔐 认证模块") filters by the module name instead of matching nothing.
  function normalizeQuery(q) {
    q = (q || '').trim();
    for (var i = 0; i < MODULES.length; i++) {
      var prefix = MODULES[i].icon + ' ';
      if (q.indexOf(prefix) === 0) { q = q.slice(prefix.length); break; }
    }
    return q;
  }

  // Subsequence match: every char of the query appears in order in the text.
  function fuzzyMatch(text, query) {
    query = query.toLowerCase().replace(/\s+/g, '');
    if (!query) { return true; }
    text = text.toLowerCase();
    var qi = 0;
    for (var i = 0; i < text.length && qi < query.length; i++) {
      if (text.charCodeAt(i) === query.charCodeAt(qi)) { qi++; }
    }
    return qi === query.length;
  }

  // Wrap matched chars in <b> so the user sees why an item matched.
  function highlight(text, query) {
    query = query.toLowerCase().replace(/\s+/g, '');
    if (!query) { return text; }
    var t = text.toLowerCase();
    var out = '', last = 0, qi = 0;
    for (var i = 0; i < t.length && qi < query.length; i++) {
      if (t.charCodeAt(i) === query.charCodeAt(qi)) {
        out += text.slice(last, i) + '<b>' + text[i] + '</b>';
        last = i + 1;
        qi++;
      }
    }
    return out + text.slice(last);
  }

  function renderModuleList(rawQuery) {
    // A display value ("🔐 认证模块") is a selection placeholder, not a real
    // search: open the list showing every module. Anything else is a fuzzy
    // query (icon prefix stripped, if present).
    var query = isDisplayValue(rawQuery) ? '' : normalizeQuery(rawQuery);
    picker.filtered = MODULES.filter(function (m) {
      return fuzzyMatch(m.key + ' ' + m.name + ' ' + m.hay, query);
    });
    moduleListEl.innerHTML = '';
    if (!picker.filtered.length) {
      var empty = document.createElement('div');
      empty.className = 'empty';
      empty.textContent = '没有匹配的板块';
      moduleListEl.appendChild(empty);
      picker.active = -1;
      return;
    }
    picker.filtered.forEach(function (m, i) {
      var item = document.createElement('div');
      item.className = 'item' + (i === picker.active ? ' active' : '');
      item.innerHTML = '<span>' + m.icon + ' ' + highlight(m.name, query) +
        '</span><span class="hint">' + m.key + '</span>';
      // mousedown (not click) so the input keeps focus and blur never
      // hides the list before the selection lands.
      item.addEventListener('mousedown', function (e) {
        e.preventDefault();
        selectModule(m.key);
      });
      moduleListEl.appendChild(item);
    });
  }

  function showModuleList() {
    renderModuleList(moduleSearch.value);
    moduleListEl.hidden = false;
    picker.open = true;
  }

  function hideModuleList() {
    moduleListEl.hidden = true;
    picker.open = false;
    picker.active = -1;
  }

  function setPickerValue(key) {
    var m = moduleByKey(key);
    moduleSearch.value = m.icon + ' ' + m.name;
  }

  function selectModule(key) {
    setPickerValue(key);
    hideModuleList();
    // Release focus so the next click re-opens the dropdown (the item
    // mousedown keeps focus to land the selection, which would otherwise
    // swallow the following focus event).
    moduleSearch.blur();
    openMicro(key);
  }

  // Light codeflow-style palette: yellow modules, purple classes,
  // red base classes, black file cubes, black lines, gray labels.
  var PALETTE = {
    moduleFill: '#FFF3C4',
    moduleBorder: '#C9A227',
    classFill: '#8E5FD7',
    baseFill: '#D64545',
    fileFill: '#23272E',
    line: '#111111',
    label: '#8a8f98'
  };
  var MODULE_KEYS = ['AUTH', 'NEWS', 'FAV', 'HIST', 'SUPPORT'];

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

  // ---- mermaid ---------------------------------------------------------
  mermaid.initialize({
    startOnLoad: false,
    securityLevel: 'loose',
    theme: 'base',
    themeVariables: {
      background: '#ffffff',
      primaryColor: PALETTE.moduleFill,
      primaryBorderColor: PALETTE.moduleBorder,
      primaryTextColor: '#1a1a1a',
      secondaryColor: '#ffffff',
      tertiaryColor: '#ffffff',
      lineColor: PALETTE.line,
      textColor: '#1a1a1a',
      edgeLabelBackground: '#ffffff',
      clusterBkg: PALETTE.moduleFill,
      clusterBorder: PALETTE.moduleBorder,
      fontFamily: '"Segoe UI", "Microsoft YaHei", system-ui, sans-serif',
      fontSize: '13px'
    },
    flowchart: {
      curve: 'step',      // orthogonal (horizontal/vertical) edges only
      htmlLabels: true,
      nodeSpacing: 40,
      rankSpacing: 60,
      padding: 10,
      useMaxWidth: false
    }
  });

  // ---- macro view: business modules ------------------------------------
  function buildMacro(data) {
    // Diagram-level init directive: forces orthogonal step curves even if a
    // future mermaid.initialize() merge drops the flowchart.curve setting.
    var L = ['%%{init: {"flowchart": {"curve": "step"}}}%%',
      'flowchart TB', '  %% CodeMap 宏观：业务模块图'];
    data.modules.forEach(function (mod) {
      L.push('  subgraph G' + mod.key + '["' + mod.icon + ' ' + esc(mod.name) + '"]');
      var lines = mod.files.map(function (f) { return esc(f); }).join('<br/>');
      var more = mod.more_files
        ? '<br/><span style="color:#c8cdd6;font-size:11px">+ ' + mod.more_files + ' 更多</span>' : '';
      // Change badges from the last incremental refresh (real graph diff).
      var ch = mod.change || { added: 0, modified: 0, removed: 0 };
      var badge = '';
      if (ch.added || ch.modified || ch.removed) {
        badge = '<br/><span style="font-size:11px">' +
          (ch.added ? '<span style="color:#27ae60">+新增 ' + ch.added + '</span> ' : '') +
          (ch.modified ? '<span style="color:#d4a017">~修改 ' + ch.modified + '</span> ' : '') +
          (ch.removed ? '<span style="color:#e74c3c">-删除 ' + ch.removed + '</span>' : '') +
          '</span>';
      }
      var label =
        '<div style="text-align:left;line-height:1.5">' +
        '<b>' + esc(mod.name) + '</b><br/>' +
        '<span style="color:#c8cdd6;font-size:11px">' + mod.class_count + ' 类 · ' +
          mod.file_count + ' 文件</span><br/>' +
        lines + more + badge +
        '</div>';
      L.push('    M' + mod.key + '["' + label + '"]');
      L.push('  end');
    });
    data.edges.forEach(function (e) {
      L.push('  M' + e.from + ' -->|' + e.count + '| M' + e.to);
    });
    data.modules.forEach(function (mod) {
      L.push('  style M' + mod.key + ' fill:' + PALETTE.fileFill +
        ',stroke:#000,color:#fff');
    });
    return L.join('\n');
  }

  // ---- micro view: class diagram of one module -------------------------
  // Incremental-change highlights (from the last refresh's real SQL diff):
  //   added    -> green outline   (classDef addedClass, CSS backs it up)
  //   modified -> yellow fill + CSS blink (classDef modifiedClass)
  //   removed  -> red dashed ghost node (classDef removedClass)
  // Added functions don't render as standalone nodes, so they appear as
  // small green ghost badges next to the diagram.
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

  // Normalise the arrowhead. mermaid's default marker can sit slightly off
  // the line end on step-curved edges, which reads as a "crooked" arrow;
  // injecting one clean triangle marker and forcing every flowchart edge to
  // use it keeps arrowheads pointing exactly along the last line segment.
  function installArrowMarker(svg) {
    if (!svg || svg.querySelector('marker#codemap-arrow')) { return; }
    var NS = 'http://www.w3.org/2000/svg';
    var defs = svg.querySelector('defs');
    if (!defs) {
      defs = document.createElementNS(NS, 'defs');
      svg.insertBefore(defs, svg.firstChild);
    }
    var marker = document.createElementNS(NS, 'marker');
    marker.setAttribute('id', 'codemap-arrow');
    marker.setAttribute('viewBox', '0 0 10 10');
    marker.setAttribute('refX', '8.5');
    marker.setAttribute('refY', '5');
    marker.setAttribute('markerWidth', '7');
    marker.setAttribute('markerHeight', '7');
    marker.setAttribute('orient', 'auto-start-reverse');
    var arrow = document.createElementNS(NS, 'path');
    arrow.setAttribute('d', 'M 0 0 L 10 5 L 0 10 z');
    arrow.setAttribute('fill', PALETTE.line);
    marker.appendChild(arrow);
    defs.appendChild(marker);
  }

  // ---- viewport: one scrollable container, own zoom + pan --------------
  // #graph is the only viewport (macro and micro both render into it).
  // The fit/zoom/anchor math is identical to the original single-view
  // implementation.
  function makeViewport(container) {
    var vp = { container: container, zoom: 1, fitW: 0, fitH: 0 };

    vp.applyZoom = function () {
      var svg = container.querySelector('svg');
      var holder = svg ? svg.parentElement : null;
      if (!holder || !holder.classList.contains('mermaid')) { return; }
      var natW = parseFloat(svg.getAttribute('width'));
      var natH = parseFloat(svg.getAttribute('height'));
      if (!natW || !natH) { return; }
      var cw = container.clientWidth || 1;
      var ch = container.clientHeight || 1;
      var s = Math.min((cw - FIT_PAD) / natW, (ch - FIT_PAD) / natH);
      if (!isFinite(s) || s <= 0) { s = 1; }
      vp.fitW = natW * s;
      vp.fitH = natH * s;
      holder.style.width = (vp.fitW * vp.zoom) + 'px';
      holder.style.height = (vp.fitH * vp.zoom) + 'px';
      svg.style.width = '100%';
      svg.style.height = '100%';
      window.__codemap_zoom = vp.zoom;
    };

    vp.setZoom = function (z, cx, cy) {
      var oldZ = vp.zoom;
      z = Math.max(MIN_ZOOM, Math.min(MAX_ZOOM, z));
      var svg = container.querySelector('svg');
      if (z === oldZ || !svg) { return; }
      vp.zoom = z;
      var rect = container.getBoundingClientRect();
      var px = (typeof cx === 'number' ? cx : rect.left + rect.width / 2);
      var py = (typeof cy === 'number' ? cy : rect.top + rect.height / 2);
      var holder = svg.parentElement;
      var hr = holder.getBoundingClientRect();
      var contentX = (px - hr.left) / oldZ;
      var contentY = (py - hr.top) / oldZ;
      vp.applyZoom();
      var hr2 = holder.getBoundingClientRect();
      container.scrollLeft += hr2.left - (px - contentX * z);
      container.scrollTop += hr2.top - (py - contentY * z);
    };

    vp.render = function (code, onDone) {
      var id = 'codemap-graph-' + (++renderSeq);
      return mermaid.render(id, code).then(function (res) {
        var holder = document.createElement('div');
        holder.className = 'mermaid';
        holder.innerHTML = res.svg.replace(/<script[\s\S]*?<\/script>/gi, '');
        installArrowMarker(holder.querySelector('svg'));
        container.innerHTML = '';
        container.appendChild(holder);
        vp.zoom = 1; vp.fitW = 0; vp.fitH = 0;
        vp.applyZoom();
        vp.bindClusters();
        if (onDone) { onDone(); }
      });
    };

    vp.bindClusters = function () {
      if (container !== graphEl) { return; }
      container.querySelectorAll('g.cluster').forEach(function (g) {
        ['click', 'dblclick'].forEach(function (t) {
          g.addEventListener(t, function () {
            var id = g.getAttribute('id') || '';
            var key = null;
            MODULE_KEYS.forEach(function (k) {
              if (id.indexOf('G' + k) !== -1) { key = k; }
            });
            if (key) { onModuleClick(key); }
          });
        });
      });
    };
    // preventDefault on pointerdown also stops pywebview's easy_drag window
    // move, so the diagram navigates and the header moves the window.
    vp.attach = function () {
      container.addEventListener('wheel', function (e) {
        e.preventDefault();
        state.activeVp = vp;
        vp.setZoom(vp.zoom * (e.deltaY < 0 ? ZOOM_STEP : 1 / ZOOM_STEP),
          e.clientX, e.clientY);
        updateZoomLabel();
      }, { passive: false });

      var pan = null;
      var panMoved = 0;
      container.addEventListener('pointerdown', function (e) {
        if (e.button !== 0) { return; }
        pan = { x: e.clientX, y: e.clientY, sl: container.scrollLeft, st: container.scrollTop };
        panMoved = 0;
        try { e.target.setPointerCapture(e.pointerId); } catch (err) { /* synthetic events */ }
        container.classList.add('panning');
        e.preventDefault();
        e.stopPropagation();
      });
      container.addEventListener('pointermove', function (e) {
        if (!pan) { return; }
        var dx = e.clientX - pan.x;
        var dy = e.clientY - pan.y;
        if (Math.abs(dx) + Math.abs(dy) > 4) { panMoved = 1; }
        container.scrollLeft = pan.sl - dx;
        container.scrollTop = pan.st - dy;
        e.preventDefault();
      });
      function endPan(e) {
        if (!pan) { return; }
        pan = null;
        container.classList.remove('panning');
        try { e.target.releasePointerCapture(e.pointerId); } catch (err) { /* synthetic events */ }
        e.preventDefault();
        // Clear the drag flag shortly after release so a later click is not
        // suppressed; a fresh drag re-arms it on the next pointerdown.
        setTimeout(function () { panMoved = 0; }, 300);
      }
      container.addEventListener('pointerup', endPan);
      container.addEventListener('pointercancel', endPan);
      // A real drag suppresses the click/dblclick that would otherwise follow
      // (e.g. accidentally opening a module right after panning).
      ['click', 'dblclick'].forEach(function (t) {
        container.addEventListener(t, function (e) {
          if (panMoved) { panMoved = 0; e.stopPropagation(); e.preventDefault(); }
        }, true);
      });
    };

    return vp;
  }

  var vpMain = makeViewport(graphEl);
  state.activeVp = vpMain;
  vpMain.attach();

  // ---- view switching ----------------------------------------------------
  function setViewButtons() {
    viewMacroBtn.classList.toggle('active', state.view === 'macro');
    viewMicroBtn.classList.toggle('active', state.view === 'micro');
  }

  function updateZoomLabel() {
    var vp = state.activeVp || vpMain;
    zoomResetBtn.textContent = Math.round(vp.zoom * 100) + '%';
  }

  function renderView() {
    window.__codemap_view = state.view;
    if (state.view === 'macro') {
      var m = state.macroData;
      viewCountEl.innerHTML = '模块 <b>' + m.modules.length + '</b>' +
        ' · 跨模块依赖 <b>' + m.edges.length + '</b> 组 / ' +
        '<b>' + m.stats.cross_edges + '</b> 条';
      setStatus('宏观：' + m.modules.length + ' 个业务模块 · 滚轮缩放 · 拖动平移 · 点击模块方块 / 「微观」进类图');
      state.activeVp = vpMain;
      vpMain.render(buildMacro(m), updateZoomLabel)
        .catch(function (err) { setStatus('渲染失败: ' + err); });
    } else if (state.view === 'micro') {
      var d = state.microData;
      viewCountEl.innerHTML = d.module.name + ' · 类 <b>' + d.stats.classes + '</b>' +
        ' · 文件 <b>' + d.stats.files + '</b> · 边 <b>' + d.stats.edges + '</b>';
      setStatus('微观：' + d.module.name + ' 类图 · 滚轮缩放 · 拖动平移 · 「宏观」按钮返回 · 「⧉ 弹出」拆独立窗口');
      state.activeVp = vpMain;
      vpMain.render(buildMicro(d), updateZoomLabel)
        .catch(function (err) { setStatus('渲染失败: ' + err); });
    }
  }

  // ---- data loading -------------------------------------------------------
  function loadMacro() {
    state.view = 'macro';
    setPickerValue(state.module);
    hideModuleList();
    setViewButtons();
    setStatus('读取业务模块图…');
    api().get_macro().then(function (data) {
      if (data.error) { setStatus('读取失败: ' + data.error); return; }
      state.macroData = data;
      window.__codemap_nodes = data.stats.classes;
      window.__codemap_edges = data.stats.total_edges;
      renderView();
    }).catch(function (err) { setStatus('读取失败: ' + err); });
  }

  function openMicro(key) {
    state.view = 'micro';
    state.module = key;
    setPickerValue(key);
    hideModuleList();
    setViewButtons();
    setStatus('读取类图…');
    api().get_micro(key).then(function (data) {
      if (data.error) { setStatus('读取失败: ' + data.error); return; }
      state.microData = data;
      renderView();
    }).catch(function (err) { setStatus('读取失败: ' + err); });
  }

  // Called when a module box is clicked (or double-clicked) -> micro view.
  function onModuleClick(key) {
    openMicro(key);
  }

  function reload() {
    if (state.view === 'macro') { loadMacro(); }
    else { openMicro(state.module); }
  }

  // ---- boot --------------------------------------------------------------
  function boot() {
    if (api()) {
      armPolling();
      loadMacro();
    }
    else { setStatus('pywebview 桥接不可用'); }
  }

  window.addEventListener('pywebviewready', boot);
  if (window.pywebview) { boot(); }

  // ---- incremental refresh (CRG update + change-set highlight) ----------
  // Bridge.refresh() runs `code-review-graph update --repo <root> -q`, then
  // diffs the nodes table before/after (file_hash deltas + qualified_name
  // add/remove) and returns {added, modified, removed}. The current view is
  // re-pulled afterwards, so the data carries per-node status flags and the
  // diagrams render green/yellow/red highlights. A 2 s poll on graph.db's
  // mtime re-triggers this automatically whenever the db changes.
  var POLL_MS = 2000;
  var lastDbMtime = null;
  var REFRESH_BTN = document.getElementById('refresh');

  function applyChangeSet(r, auto) {
    if (!r || r.error) {
      setStatus('刷新失败: ' + ((r && r.error) || '未知错误'));
      return;
    }
    if (r.status === 'busy') { setStatus('刷新进行中…'); return; }
    lastDbMtime = r.db_mtime;
    if (r.changed && r.change_set) {
      var cs = r.change_set;
      state.lastChange = { changed: true, auto: !!auto,
        added: cs.added.length, modified: cs.modified.length,
        removed: cs.removed.length };
      window.__codemap_last_change = state.lastChange;
      setStatus('已刷新：新增 ' + cs.added.length + ' · 修改 ' +
        cs.modified.length + ' · 删除 ' + cs.removed.length +
        (auto ? '（自动检测到图谱更新）' : ''));
      reload();  // re-pull; data now carries the change-set highlights
    } else {
      state.lastChange = { changed: false, auto: !!auto };
      window.__codemap_last_change = state.lastChange;
      setStatus('图谱已是最新，无变更');
    }
  }

  function doRefresh(auto) {
    if (!api()) { setStatus('pywebview 桥接不可用'); return; }
    setStatus(auto ? '检测到图谱更新，正在增量刷新…' : '刷新中：增量更新图谱…');
    api().refresh().then(function (r) { applyChangeSet(r, auto); })
      .catch(function (err) { setStatus('刷新失败: ' + err); });
  }

  function armPolling() {
    if (!api()) { return; }
    api().stat().then(function (s) {
      if (s && !s.error && s.mtime != null) { lastDbMtime = s.mtime; }
      if (s && s.repo && repoCurrentEl) { repoCurrentEl.textContent = s.repo; }
    });
    setInterval(function () {
      if (!api() || repoLoading) { return; }  // pause while loading a repo
      api().stat().then(function (s) {
        if (!s || s.error || s.mtime == null) { return; }
        if (lastDbMtime == null) { lastDbMtime = s.mtime; return; }
        if (s.mtime !== lastDbMtime) {
          lastDbMtime = s.mtime;
          doRefresh(true);
        }
      }).catch(function () { /* transient stat errors are ignored */ });
    }, POLL_MS);
  }

  REFRESH_BTN.addEventListener('click', function () { doRefresh(false); });

  // ---- in-app repo loading: panel, drag-drop, recent list --------------
  document.getElementById('open-repo').addEventListener('click', openRepoPanel);
  document.getElementById('repo-cancel').addEventListener('click', closeRepoPanel);
  repoLoadBtn.addEventListener('click', function () {
    var p = repoPathEl.value.trim();
    if (!p) { repoStatusEl.textContent = '请输入仓库路径'; return; }
    loadRepo(p);
  });
  repoPathEl.addEventListener('keydown', function (e) {
    if (e.key === 'Enter') {
      e.preventDefault();
      var p = repoPathEl.value.trim();
      if (!p) { repoStatusEl.textContent = '请输入仓库路径'; return; }
      loadRepo(p);
    }
  });
  // The overlay must not start a window drag (same easy_drag handling as
  // the module picker): pressing anywhere inside it never reaches window.
  repoOverlayEl.addEventListener('mousedown', function (e) {
    e.stopPropagation();
  });
  // Drag a repo folder onto the window to load it directly.
  window.addEventListener('dragover', function (e) { e.preventDefault(); });
  window.addEventListener('drop', function (e) {
    e.preventDefault();
    var dt = e.dataTransfer;
    if (!dt || !dt.items || !dt.items.length) { return; }
    var item = dt.items[0];
    var path = null;
    try {
      var entry = item.webkitGetAsEntry && item.webkitGetAsEntry();
      if (entry && entry.fullPath) { path = entry.fullPath.replace(/^\//, ''); }
    } catch (err) { /* fall through to File */ }
    if (!path && item.getAsFile) {
      var f = item.getAsFile();
      if (f) { path = (f.path || f.webkitRelativePath || '').replace(/^\//, ''); }
    }
    if (!path) {
      openRepoPanel();
      repoStatusEl.textContent = '无法从拖放读取路径，请手动粘贴仓库路径';
      return;
    }
    openRepoPanel();
    repoPathEl.value = path;
    loadRepo(path);
  });
  window.__codemap_poll = POLL_MS;   // smoke-test probe
  window.__codemap_go = openMicro;   // smoke-test probe: jump to a module

  viewMacroBtn.addEventListener('click', loadMacro);
  viewMicroBtn.addEventListener('click', function () { openMicro(state.module); });

  // ---- module picker interactions --------------------------------------
  // Focus = open the dropdown AND select the whole pre-filled value, so the
  // user can either click/arrow/Enter (pure dropdown) or just start typing
  // (select-all makes the first keystroke replace the module name, which is
  // what turns the box into a real fuzzy search).
  var composing = false;  // true while a Chinese/IME composition is active
  moduleSearch.addEventListener('focus', function () {
    moduleSearch.select();
    showModuleList();
  });
  moduleSearch.addEventListener('compositionstart', function () {
    composing = true;
  });
  moduleSearch.addEventListener('compositionend', function () {
    composing = false;
    showModuleList();  // refresh the list with the committed text
  });
  moduleSearch.addEventListener('input', function () {
    if (composing) { return; }  // compositionend re-renders the list
    picker.active = 0;
    showModuleList();
  });
  moduleSearch.addEventListener('keydown', function (e) {
    if (composing) { return; }  // let the IME navigate/commit its candidates
    if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
      e.preventDefault();
      if (!picker.open || !picker.filtered.length) { showModuleList(); return; }
      var step = e.key === 'ArrowDown' ? 1 : -1;
      picker.active =
        (picker.active + step + picker.filtered.length) % picker.filtered.length;
      renderModuleList(moduleSearch.value);
      var activeEl = moduleListEl.querySelector('.item.active');
      if (activeEl) { activeEl.scrollIntoView({ block: 'nearest' }); }
    } else if (e.key === 'Enter') {
      e.preventDefault();
      if (picker.open && picker.filtered.length) {
        var idx = picker.active >= 0 ? picker.active : 0;
        selectModule(picker.filtered[idx].key);
      } else {
        var matches = MODULES.filter(function (m) {
          return fuzzyMatch(m.key + ' ' + m.name + ' ' + m.hay,
            normalizeQuery(moduleSearch.value));
        });
        if (matches.length) { selectModule(matches[0].key); }
      }
    } else if (e.key === 'Escape') {
      hideModuleList();
    }
  });
  // The frameless window drags through pywebview's easy_drag, which arms on
  // ANY window-level mousedown. Stop propagation from the picker container so
  // pressing the search box / dropdown never starts a window drag ("mouse
  // glued" bug) — text selection and item clicks still work, and the rest of
  // the window stays draggable.
  (moduleSearch.parentElement || moduleSearch).addEventListener(
    'mousedown', function (e) {
      e.stopPropagation();
    });
  // Clicking anywhere outside the picker closes the list.
  document.addEventListener('mousedown', function (e) {
    if (e.target !== moduleSearch && !moduleListEl.contains(e.target)) {
      hideModuleList();
    }
  });

  popMicroBtn.addEventListener('click', function () {
    var key = state.module || 'AUTH';
    setStatus('弹出微观窗口：' + key + ' …');
    api().pop_micro(key).then(function (r) {
      setStatus(r && r.error
        ? '弹出失败: ' + r.error
        : '微观图已弹出为独立窗口（点微观窗口 ✕ 收起，可再次「⧉ 弹出」）');
    }).catch(function (err) { setStatus('弹出失败: ' + err); });
  });

  zoomInBtn.addEventListener('click', function () {
    var vp = state.activeVp || vpMain;
    vp.setZoom(vp.zoom * ZOOM_STEP);
    updateZoomLabel();
  });
  zoomOutBtn.addEventListener('click', function () {
    var vp = state.activeVp || vpMain;
    vp.setZoom(vp.zoom / ZOOM_STEP);
    updateZoomLabel();
  });
  zoomResetBtn.addEventListener('click', function () {
    var vp = state.activeVp || vpMain;
    vp.setZoom(1);
    updateZoomLabel();
  });

  function closeWindow() {
    if (api()) { api().close(); }
  }
  document.getElementById('close').addEventListener('click', closeWindow);
  window.addEventListener('keydown', function (e) {
    if (e.key === 'Escape') {
      // Close the repo panel first; only quit the app when nothing else
      // is open (the panel's own ✕ / Esc affordance comes first).
      if (repoOverlayEl && !repoOverlayEl.hidden) { closeRepoPanel(); return; }
      closeWindow(); return;
    }
    if (e.ctrlKey && (e.key === '=' || e.key === '+')) {
      e.preventDefault();
      var vp = state.activeVp || vpMain;
      vp.setZoom(vp.zoom * ZOOM_STEP);
      updateZoomLabel();
    } else if (e.ctrlKey && e.key === '-') {
      e.preventDefault();
      var vp2 = state.activeVp || vpMain;
      vp2.setZoom(vp2.zoom / ZOOM_STEP);
      updateZoomLabel();
    } else if (e.ctrlKey && e.key === '0') {
      e.preventDefault();
      var vp3 = state.activeVp || vpMain;
      vp3.setZoom(1);
      updateZoomLabel();
    }
  });

  // ---- visible border resize -------------------------------------------
  // Dragging any of the 8 frame handles resizes the native window through
  // the bridge. preventDefault() on pointerdown also suppresses the
  // compatibility mousedown event, so pywebview's easy_drag (move) never
  // starts while resizing.
  var handles = document.querySelectorAll('.rh');
  var drag = null;

  function onHandleDown(e) {
    e.preventDefault();
    e.stopPropagation();
    var edges = e.target.getAttribute('data-edges');
    drag = {
      edges: edges,
      sx: e.screenX,
      sy: e.screenY,
      // JS pointer deltas are CSS px (WebView2 reports device px divided by
      // devicePixelRatio); the bridge applies physical px, so scale here to
      // keep the resize tracking the mouse 1:1 on any DPI display.
      scale: window.devicePixelRatio || 1,
      base: null
    };
    // Snapshot the native rect once per drag; every pointermove sends it
    // back so rapid async bridge calls can never compound their deltas.
    if (window.pywebview && window.pywebview.api) {
      window.pywebview.api.resize_begin().then(function (b) {
        if (drag && drag.edges === edges && b && b.width) { drag.base = b; }
      });
    }
    if (e.target.setPointerCapture) { e.target.setPointerCapture(e.pointerId); }
  }

  function onHandleMove(e) {
    if (!drag || !drag.base || !window.pywebview || !window.pywebview.api) { return; }
    var dx = (e.screenX - drag.sx) * drag.scale;
    var dy = (e.screenY - drag.sy) * drag.scale;
    window.pywebview.api.resize(drag.edges, dx, dy, drag.base);
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
    // Belt-and-suspenders: some WebView2 versions still emit the mouse event.
    h.addEventListener('mousedown', function (e) { e.stopPropagation(); });
  });
})();
