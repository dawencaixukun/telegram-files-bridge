/* =====================================================================
   app.js — theme, drawer, toast, submit modal, logout, SSE helpers
   (Alpine glue lives inline in templates; this is plain vanilla glue)
   ===================================================================== */
(function () {
  'use strict';

  var TOAST_TTL = 3800;

  /* hx-boost 下 body 内容会被整体替换，而本脚本位于 body 内、每次切页都会重新执行。
     文档级监听与一次性初始化必须只做一次，其余 window.__* 重赋值是幂等的。 */
  var firstBoot = !window.__appGlueBooted;
  window.__appGlueBooted = true;

  /* 不支持 View Transitions API 的浏览器：标记后由 CSS 走 settling 淡入兜底 */
  if (!document.startViewTransition) {
    document.documentElement.classList.add('no-vt');
  }

  /* ---------------- 页面级清理注册（boost 切页时统一执行） ----------------
     boost 之后文档不再整页刷新：旧页面的 SSE 连接、图表实例、window 监听器
     若不显式清理，会随切换次数累积泄漏。页面脚本用 __pageOnLeave 注册清理函数。 */
  window.__pageCleanups = window.__pageCleanups || [];
  window.__pageOnLeave = function (fn) {
    if (typeof fn === 'function') window.__pageCleanups.push(fn);
  };
  function runPageCleanups() {
    var list = window.__pageCleanups;
    window.__pageCleanups = [];
    for (var i = 0; i < list.length; i++) {
      try { list[i](); } catch (e) { /* 单个清理失败不阻塞切页 */ }
    }
  }


  /* ---------------- 缩略图加载失败兜底 ----------------
     /preview 代理在 TDLib 未就绪 / Java 后端离线 / 补图失败时返回 404，
     历史缺陷：<img> 没有任何失败处理，用户看到的是破图图标。
     这里做统一降级：把破图换成「扩展名占位」（与模板无图分支同款 SVG + t-ext），
     事件委托在捕获阶段监听 img error（error 不冒泡，必须用捕获），覆盖
     首渲染与 htmx swap 动态插入的所有卡片，无需每个模板单独写 onerror。 */
  function thumbFallback(img) {
    if (img.getAttribute('data-fb')) return; // 已降级过，避免循环触发
    img.setAttribute('data-fb', '1');
    var host = img.parentNode;
    if (!host) return;
    var card = img.closest('.thumb');
    var name = (card && (card.getAttribute('data-name') || card.getAttribute('data-filename'))) || '';
    var ext = name && name.indexOf('.') > -1 ? name.split('.').pop().toUpperCase() : '文件';
    var svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    svg.setAttribute('viewBox', '0 0 24 24');
    svg.setAttribute('fill', 'none');
    svg.setAttribute('stroke', 'currentColor');
    svg.setAttribute('stroke-width', '1.5');
    var use = document.createElementNS('http://www.w3.org/2000/svg', 'use');
    use.setAttribute('href', '#ic-file');
    svg.appendChild(use);
    var label = document.createElement('span');
    label.className = 't-ext';
    label.textContent = ext;
    img.style.display = 'none';
    host.appendChild(svg);
    host.appendChild(label);
  }
  window.__thumbFallback = thumbFallback;
  document.addEventListener('error', function (e) {
    var t = e.target;
    if (t && t.tagName === 'IMG' && t.closest('.t-img')) thumbFallback(t);
  }, true);

  /* ---------------- CSRF (double-submit) ---------------- */
  function csrfToken() {
    var m = document.cookie.match(/(?:^|;\s*)tf_portal_csrf=([^;]*)/);
    return m ? decodeURIComponent(m[1]) : '';
  }
  function postJSON(url, body) {
    return fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrfToken() },
      body: JSON.stringify(body)
    });
  }
  window.__csrfToken = csrfToken;

  /* ---------------- Theme ---------------- */
  function currentTheme() {
    return document.documentElement.getAttribute('data-theme') || 'dark';
  }
  function applyTheme(t) {
    document.documentElement.setAttribute('data-theme', t);
    try { localStorage.setItem('tg-archive-theme', t); } catch (e) {}
    var ic = document.getElementById('themeIcon');
    if (ic) {
      ic.setAttribute('href', t === 'light' ? '#ic-moon' : '#ic-sun');
    }
    // notify pages (charts re-render) without a full reload
    try { window.dispatchEvent(new CustomEvent('themechange', { detail: t })); } catch (e) {}
  }
  function prefersReducedMotion() {
    try { return window.matchMedia('(prefers-reduced-motion: reduce)').matches; } catch (e) { return false; }
  }
  /* 主题圆形揭示：以主题按钮为圆心，新主题自点击处涟漪扩散铺满整页（View Transitions）。
     #content 有独立的 view-transition-name（导航滑动动画用），主题切换需要整页
     参与同一个过渡组，故切换期间临时摘掉命名，结束后恢复。 */
  function themeReveal(t) {
    var root = document.documentElement;
    var content = document.getElementById('content');
    var btn = document.getElementById('themeBtn');
    var r = btn && btn.getBoundingClientRect ? btn.getBoundingClientRect() : null;
    var cx = r ? r.left + r.width / 2 : window.innerWidth - 40;
    var cy = r ? r.top + r.height / 2 : 40;
    var radius = Math.hypot(Math.max(cx, window.innerWidth - cx), Math.max(cy, window.innerHeight - cy)) + 24;
    var restore = function () {
      if (content) content.style.viewTransitionName = '';
    };
    if (content) content.style.viewTransitionName = 'none';
    var vt = null;
    try {
      vt = document.startViewTransition(function () { applyTheme(t); });
    } catch (e) {
      restore();
      applyTheme(t);
      return;
    }
    vt.ready.then(function () {
      root.animate(
        { clipPath: [
            'circle(0px at ' + cx + 'px ' + cy + 'px)',
            'circle(' + radius + 'px at ' + cx + 'px ' + cy + 'px)'
          ] },
        { duration: 460, easing: 'ease-in-out', pseudoElement: '::view-transition-new(root)' }
      );
    }, function () { /* 过渡被新过渡顶替时 ready 会拒绝，忽略即可 */ });
    vt.finished.then(restore, restore);
  }
  window.__toggleTheme = function () {
    var t = currentTheme() === 'dark' ? 'light' : 'dark';
    if (document.startViewTransition && !prefersReducedMotion()) {
      themeReveal(t);
    } else if (prefersReducedMotion()) {
      applyTheme(t);   // 减弱动态效果：瞬时切换
    } else {
      // 无 View Transitions 的浏览器：全站色彩属性整体淡变兜底（CSS .theme-anim）
      var rootEl = document.documentElement;
      rootEl.classList.add('theme-anim');
      applyTheme(t);
      setTimeout(function () { rootEl.classList.remove('theme-anim'); }, 520);
    }
  };
  // sync icon on load (首次加载时；boost 重执行无需重复)
  if (firstBoot) applyTheme(currentTheme());

  /* ---------------- htmx 生命周期 ---------------- */
  if (firstBoot && typeof document !== 'undefined') {
    // 离开当前页面前：断开 SSE、销毁图表等（注册表见文件头）
    document.addEventListener('htmx:beforeSwap', runPageCleanups);
    document.addEventListener('htmx:afterSwap', function () {
      if (document.body) {
        document.body.style.overflow = '';
        document.body.classList.remove('drawer-open');
      }
      var ic = document.getElementById('themeIcon');
      if (ic) ic.setAttribute('href', currentTheme() === 'light' ? '#ic-moon' : '#ic-sun');
    });
  }

  /* ---------------- Drawer (mobile) ---------------- */
  window.__toggleDrawer = function () {
    document.body.classList.toggle('drawer-open');
  };
  window.__closeDrawer = function () {
    document.body.classList.remove('drawer-open');
  };

  /* ---------------- Toast ---------------- */
  function toast(msg, type, sub, ttl) {
    var stack = document.getElementById('toastStack');
    if (!stack) return;
    var el = document.createElement('div');
    el.className = 'toast ' + (type || 'success');
    var icon = type === 'error' ? 'ic-alert' : (type === 'info' ? 'ic-info' : 'ic-check');
    // msg/sub 来自后端错误文本（不可信），必须用 textContent 注入，不能 innerHTML
    var box = document.createElement('div');
    var line = document.createElement('div');
    line.className = 't-msg';
    line.textContent = msg;
    box.appendChild(line);
    if (sub) {
      var subEl = document.createElement('div');
      subEl.className = 't-sub';
      subEl.textContent = sub;
      box.appendChild(subEl);
    }
    var svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    svg.setAttribute('class', 't-icon');
    svg.setAttribute('viewBox', '0 0 24 24');
    svg.setAttribute('fill', 'none');
    svg.setAttribute('stroke', 'currentColor');
    svg.setAttribute('stroke-width', '2');
    svg.setAttribute('stroke-linecap', 'round');
    svg.setAttribute('stroke-linejoin', 'round');
    var use = document.createElementNS('http://www.w3.org/2000/svg', 'use');
    use.setAttribute('href', '#' + icon);
    svg.appendChild(use);
    el.appendChild(svg);
    el.appendChild(box);
    stack.appendChild(el);
    requestAnimationFrame(function () { el.classList.add('in'); });
    setTimeout(function () {
      el.classList.remove('in');
      setTimeout(function () { el.remove(); }, 300);
    }, ttl || TOAST_TTL);
  }
  window.__toast = toast;

  /* ---------------- Submit modal ---------------- */
  var submitBackdrop = null;
  window.__openSubmit = function () {
    var b = document.getElementById('submitBackdrop');
    if (b) { b.classList.add('show'); document.body.style.overflow = 'hidden'; }
  };
  window.__closeSubmit = function () {
    var b = document.getElementById('submitBackdrop');
    if (b) { b.classList.remove('show'); document.body.style.overflow = ''; }
  };
  // wire escape key to close modal/dropdowns
  if (firstBoot) {
    document.addEventListener('keydown', function (e) {
      if (e.key === 'Escape') { window.__closeSubmit(); window.__closeArchive(); window.__closeQuickDl(); window.__closeDrawer(); }
    });
    // allow backdrop click to close
    document.addEventListener('click', function (e) {
      if (e.target && e.target.id === 'submitBackdrop') window.__closeSubmit();
      if (e.target && e.target.id === 'archiveBackdrop') window.__closeArchive();
      if (e.target && e.target.id === 'quickDlBackdrop') window.__closeQuickDl();
    });
  }

  /* ---------------- Submit modal (real fetch) ---------------- */
  window.__submitLinks = function (evt) {
    evt.preventDefault();
    // 同一 form 上 htmx 也监听 submit（hx-trigger="submit from:#submitForm"），
    // 这里阻断后续监听器，避免与真实 fetch 双重提交
    if (evt && evt.stopImmediatePropagation) evt.stopImmediatePropagation();
    var ta = document.getElementById('submitLinks');
    var links = [];
    if (ta) {
      links = ta.value.split(/\n+/).map(function (l) { return l.trim(); }).filter(function (l) { return l; });
    }
    if (!links.length) {
      toast('没有可提交的链接', 'error');
      return;
    }
    postJSON('/tasks', { links: links }).then(function (r) { return r.json(); }).then(function (d) {
      if (d && d.ok) {
        window.__closeSubmit();
        if (ta) ta.value = '';
        toast('已提交 ' + links.length + ' 条链接', 'success', '已加入下载队列');
      } else {
        toast('提交失败', 'error', (d && d.message) || '后端返回错误');
      }
      return null;
    }).catch(function (e) {
      toast('提交失败', 'error', '网络错误');
      return null;
    });
  };

  /* ---------------- Quick Link Resolve & Direct Download ---------------- */
  var currentQuickData = null;

  window.__openQuickDl = function () {
    var b = document.getElementById('quickDlBackdrop');
    if (b) { b.classList.add('show'); document.body.style.overflow = 'hidden'; }
  };

  window.__closeQuickDl = function () {
    var b = document.getElementById('quickDlBackdrop');
    if (b) { b.classList.remove('show'); document.body.style.overflow = ''; }
    currentQuickData = null;
  };

  window.__toggleQuickArchiveOptions = function () {
    var chk = document.getElementById('quickAutoArchive');
    var wrap = document.getElementById('quickDirWrap');
    if (chk && wrap) {
      wrap.style.display = chk.checked ? 'block' : 'none';
    }
  };

  window.__resolveQuickLink = function () {
    var inp = document.getElementById('quickLinkInput');
    var val = (inp && inp.value ? inp.value.trim() : '');
    if (!val) {
      toast('请输入 Telegram 消息链接', 'error');
      if (inp) inp.focus();
      return;
    }

    var btn = document.getElementById('quickResolveBtn');
    var spinner = document.getElementById('quickResolveSpinner');
    var btnText = btn ? btn.querySelector('.quick-btn-text') : null;
    if (btn) btn.disabled = true;
    if (spinner) spinner.style.display = 'inline-block';
    if (btnText) btnText.style.display = 'none';

    postJSON('/api/tg/resolve-link', { link: val })
      .then(function (r) { return r.json(); })
      .then(function (d) {
        if (!d || !d.ok) {
          toast('解析失败', 'error', (d && d.message) || '无法解析该链接');
          return;
        }
        window.__openQuickDlModal(d.data);
      })
      .catch(function (err) {
        toast('解析失败', 'error', '网络异常或服务暂不可用');
      })
      .finally(function () {
        if (btn) btn.disabled = false;
        if (spinner) spinner.style.display = 'none';
        if (btnText) btnText.style.display = 'inline';
      });
  };

  window.__openQuickDlModal = function (data) {
    if (!data || !data.files || !data.files.length) {
      toast('未找到媒体', 'error', '该消息未包含有效媒体文件');
      return;
    }
    currentQuickData = data;
    var f0 = data.files[0];

    var titleEl = document.getElementById('quickMediaTitle');
    var sizeEl = document.getElementById('quickMetaSize');
    var typeEl = document.getElementById('quickMetaType');
    var chatEl = document.getElementById('quickMetaChat');
    var msgEl = document.getElementById('quickMetaMsg');
    var bannerEl = document.getElementById('quickStatusBanner');

    if (titleEl) titleEl.textContent = f0.filename || '未命名媒体文件';
    if (sizeEl) sizeEl.textContent = f0.sizeHuman || '未知大小';
    if (typeEl) typeEl.textContent = (f0.fileType || 'file').toUpperCase();
    if (chatEl) chatEl.textContent = f0.chatTitle || ('Chat #' + f0.chatId);
    if (msgEl) msgEl.textContent = '#' + (f0.messageId || data.messageId);

    if (bannerEl) {
      if (f0.isAlreadyArchived) {
        bannerEl.style.display = 'block';
        bannerEl.className = 'quick-status-banner warning';
        bannerEl.innerHTML = '⚠️ <b>提示：</b>该文件已在 OpenList 云端归档库中存在。直投仍可重新下载。';
      } else if (f0.isAlreadyDownloaded) {
        bannerEl.style.display = 'block';
        bannerEl.className = 'quick-status-banner info';
        bannerEl.innerHTML = 'ℹ️ <b>提示：</b>该文件在本地在存库中已下载完成。直投将重新入队校验。';
      } else {
        bannerEl.style.display = 'none';
        bannerEl.innerHTML = '';
      }
    }

    // 默认云端目录记忆
    var dirInp = document.getElementById('quickArchiveDir');
    var savedDir = localStorage.getItem('tg-archive-last-dir');
    if (dirInp && savedDir) {
      dirInp.value = savedDir;
    }

    window.__openQuickDl();
  };

  window.__confirmQuickDownload = function () {
    if (!currentQuickData) {
      toast('无法提交', 'error', '解析数据已失效，请重新解析');
      window.__closeQuickDl();
      return;
    }

    var chk = document.getElementById('quickAutoArchive');
    var autoArchive = chk ? chk.checked : true;
    var dirInp = document.getElementById('quickArchiveDir');
    var archiveDir = dirInp ? dirInp.value.trim() : '/阿里云盘/tg-archive/';
    if (autoArchive && archiveDir) {
      localStorage.setItem('tg-archive-last-dir', archiveDir);
    }

    var startBtn = document.getElementById('quickStartDlBtn');
    if (startBtn) startBtn.disabled = true;

    postJSON('/api/tg/quick-download', {
      link: currentQuickData.link,
      files: currentQuickData.files,
      autoArchive: autoArchive,
      archiveDir: archiveDir
    })
      .then(function (r) { return r.json(); })
      .then(function (d) {
        if (d && d.ok) {
          window.__closeQuickDl();
          var inp = document.getElementById('quickLinkInput');
          if (inp) inp.value = '';
          toast('直投下载成功', 'success', '已加入下载队列' + (autoArchive ? '，完成后自动归档' : ''));
        } else {
          toast('直投失败', 'error', (d && d.message) || '提交失败');
        }
      })
      .catch(function (err) {
        toast('提交失败', 'error', '网络或服务端异常');
      })
      .finally(function () {
        if (startBtn) startBtn.disabled = false;
      });
  };

  /* ---------------- Archive modal (本地在存 → OpenList 云端归档) ----------------
     弹窗 partial: templates/partials/_archive_modal.html
     目标目录经 /openlist/dirs 逐级浏览（多网盘），默认记忆上次选择；
     提交 POST /archive/start（CSRF）；进度轮询 /archive/status。 */
  var archItem = null;        // 当前弹窗主文件 {uniqueId, filename, size, source}
  var archItems = [];         // 批量归档文件列表 [{uniqueId, filename, size, source}]
  var archDir = '';           // 已选定的目标目录（提交用）
  var archBrowsePath = '/';   // 目录浏览器当前所在路径
  var ARCH_DIR_KEY = 'tg-archive-last-dir';
  // 版本化键名：旧版本的键会在每次提交时无条件写入 '0'（复选框从未回填过），
  // 属于假记忆，不能当作「用户主动选择」；换键后旧值自动失效。
  var ARCH_DEL_LOCAL_KEY = 'tg-archive-del-local-v2';
  // 用户是否在归档弹窗内主动改过「删除本地原文件」勾选。
  // 未主动选择时提交体不带 deleteLocal，交由后端回落到设置页全局开关，
  // 避免弹窗复选框默认未勾导致「设置里已开启却仍保留本地文件」。
  var archDelLocalExplicit = false;

  window.__archDelLocalTouch = function (el) {
    archDelLocalExplicit = true;
    try { localStorage.setItem(ARCH_DEL_LOCAL_KEY, (el && el.checked) ? '1' : '0'); } catch (e) {}
  };

  function archSetLoginLine(ok, msg) {
    var el = document.getElementById('archLoginState');
    if (!el) return;
    el.textContent = ok ? '✓ OpenList 已登录，可以开始归档' : ('✗ ' + (msg || 'OpenList 未登录，请先到设置页登录'));
    el.style.color = ok ? 'var(--ok)' : 'var(--err)';
  }

  window.__openArchive = function (target) {
    if (!target) return;
    if (Array.isArray(target)) {
      archItems = target.filter(function (it) { return it && it.uniqueId; });
      archItem = archItems[0] || null;
    } else if (target.getAttribute) {
      var uid = target.getAttribute('data-unique-id');
      if (!uid) { window.__toast('该记录缺少 uniqueId，无法归档', 'error'); return; }
      archItem = {
        uniqueId: uid,
        filename: target.getAttribute('data-filename') || '',
        size: target.getAttribute('data-size') || '',
        source: target.getAttribute('data-source') || ''
      };
      archItems = [archItem];
    } else if (target.uniqueId) {
      archItem = target;
      archItems = [target];
    }

    if (!archItems.length) {
      window.__toast('没有可归档的文件', 'error');
      return;
    }

    var nameEl = document.getElementById('archFileName');
    var metaEl = document.getElementById('archFileMeta');
    if (archItems.length === 1) {
      if (nameEl) nameEl.textContent = archItem.filename || ('文件 ' + archItem.uniqueId);
      if (metaEl) metaEl.textContent = [archItem.size, archItem.source].filter(Boolean).join(' · ');
    } else {
      if (nameEl) nameEl.textContent = '批量归档 ' + archItems.length + ' 个文件';
      if (metaEl) {
        var names = archItems.map(function (it) { return it.filename; }).filter(Boolean).slice(0, 3);
        metaEl.textContent = '包括：' + names.join('、') + (archItems.length > 3 ? ' 等' : '');
      }
    }

    var delLocalEl = document.getElementById('archDeleteLocal');
    if (delLocalEl) {
      archDelLocalExplicit = false;
      var storedDel = null;
      try {
        storedDel = localStorage.getItem(ARCH_DEL_LOCAL_KEY);
      } catch (e) {
        storedDel = null;
      }
      // 先按弹窗内上次的选择回填；从未在弹窗里选过（无记忆）时，以设置页
      // 全局开关「归档成功后自动删除本地文件」为准，避免设置里已开启、
      // 手动归档却因复选框默认不勾而静默保留本地文件。
      delLocalEl.checked = (storedDel === '1');
      // 有历史记忆 = 用户曾在弹窗里明确选择过，提交时显式携带；
      // 无记忆则提交体不带该字段，由后端回落设置页全局开关。
      archDelLocalExplicit = (storedDel !== null);
      if (storedDel === null) {
        fetch('/archive/config', { headers: { 'Accept': 'application/json' } })
          .then(function (r) { return r.json(); })
          .then(function (d) {
            if (!d || !d.ok || !d.config || !delLocalEl) return null;
            var now = null;
            try { now = localStorage.getItem(ARCH_DEL_LOCAL_KEY); } catch (e) { now = null; }
            // 仅当用户在本次弹窗打开后仍未做过选择时才回填，避免覆盖用户刚改的勾选
            if (now === null) delLocalEl.checked = !!d.config.deleteLocal;
            return null;
          })
          .catch(function () { /* 读取失败则保持未勾选，提交时后端仍有全局兜底 */ });
      }
    }

    var last = '';
    try { last = localStorage.getItem(ARCH_DIR_KEY) || ''; } catch (e) {}
    if (!last && window.__DEFAULT_ARCH_DIR) {
      last = window.__DEFAULT_ARCH_DIR;
    }
    archDir = last;
    var input = document.getElementById('archDirInput');
    if (input) input.value = last;
    archSetLoginLine(true, '');  // 先复位再异步检查
    fetch('/openlist/status', { headers: { 'Accept': 'application/json' } })
      .then(function (r) { return r.json(); })
      .then(function (d) {
        var ok = !!(d && d.ok && d.loggedIn && d.verified);
        archSetLoginLine(ok, ok ? '' : ((d && d.message) || 'OpenList 未登录，请先到设置页登录'));
        return null;
      })
      .catch(function () { archSetLoginLine(false, '无法连接服务，请稍后重试'); });
    var b = document.getElementById('archiveBackdrop');
    if (b) { b.classList.add('show'); document.body.style.overflow = 'hidden'; }
    // 弹窗打开时下拉保持初始闭合，预设好已选路径供展开时直达
    window.__archDropdownClose();
  };

  window.__closeArchive = function () {
    window.__archDropdownClose();
    var b = document.getElementById('archiveBackdrop');
    if (b) { b.classList.remove('show'); document.body.style.overflow = ''; }
  };

  function archSetArrow(open) {
    var a = document.getElementById('archDirArrow');
    if (a) a.classList.toggle('open', !!open);
  }
  window.__archDropdownClose = function () {
    var d = document.getElementById('archDropdown');
    if (d) d.style.display = 'none';
    archSetArrow(false);
  };
  // 展开下拉并加载目录（优先停在已选目录，与输入框内容一致；无则从根开始）
  window.__archOpen = function () {
    var d = document.getElementById('archDropdown');
    if (!d) return;
    d.style.display = 'block';
    archSetArrow(true);
    window.__archLoad(archDir || archBrowsePath || '/');
  };
  window.__archToggle = function () {
    var d = document.getElementById('archDropdown');
    if (!d) return;
    if (d.style.display === 'none' || !d.style.display) {
      window.__archOpen();
    } else {
      window.__archDropdownClose();
    }
  };

  // 点击下拉/输入框以外区域自动收起下拉（防范节点移出 DOM 后误判）
  if (!window.__archDropdownClickBound) {
    window.__archDropdownClickBound = true;
    document.addEventListener('click', function (e) {
      if (!window.__archDropdownClose) return;
      var d = document.getElementById('archDropdown');
      if (!d || d.style.display === 'none') return;
      var path = e.composedPath ? e.composedPath() : [];
      for (var i = 0; i < path.length; i++) {
        var el = path[i];
        if (el.classList && (el.classList.contains('arch-dir-field') || el.classList.contains('arch-dropdown'))) {
          return;
        }
      }
      var field = e.target && e.target.closest && e.target.closest('.arch-dir-field');
      if (!field) {
        window.__archDropdownClose();
      }
    });
  }

  // 加载某目录的子目录列表（根 "/" 起逐级进入各挂载网盘）
  var archLoadSeq = 0;
  window.__archLoad = function (path) {
    var list = document.getElementById('archDirList');
    var crumb = document.getElementById('archCrumb');
    if (!list || !crumb) return;
    archBrowsePath = path || '/';
    var curSeq = ++archLoadSeq;
    // 面包屑（文本注入一律 textContent，杜绝路径里的 HTML 逃逸）
    while (crumb.firstChild) crumb.removeChild(crumb.firstChild);
    (function buildCrumb() {
      var segs = (archBrowsePath || '/').split('/').filter(Boolean);
      var acc = '';
      function add(label, fullPath, isLast) {
        var a = document.createElement('a');
        a.className = isLast ? 'crumb-link cur' : 'crumb-link';
        a.textContent = label;
        if (!isLast) {
          a.setAttribute('href', 'javascript:void(0)');
          a.addEventListener('click', function (e) {
            e.preventDefault();
            e.stopPropagation();
            window.__archLoad(fullPath);
          });
        }
        crumb.appendChild(a);
        if (!isLast) {
          var sep = document.createElement('span');
          sep.className = 'crumb-sep';
          sep.textContent = '/';
          crumb.appendChild(sep);
        }
      }
      add('根目录', '/', segs.length === 0);
      for (var i = 0; i < segs.length; i++) {
        acc += '/' + segs[i];
        add(segs[i], acc, i === segs.length - 1);
      }
    })();
    list.innerHTML = '<div style="padding:10px 12px;color:var(--text-3);font-size:12px;">正在读取目录…</div>';
    var hint = document.getElementById('archEmptyHint');
    if (hint) hint.style.display = 'none';
    var pickBtn = document.querySelector('#archDropdown .arch-drop-foot button');
    if (pickBtn) {
      if (archBrowsePath === '/' || !archBrowsePath) {
        pickBtn.disabled = true;
        pickBtn.title = '根目录不可直接归档，请点击进入具体网盘';
        pickBtn.innerHTML = '<svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><use href="#ic-chevron"/></svg> 请选择网盘/文件夹';
      } else {
        pickBtn.disabled = false;
        pickBtn.title = '选定 ' + archBrowsePath;
        pickBtn.innerHTML = '<svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><use href="#ic-check"/></svg> 使用当前目录';
      }
    }
    function renderErr(msg) {
      list.textContent = '';
      var errRow = document.createElement('div');
      errRow.style.padding = '10px';
      errRow.style.color = 'var(--err)';
      errRow.style.fontSize = '12px';
      errRow.textContent = msg;
      list.appendChild(errRow);
    }
    fetch('/openlist/dirs?path=' + encodeURIComponent(archBrowsePath))
      .then(function (r) { return r.json(); })
      .then(function (d) {
        if (curSeq !== archLoadSeq) return null;
        list.textContent = '';
        if (!d || !d.ok) { renderErr((d && d.message) || '读取目录失败'); return null; }
        var dirs = d.dirs || [];
        // 非根目录时，第一行始终显示显眼的「返回根目录 / 上一级」入口
        if (archBrowsePath && archBrowsePath !== '/') {
          var pSegs = archBrowsePath.split('/').filter(Boolean);
          pSegs.pop();
          var pPath = pSegs.length ? ('/' + pSegs.join('/')) : '/';
          var upRow = document.createElement('div');
          upRow.className = 'arch-dir-row arch-up-row';
          upRow.style.cssText = 'color:var(--brand);font-weight:600;background:rgba(124,58,237,.08);margin-bottom:4px;';
          var upSvg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
          upSvg.setAttribute('viewBox', '0 0 24 24'); upSvg.setAttribute('fill', 'none');
          upSvg.setAttribute('stroke', 'currentColor'); upSvg.setAttribute('stroke-width', '2');
          var upUse = document.createElementNS('http://www.w3.org/2000/svg', 'use');
          upUse.setAttribute('href', '#ic-chevron');
          upSvg.appendChild(upUse);
          upSvg.style.transform = 'rotate(90deg)';
          upRow.appendChild(upSvg);
          var upName = document.createElement('span');
          upName.className = 'd-name';
          upName.textContent = pPath === '/' ? '⮬ 返回根目录 (切换其他网盘)' : '⮬ 返回上一级';
          upRow.appendChild(upName);
          upRow.addEventListener('click', function (e) {
            e.preventDefault();
            e.stopPropagation();
            window.__archLoad(pPath);
          });
          list.appendChild(upRow);
        }
        if (!dirs.length && hint) hint.style.display = '';
        dirs.forEach(function (dir) {
          var row = document.createElement('div');
          row.className = 'arch-dir-row';
          var svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
          svg.setAttribute('viewBox', '0 0 24 24'); svg.setAttribute('fill', 'none');
          svg.setAttribute('stroke', 'currentColor'); svg.setAttribute('stroke-width', '2');
          var use = document.createElementNS('http://www.w3.org/2000/svg', 'use');
          use.setAttribute('href', '#ic-folder');
          svg.appendChild(use);
          row.appendChild(svg);
          var name = document.createElement('span');
          name.className = 'd-name';
          name.textContent = dir.name;
          row.appendChild(name);
          row.addEventListener('click', function (e) {
            e.preventDefault();
            e.stopPropagation();
            window.__archLoad(dir.path);
          });
          list.appendChild(row);
        });
        return null;
      })
      .catch(function () {
        if (curSeq === archLoadSeq) renderErr('网络错误，读取目录失败');
      });
  };

  window.__archPick = function () {
    archDir = archBrowsePath || '/';
    var input = document.getElementById('archDirInput');
    if (input) input.value = archDir;
    try { localStorage.setItem(ARCH_DIR_KEY, archDir); } catch (e) {}
    window.__archDropdownClose();
  };

  window.__startArchive = function (evt) {
    evt.preventDefault();
    if (!archItems || !archItems.length) {
      window.__toast('没有可归档的文件', 'error');
      return;
    }
    var dir = (archDir || '').trim();
    if (!dir || dir === '/' || dir.charAt(0) !== '/') {
      window.__toast('请先选择目标目录', 'error');
      return;
    }
    var policyEl = document.querySelector('input[name="archPolicy"]:checked');
    var policy = policyEl ? policyEl.value : 'overwrite';
    var delLocalEl = document.getElementById('archDeleteLocal');
    var deleteLocal = !!(delLocalEl && delLocalEl.checked);
    // 用户在弹窗内未主动选择过时，不显式发送 deleteLocal，交由后端回落设置页全局开关。

    var uids = archItems.map(function (it) { return it.uniqueId; }).filter(Boolean);
    if (!uids.length) {
      window.__toast('没有有效的文件 ID', 'error');
      return;
    }
    var submitBtn = evt.target.querySelector('button[type="submit"]');
    if (submitBtn) submitBtn.disabled = true;

    var archBody = {
      uniqueIds: uids,
      remoteDir: dir,
      policy: policy
    };
    if (archDelLocalExplicit) archBody.deleteLocal = deleteLocal;

    postJSON('/archive/start', archBody).then(function (r) { return r.json(); }).then(function (d) {
      if (d && d.ok) {
        window.__closeArchive();
        var label = uids.length === 1 ? (archItems[0].filename || '文件') : ('批量 ' + uids.length + ' 个文件');
        window.__toast('已加入归档队列', 'success', label + ' → ' + dir + (deleteLocal ? '（归档后将自动删除本地）' : ''));
        if (window.__localClearSelection) window.__localClearSelection();
        __archPollStart();
      } else {
        window.__toast('归档提交失败', 'error', (d && d.message) || '后端返回错误');
      }
      if (submitBtn) submitBtn.disabled = false;
      return null;
    }).catch(function () {
      if (submitBtn) submitBtn.disabled = false;
      window.__toast('归档提交失败', 'error', '网络错误');
    });
  };

  /* ---------------- 归档进度轮询 ---------------- */
  var archTimer = null;
  var archIdleCount = 0;
  function archApplyButton(btn, j) {
    var st = j.state;
    if (st === 'uploading') { btn.textContent = '归档中 ' + (j.progress || 0) + '%'; btn.disabled = true; btn.title = j.remotePath || ''; }
    else if (st === 'queued') { btn.textContent = '排队中…'; btn.disabled = true; btn.title = j.remotePath || ''; }
    else if (st === 'done') {
      if (j.localDeleted) {
        btn.textContent = '已归档 (本地已删)';
        btn.disabled = true;
        btn.title = '已归档到 ' + (j.remotePath || '') + '，本地原文件已释放';
      } else if (j.deleteLocal) {
        // 请求了删本地但任务记录里没有 local_deleted：删除失败或尚未执行，
        // 不能宣称「本地已删」，保留按钮让用户可重试。
        btn.textContent = '已归档 (本地待清理)';
        btn.disabled = false;
        btn.title = '已归档到 ' + (j.remotePath || '') +
          (j.localDeleteError ? ('，删除本地失败：' + j.localDeleteError)
                              : '，本地文件尚未确认删除') + '，点击可重试';
      } else {
        btn.textContent = '重新归档';
        btn.disabled = false;
        btn.title = '已归档到 ' + (j.remotePath || '');
      }
    }
    else if (st === 'failed') { btn.textContent = '归档失败 · 重试'; btn.disabled = false; btn.title = j.error || '归档失败，点击重试'; }
    else if (st === 'cancelled') { btn.textContent = '重新归档'; btn.disabled = false; btn.title = '上次已取消，点击重新归档'; }
  }
  function archApplyPill(p, j) {
    p.className = 'pill ' + (j.pillCls || 'pending');
    while (p.firstChild) p.removeChild(p.firstChild);
    var dot = document.createElement('span');
    dot.className = 'p-dot';
    p.appendChild(dot);
    var label = document.createElement('span');
    label.textContent = j.pillLabel || '';
    p.appendChild(label);
  }
  function __archPollTick() {
    fetch('/archive/status', { headers: { 'Accept': 'application/json' } })
      .then(function (r) { return r.json(); })
      .then(function (d) {
        if (!d || !d.ok) return null;
        var jobs = d.jobs || [];
        var byUid = {};
        var active = false;
        jobs.forEach(function (j) {
          if (!j || !j.uniqueId) return;
          var cur = byUid[j.uniqueId];
          // 比较 createdAt，保留该文件最新的一条任务，绝不让历史旧任务覆盖新任务
          if (!cur || (j.createdAt || 0) > (cur.createdAt || 0)) {
            byUid[j.uniqueId] = j;
          }
          if (j.state === 'queued' || j.state === 'uploading') active = true;
        });
        var btns = document.querySelectorAll('[data-arch-btn][data-unique-id]');
        Array.prototype.forEach.call(btns, function (btn) {
          var j = byUid[btn.getAttribute('data-unique-id')];
          if (j) archApplyButton(btn, j);
        });
        var pills = document.querySelectorAll('[data-arch-pill][data-unique-id]');
        Array.prototype.forEach.call(pills, function (p) {
          var j = byUid[p.getAttribute('data-unique-id')];
          if (j) archApplyPill(p, j);
        });
        if (active) {
          archIdleCount = 0;
        } else {
          archIdleCount++;
          if (archIdleCount >= 3 && archTimer) {
            clearInterval(archTimer);
            archTimer = null;
            archIdleCount = 0;
          }
        }
        return null;
      })
      .catch(function () { /* 网络抖动忽略，下个周期再试 */ });
  }
  function __archPollStart() {
    archIdleCount = 0;
    if (!archTimer) {
      archTimer = setInterval(__archPollTick, 2000);
    }
    __archPollTick();
  }
  window.__archPollStart = __archPollStart;
  // boost 切页离开时停掉轮询，避免长寿命文档里定时器泄漏
  window.__pageOnLeave(function () {
    if (archTimer) { clearInterval(archTimer); archTimer = null; }
  });
  // 归档胶水版本标记：与 _archive_modal.html 的自愈守卫对账，
  // 旧缓存 JS 会触发一次整页刷新拉新版本
  window.__ARCH_GLUE = 'v20260905a';

  /* ---------------- 本地在存增强：多选、批量归档、单项/批量删除 ---------------- */
  function safeSelectorVal(val) {
    if (window.CSS && typeof CSS.escape === 'function') {
      return CSS.escape(val);
    }
    return String(val).replace(/["\\]/g, '\\$&');
  }

  function removeLocalFileFromDom(uid) {
    var items = document.querySelectorAll('[data-unique-id="' + safeSelectorVal(uid) + '"]');
    Array.prototype.forEach.call(items, function (el) {
      if (el.tagName === 'TR') {
        el.remove();
      } else if (el.classList && el.classList.contains('thumb')) {
        el.remove();
      } else {
        var c = el.closest('tr, .thumb');
        if (c) c.remove();
      }
    });
  }

  function reloadLocalFiles() {
    var form = document.querySelector('form[hx-get="/partials/local-files"]');
    if (form && window.htmx) {
      htmx.trigger(form, 'change');
    }
  }

  function getCheckedUniqueIds() {
    var checked = document.querySelectorAll('.local-check:checked');
    var seen = {};
    var uids = [];
    Array.prototype.forEach.call(checked, function (c) {
      var uid = c.getAttribute('data-unique-id');
      if (uid && !seen[uid]) {
        seen[uid] = true;
        uids.push(uid);
      }
    });
    return uids;
  }

  window.__localCheckChange = function (cb) {
    var uid = cb.getAttribute('data-unique-id');
    var isChecked = !!cb.checked;
    if (uid) {
      var allWithUid = document.querySelectorAll('.local-check[data-unique-id="' + safeSelectorVal(uid) + '"]');
      Array.prototype.forEach.call(allWithUid, function (other) {
        if (other !== cb) other.checked = isChecked;
        var parent = other.closest('.thumb, tr');
        if (parent) parent.classList.toggle('selected', isChecked);
      });
    }
    var currentParent = cb.closest('.thumb, tr');
    if (currentParent) currentParent.classList.toggle('selected', isChecked);
    window.__localUpdateSelection();
  };

  window.__localUpdateSelection = function () {
    var uids = getCheckedUniqueIds();
    var count = uids.length;

    var allChecks = document.querySelectorAll('.local-check');
    var allUidsSeen = {};
    Array.prototype.forEach.call(allChecks, function (c) {
      var u = c.getAttribute('data-unique-id');
      if (u) allUidsSeen[u] = true;
    });
    var totalUniqueFiles = Object.keys(allUidsSeen).length;

    var bar = document.getElementById('localBatchBar');
    var countEl = document.getElementById('localSelectedCount');
    if (countEl) countEl.textContent = count;
    if (bar) bar.style.display = count > 0 ? '' : 'none';

    var isAll = totalUniqueFiles > 0 && count === totalUniqueFiles;
    var isIndet = count > 0 && count < totalUniqueFiles;
    var top = document.getElementById('localSelAllTop');
    var tbl = document.getElementById('localSelAllTable');
    [top, tbl].forEach(function (el) {
      if (el) {
        el.checked = isAll;
        el.indeterminate = isIndet;
      }
    });
  };

  window.__localSelAll = function (sourceCheckbox) {
    var isChecked = !!(sourceCheckbox && sourceCheckbox.checked);
    var checks = document.querySelectorAll('.local-check');
    Array.prototype.forEach.call(checks, function (c) {
      c.checked = isChecked;
      var parent = c.closest('.thumb, tr');
      if (parent) parent.classList.toggle('selected', isChecked);
    });
    var top = document.getElementById('localSelAllTop');
    var tbl = document.getElementById('localSelAllTable');
    if (top && top !== sourceCheckbox) { top.checked = isChecked; top.indeterminate = false; }
    if (tbl && tbl !== sourceCheckbox) { tbl.checked = isChecked; tbl.indeterminate = false; }
    window.__localUpdateSelection();
  };

  window.__localClearSelection = function () {
    var checks = document.querySelectorAll('.local-check');
    Array.prototype.forEach.call(checks, function (c) {
      c.checked = false;
      var parent = c.closest('.thumb, tr');
      if (parent) parent.classList.remove('selected');
    });
    var top = document.getElementById('localSelAllTop');
    var tbl = document.getElementById('localSelAllTable');
    if (top) { top.checked = false; top.indeterminate = false; }
    if (tbl) { tbl.checked = false; tbl.indeterminate = false; }
    window.__localUpdateSelection();
  };

  window.__localCardClick = function (evt, container) {
    if (evt.target.closest('button, a, label, input')) return;
    var cb = container.querySelector('.local-check');
    if (cb) {
      cb.checked = !cb.checked;
      window.__localCheckChange(cb);
    }
  };

  window.__bulkArchiveLocal = function () {
    var checked = document.querySelectorAll('.local-check:checked');
    if (!checked.length) {
      window.__toast('请先勾选要归档的文件', 'error');
      return;
    }
    var seen = {};
    var items = [];
    var unarchivable = 0;
    Array.prototype.forEach.call(checked, function (c) {
      var uid = c.getAttribute('data-unique-id');
      if (!uid || seen[uid]) return;
      seen[uid] = true;
      var fn = c.getAttribute('data-filename') || '';
      var sz = c.getAttribute('data-size') || '';
      var sc = c.getAttribute('data-source') || '';
      var archEnabled = c.getAttribute('data-arch-enabled') !== '0';
      if (!archEnabled) unarchivable++;
      items.push({ uniqueId: uid, filename: fn, size: sz, source: sc, archEnabled: archEnabled });
    });

    if (!items.length) {
      window.__toast('所选项目中没有有效的文件', 'error');
      return;
    }
    if (unarchivable > 0) {
      if (!confirm('选中的文件中有 ' + unarchivable + ' 个尚未下载完成（无法归档），是否跳过未完成文件，继续对其余 ' + (items.length - unarchivable) + ' 个文件进行归档？')) {
        return;
      }
      items = items.filter(function (it) { return it.archEnabled; });
      if (!items.length) {
        window.__toast('没有已完成下载、可归档的文件', 'error');
        return;
      }
    }
    window.__openArchive(items);
  };

  window.__deleteLocalOne = function (btn) {
    var uid = btn.getAttribute('data-unique-id');
    var filename = btn.getAttribute('data-filename') || '该文件';
    if (!uid) {
      window.__toast('缺少文件 ID，无法删除', 'error');
      return;
    }
    if (!window.confirm('确定要彻底删除本地原文件吗？\n\n文件名: ' + filename + '\n\n删除后将永久释放磁盘空间，无法恢复。')) {
      return;
    }
    btn.disabled = true;
    postJSON('/library/local/delete', { uniqueId: uid })
      .then(function (r) { return r.json(); })
      .then(function (d) {
        btn.disabled = false;
        if (d && d.ok) {
          window.__toast('本地文件删除成功', 'success', filename);
          removeLocalFileFromDom(uid);
          window.__localUpdateSelection();
          reloadLocalFiles();
        } else {
          window.__toast('删除本地文件失败', 'error', (d && d.message) || '后端返回错误');
        }
        return null;
      })
      .catch(function () {
        btn.disabled = false;
        window.__toast('删除本地文件失败', 'error', '网络错误');
      });
  };

  window.__bulkDeleteLocal = function () {
    var uids = getCheckedUniqueIds();
    if (!uids.length) {
      window.__toast('请先勾选要删除的文件', 'error');
      return;
    }
    if (uids.length > 100) {
      window.__toast('单次最多批量删除 100 个文件', 'error');
      return;
    }
    if (!window.confirm('确定要彻底删除选中的 ' + uids.length + ' 个本地原文件吗？\n\n此操作将永久释放磁盘空间，无法恢复。')) {
      return;
    }
    var btn = document.getElementById('btnBatchDelete');
    if (btn) btn.disabled = true;
    postJSON('/library/local/delete', { uniqueIds: uids })
      .then(function (r) { return r.json(); })
      .then(function (d) {
        if (btn) btn.disabled = false;
        if (d && d.ok) {
          window.__toast('本地文件批量删除成功', 'success', d.message || ('已删除 ' + (d.deleted || uids.length) + ' 个文件'));
          (d.deletedUids || uids).forEach(function (uid) {
            removeLocalFileFromDom(uid);
          });
          window.__localClearSelection();
          reloadLocalFiles();
        } else {
          window.__toast('批量删除失败', 'error', (d && d.message) || '后端返回错误');
        }
        return null;
      })
      .catch(function () {
        if (btn) btn.disabled = false;
        window.__toast('批量删除失败', 'error', '网络错误');
      });
  };

  /* ---------------- 云端归档增强：云端删除、失效清理、文件取回与轮询 ---------------- */
  window.__deleteCloudFile = function (btn) {
    var path = btn.getAttribute('data-cloud-path');
    var jid = btn.getAttribute('data-job-id');
    var filename = btn.getAttribute('data-filename') || '该文件';
    if (!path && !jid) {
      window.__toast('缺少云端路径，无法删除', 'error');
      return;
    }
    if (!window.confirm('确定要从云端删除文件吗？\n\n文件名: ' + filename + '\n云端路径: ' + (path || '未知') + '\n\n此操作将调用 OpenList 删除云端文件并清理本地归档记录，不可恢复。')) {
      return;
    }
    btn.disabled = true;
    postJSON('/library/cloud/delete', { remotePath: path, id: jid })
      .then(function (r) { return r.json(); })
      .then(function (d) {
        btn.disabled = false;
        if (d && d.ok) {
          window.__toast('云端文件已删除', 'success', filename);
          var row = btn.closest('tr, .cloud-card');
          if (row) row.remove();
        } else {
          window.__toast('云端删除失败', 'error', (d && d.message) || '后端返回错误');
        }
        return null;
      })
      .catch(function () {
        btn.disabled = false;
        window.__toast('云端删除失败', 'error', '网络错误');
      });
  };

  var clearingMissing = false;
  window.__clearMissingCloudFiles = function () {
    if (clearingMissing) return;
    if (!window.confirm('确定要清理所有云端已失效的记录吗？\n\n将清理所有在 OpenList 云端已找不到原文件的归档记录。')) {
      return;
    }
    clearingMissing = true;
    postJSON('/library/cloud/clear-missing', {})
      .then(function (r) { return r.json(); })
      .then(function (d) {
        clearingMissing = false;
        if (d && d.ok) {
          window.__toast('清理完成', 'success', d.message || ('已清理 ' + (d.cleared || 0) + ' 条失效记录'));
          var missingRows = document.querySelectorAll('tr[data-status="missing"], .cloud-card[data-status="missing"]');
          Array.prototype.forEach.call(missingRows, function (r) { r.remove(); });
          var banner = document.getElementById('missingBanner');
          if (banner) banner.style.display = 'none';
        } else {
          window.__toast('清理失败', 'error', (d && d.message) || '后端返回错误');
        }
        return null;
      })
      .catch(function () {
        clearingMissing = false;
        window.__toast('清理失败', 'error', '网络错误');
      });
  };

  /* ---- 云端取回与状态轮询 ---- */
  var retrieveTimer = null;

  function applyRetrieveButton(btn, job) {
    var textEl = btn.querySelector('.ret-text') || btn;
    var st = job.state;
    if (st === 'downloading') {
      btn.disabled = true;
      textEl.textContent = '取回中 ' + (job.progress || 0) + '%';
      btn.classList.remove('danger');
    } else if (st === 'queued') {
      btn.disabled = true;
      textEl.textContent = '排队中…';
      btn.classList.remove('danger');
    } else if (st === 'done') {
      btn.disabled = false;
      textEl.textContent = '重新取回';
      btn.classList.remove('primary', 'danger');
      btn.classList.add('ghost');
      btn.title = '已取回到本地：' + (job.targetPath || '');
    } else if (st === 'failed') {
      btn.disabled = false;
      textEl.textContent = '取回失败 · 重试';
      btn.classList.add('danger');
      btn.title = job.error || '取回失败，点击重试';
    }
  }

  function applyRetrievePill(pill, job) {
    var st = job.state;
    var cls = '';
    var text = '';
    if (st === 'downloading') {
      cls = 'pending';
      var pct = Math.round(Number(job.progress) || 0);
      text = '取回中 ' + pct + '%';
    } else if (st === 'queued') {
      cls = 'pending';
      text = '排队中';
    } else if (st === 'done') {
      cls = 'ok';
      text = '已取回本地';
    } else if (st === 'failed') {
      cls = 'failed';
      text = '取回失败';
    } else {
      pill.style.display = 'none';
      return;
    }
    pill.style.display = '';
    pill.className = 'pill ' + cls;
    while (pill.firstChild) pill.removeChild(pill.firstChild);
    var dot = document.createElement('span');
    dot.className = 'p-dot';
    pill.appendChild(dot);
    var label = document.createElement('span');
    label.textContent = text;
    pill.appendChild(label);
  }

  var retrieveTimer = null;
  var retrieveIdleCount = 0;
  function __retrievePollTick() {
    fetch('/library/cloud/retrieve/status', { headers: { 'Accept': 'application/json' } })
      .then(function (r) { return r.json(); })
      .then(function (d) {
        if (!d || !d.ok) return null;
        var jobs = d.jobs || [];
        var byPath = {};
        var byId = {};
        var active = false;
        jobs.forEach(function (j) {
          if (!j) return;
          if (j.remotePath) byPath[j.remotePath.toLowerCase()] = j;
          if (j.id) byId[j.id] = j;
          if (j.state === 'queued' || j.state === 'downloading') active = true;
        });

        var btns = document.querySelectorAll('[data-retrieve-btn]');
        Array.prototype.forEach.call(btns, function (btn) {
          var p = (btn.getAttribute('data-cloud-path') || '').toLowerCase();
          var jid = btn.getAttribute('data-job-id') || '';
          var job = (p && byPath[p]) || (jid && byId[jid]);
          if (job) applyRetrieveButton(btn, job);
        });

        var pills = document.querySelectorAll('[data-ret-pill]');
        Array.prototype.forEach.call(pills, function (pill) {
          var p = (pill.getAttribute('data-cloud-path') || '').toLowerCase();
          var job = p && byPath[p];
          if (job) applyRetrievePill(pill, job);
        });

        if (active) {
          retrieveIdleCount = 0;
        } else {
          retrieveIdleCount++;
          if (retrieveIdleCount >= 3 && retrieveTimer) {
            clearInterval(retrieveTimer);
            retrieveTimer = null;
            retrieveIdleCount = 0;
          }
        }
        return null;
      })
      .catch(function () { /* 忽略瞬时网络错误 */ });
  }

  function __retrievePollStart() {
    retrieveIdleCount = 0;
    if (!retrieveTimer) {
      retrieveTimer = setInterval(__retrievePollTick, 2000);
    }
    __retrievePollTick();
  }
  window.__retrievePollStart = __retrievePollStart;

  window.__pageOnLeave(function () {
    if (retrieveTimer) { clearInterval(retrieveTimer); retrieveTimer = null; }
  });

  window.__retrieveCloudFile = function (btn) {
    var path = btn.getAttribute('data-cloud-path');
    var jid = btn.getAttribute('data-job-id');
    var filename = btn.getAttribute('data-filename') || '该文件';
    if (!path && !jid) {
      window.__toast('缺少云端文件路径，无法取回', 'error');
      return;
    }
    if (!window.confirm('确定要将该文件从云端取回并下载到本地磁盘吗？\n\n文件名: ' + filename + '\n云端路径: ' + (path || '未知'))) {
      return;
    }
    btn.disabled = true;
    var textEl = btn.querySelector('.ret-text') || btn;
    textEl.textContent = '提交中…';

    postJSON('/library/cloud/retrieve', { remotePath: path, id: jid })
      .then(function (r) { return r.json(); })
      .then(function (d) {
        if (d && d.ok) {
          if (d.alreadyExists) {
            btn.disabled = false;
            textEl.textContent = '重新取回';
            window.__toast('本地已存在同名文件', 'info', d.message || filename);
          } else if (d.alreadyRunning) {
            btn.disabled = true;
            textEl.textContent = '排队中…';
            window.__toast('任务正在运行', 'info', d.message || filename);
            __retrievePollStart();
          } else {
            textEl.textContent = '排队中…';
            window.__toast('取回任务已提交', 'success', (d.job && d.job.filename) || filename);
            __retrievePollStart();
          }
        } else {
          btn.disabled = false;
          textEl.textContent = '取回';
          window.__toast('取回提交失败', 'error', (d && d.message) || '后端返回错误');
        }
        return null;
      })
      .catch(function () {
        btn.disabled = false;
        textEl.textContent = '取回';
        window.__toast('取回提交失败', 'error', '网络错误');
      });
  };

  /* ---------------- Task actions (real backend calls) ---------------- */
  function withBtn(btn, work) {
    if (btn) btn.disabled = true;
    work().then(function () { if (btn) btn.disabled = false; }, function () { if (btn) btn.disabled = false; });
  }
  // 从触发元素取任务身份：优先 data-unique-id，其次 data-task-id（显示 id）反查 __TASK_META。
  // 元素由模板以 data-* 属性携带 id，杜绝在 onclick 内联 JS 里拼模板值（XSS 注入面）。
  function metaFromEl(el) {
    if (!el) return null;
    var uid = el.getAttribute && (el.getAttribute('data-unique-id') || el.getAttribute('data-task-id'));
    if (!uid) return null;
    var m = window.__TASK_META && window.__TASK_META[uid];
    if (m) return m;
    // 兼容以 display id 为键的旧数据
    return window.__TASK_META && window.__TASK_META['id:' + uid];
  }
  // 统一解析调用参数：__retryTask(el) 或旧式 __retryTask(id, btn)
  function resolveTaskArgs(a, b) {
    if (a && typeof a === 'object' && a.getAttribute) {   // 元素在前（推荐）
      return { el: a, id: null };
    }
    if (b && typeof b === 'object' && b.getAttribute) {   // 旧式 (id, btn)
      return { el: b, id: a };
    }
    return { el: null, id: a };
  }
  window.__retryTask = function (a, b) {
    var args = resolveTaskArgs(a, b);
    var meta = metaFromEl(args.el) || (args.id != null ? { uniqueId: String(args.id), telegramId: null } : null);
    var display = meta && (meta.displayId != null ? meta.displayId : args.id);
    if (!meta || !meta.uniqueId) {
      window.__toast('该记录缺少 uniqueId，无法操作', 'error');
      return;
    }
    withBtn(args.el, function () {
      // bridge 侧由 uniqueId 解析出 {chatId,messageId,fileId}，前端只传 uniqueId
      return postJSON('/task/retry', { uniqueId: String(meta.uniqueId) })
        .then(function (r) { return r.json(); }).then(function (d) {
          if (d && d.ok) {
            window.__toast('已重新加入下载队列', 'success', '任务 #' + display + ' 重试已提交');
          } else {
            window.__toast('重试失败', 'error', (d && d.message) || '后端返回错误');
          }
          return null;
        }).catch(function () {
          window.__toast('重试失败', 'error', '网络错误');
          return null;
        });
    });
  };
  window.__cancelTask = function (a, b) {
    var args = resolveTaskArgs(a, b);
    var meta = metaFromEl(args.el) || (args.id != null ? { uniqueId: String(args.id), telegramId: null } : null);
    var display = meta && (meta.displayId != null ? meta.displayId : args.id);
    if (!meta || !meta.uniqueId) {
      window.__toast('该记录缺少 uniqueId，无法操作', 'error');
      return;
    }
    withBtn(args.el, function () {
      return postJSON('/task/cancel', { uniqueId: String(meta.uniqueId) })
        .then(function (r) { return r.json(); }).then(function (d) {
          if (d && d.ok) {
            window.__toast('已取消任务 #' + display, 'success', '取消指令已发送');
          } else {
            window.__toast('取消失败', 'error', (d && d.message) || '后端返回错误');
          }
          return null;
        }).catch(function () {
          window.__toast('取消失败', 'error', '网络错误');
          return null;
        });
    });
  };
  /* 批量重试/取消：遍历表格里勾选的行，按 data-task-id 反查 __TASK_META 逐个调接口 */
  function bulkAction(kind) {
    var checked = Array.prototype.slice.call(document.querySelectorAll('.task-check:checked'));
    if (!checked.length) {
      window.__toast('请先勾选要操作的任务', 'error');
      return;
    }
    var metas = checked.map(function (c) {
      var row = c.closest('tr');
      var uid = row ? row.getAttribute('data-task-id') : null;
      return uid ? metaFromEl(row) : null;
    }).filter(function (v) { return v && v.uniqueId; });
    if (!metas.length) {
      window.__toast('没有可操作的任务', 'error');
      return;
    }
    var done = 0, failed = 0, pending = metas.length;
    metas.forEach(function (meta) {
      postJSON(kind === 'retry' ? '/task/retry' : '/task/cancel',
        { uniqueId: String(meta.uniqueId), telegramId: meta.telegramId })
        .then(function (r) { return r.json(); })
        .then(function (d) {
          if (d && d.ok) { done++; } else { failed++; }
          return null;
        })
        .catch(function () { failed++; })
        .then(function () {
          pending--;
          if (pending === 0) {
            window.__toast(
              kind === 'retry' ? '批量重试完成' : '批量取消完成',
              failed ? 'error' : 'success',
              '成功 ' + done + ' 条' + (failed ? '，失败 ' + failed + ' 条' : '')
            );
          }
          return null;
        });
    });
  }
  window.__bulkRetry = function () { bulkAction('retry'); };
  window.__bulkCancel = function () { bulkAction('cancel'); };
  window.__toggleTask = function (row) {
    // 接受 <tr> 元素（data-task-id）或历史字符串 'task-<id>' 两种调用
    var id = typeof row === 'string' ? row.replace(/^task-/, '') : (row && row.getAttribute ? row.getAttribute('data-task-id') : null);
    if (id === null || id === undefined) return;
    var e = document.getElementById('task-' + id);
    if (e) e.style.display = e.style.display === 'none' ? '' : 'none';
  };

  /* ---------------- Mark alerts read ---------------- */
  // 服务端已读游标：POST /alerts/read（CSRF），成功后本地即时隐藏徽标；
  // 下次页面渲染以服务端计数为准（0 时徽标不再渲染）。
  window.__markRead = function () {
    fetch('/alerts/read', {
      method: 'POST',
      headers: { 'X-CSRF-Token': csrfToken() }
    })
      .then(function (r) { return r.json(); })
      .then(function (d) {
        if (d && d.ok) {
          var b = document.querySelector('.icon-btn .dot-badge');
          if (b) b.style.display = 'none';
          toast('告警已全部标记为已读', 'info');
        } else {
          toast('标记已读失败', 'error');
        }
        return null;
      })
      .catch(function () { toast('标记已读失败', 'error', '网络错误'); });
  };

  /* ---------------- Logout ---------------- */
  window.__logout = function () {
    fetch('/auth/logout', {
      method: 'POST',
      headers: { 'X-CSRF-Token': csrfToken() }
    })
      .then(function () { window.location.href = '/login'; })
      .catch(function () { window.location.href = '/login'; });
  };

  /* ---------------- Copy helper ---------------- */
  window.__copy = function (text, el) {
    function done() {
      window.__toast('已复制到剪贴板', 'info');
    }
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(done, function () { fallback(); });
    } else { fallback(); }
    function fallback() {
      var ta = document.createElement('textarea');
      ta.value = text; document.body.appendChild(ta); ta.select();
      try { document.execCommand('copy'); done(); } catch (e) {}
      ta.remove();
    }
  };
  // 从 data-full 属性取值复制：值不经内联 JS 字符串上下文，杜绝逃逸注入
  window.__copyEl = function (el) {
    var full = el && (el.getAttribute('data-full') || el.textContent);
    if (full) window.__copy(full, el);
  };
  // set title for clipped hashes on hover
  if (firstBoot) {
    document.addEventListener('mouseover', function (e) {
      var h = e.target.closest && e.target.closest('.hash');
      if (h) {
        var full = h.getAttribute('data-full');
        if (full) h.setAttribute('title', full);
      }
    });
  }

  /* ---------------- SSE (logs / tasks) auto-reconnect ---------------- */
  window.__openSSE = function (urlOrFn, onMessage) {
    if (typeof EventSource === 'undefined') return null;
    var es = null;
    var closedByUser = false;
    var retryDelay = 2000;
    var reconnectTimer = null;
    function getUrl() {
      return typeof urlOrFn === 'function' ? urlOrFn() : urlOrFn;
    }
    function connect() {
      if (closedByUser) return;
      if (es) {
        try { es.close(); } catch (_) {}
        es = null;
      }
      try {
        es = new EventSource(getUrl());
      } catch (e) { return; }
      es.onmessage = function (e) {
        retryDelay = 2000; // 正常收到消息即重置退避
        try { onMessage(JSON.parse(e.data)); } catch (_) { onMessage(e.data); }
      };
      es.onerror = function () {
        // EventSource 断开后浏览器可能自动重连；readyState===CLOSED 时自行退避重连
        if (closedByUser) return;
        if (es && es.readyState === 2) {
          reconnectTimer = setTimeout(connect, retryDelay);
          retryDelay = Math.min(retryDelay * 2, 30000);
        }
      };
    }
    connect();
    var wrapper = {
      close: function () {
        closedByUser = true;
        if (reconnectTimer) { clearTimeout(reconnectTimer); reconnectTimer = null; }
        if (es) { try { es.close(); } catch (e) {} es = null; }
      }
    };
    // boost 切页离开当前页面时自动断流，避免长寿命文档里的 SSE 泄漏
    window.__pageOnLeave(wrapper.close);
    return wrapper;
  };

  /* ---------------- OpenList Card Component for Settings ---------------- */
  window.openlistCard = function openlistCard() {
    return {
      st: { loading: true, loggedIn: false, username: '', baseUrl: 'http://127.0.0.1:5244', verified: false, loggedAt: 0, message: '' },
      form: { baseUrl: 'http://127.0.0.1:5244', username: '', password: '' },
      busy: false,
      fmtTime: function (ts) {
        if (!ts) return '';
        var d = new Date(ts * 1000);
        var p = function (n) { return (n < 10 ? '0' : '') + n; };
        return d.getFullYear() + '-' + p(d.getMonth() + 1) + '-' + p(d.getDate()) + ' ' + p(d.getHours()) + ':' + p(d.getMinutes());
      },
      load: function () {
        var self = this;
        fetch('/openlist/status', { headers: { 'Accept': 'application/json' } })
          .then(function (r) { return r.json(); })
          .then(function (d) {
            self.st.loading = false;
            if (!d || typeof d !== 'object' || d.ok === false) {
              self.st.message = (d && d.message) || '状态检查失败';
              return;
            }
            self.st.loggedIn = !!d.loggedIn;
            self.st.username = d.username || '';
            self.st.baseUrl = d.baseUrl || 'http://127.0.0.1:5244';
            self.st.verified = !!d.verified;
            self.st.loggedAt = d.loggedAt || 0;
            self.st.message = d.message || '';
            self.form.baseUrl = self.st.baseUrl;
            if (self.st.loggedIn) self.form.username = self.st.username;
          })
          .catch(function () { self.st.loading = false; self.st.message = '网络错误'; });
      },
      login: function () {
        if (this.busy) return;
        var self = this;
        if (!this.form.username || !this.form.password) {
          window.__toast('请填写 OpenList 用户名与密码', 'error'); return;
        }
        var targetUrl = (this.form.baseUrl || '').trim().replace(/\/+$/, '') || 'http://127.0.0.1:5244';
        this.busy = true;
        fetch('/openlist/login', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': window.__csrfToken ? window.__csrfToken() : '' },
          body: JSON.stringify({ baseUrl: targetUrl, username: this.form.username, password: this.form.password })
        }).then(function (r) { return r.json(); }).then(function (d) {
          if (d && d.ok) {
            window.__toast('OpenList 登录成功', 'success', '已经 OpenList API 验证');
            self.st.loggedIn = true;
            self.st.username = (d && d.username) || self.form.username;
            self.st.baseUrl = (d && d.baseUrl) || targetUrl;
            self.st.verified = true;
            self.st.loggedAt = Date.now() / 1000;
            self.st.message = '';
            self.form.password = '';
          } else {
            window.__toast('OpenList 登录失败', 'error', (d && d.message) || '后端返回错误');
          }
          self.busy = false;
        }).catch(function () {
          self.busy = false;
          window.__toast('OpenList 登录失败', 'error', '网络错误');
        });
      },
      logout: function () {
        if (this.busy) return;
        var self = this;
        this.busy = true;
        fetch('/openlist/logout', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': window.__csrfToken ? window.__csrfToken() : '' },
          body: '{}'
        }).then(function (r) { return r.json(); }).then(function (d) {
          if (d && d.ok) {
            window.__toast('已退出 OpenList', 'success');
            self.st = { loading: false, loggedIn: false, username: '', baseUrl: self.form.baseUrl || 'http://127.0.0.1:5244', verified: false, loggedAt: 0, message: '' };
            self.form.password = '';
          } else {
            window.__toast('退出失败', 'error', (d && d.message) || '后端返回错误');
          }
          self.busy = false;
        }).catch(function () {
          self.busy = false;
          window.__toast('退出失败', 'error', '网络错误');
        });
      }
    };
  };

  /* ---------------- Archive Settings Card Component ---------------- */
  window.archiveSettingsCard = function archiveSettingsCard() {
    return {
      cfg: {
        autoArchive: true,
        defaultDir: '',
        policy: 'overwrite',
        deleteLocal: false,
        cleanFilename: true,
        diskWatermarkGB: 5.0,
        diskAutoClean: true,
        diskHighWatermarkPercent: 85.0,
        diskLowWatermarkPercent: 75.0,
        diskUsagePercent: 0,
        diskFreeGB: 0,
        stats: {}
      },
      busy: false,
      sweeping: false,
      dirOpen: false,
      browsePath: '/',
      dirs: [],
      crumbList: [],
      loadingDirs: false,
      buildCrumbs: function (path) {
        var segs = (path || '/').split('/').filter(Boolean);
        var res = [{ label: '根目录 (全部网盘)', path: '/' }];
        var acc = '';
        segs.forEach(function (s) {
          acc += '/' + s;
          res.push({ label: s, path: acc });
        });
        return res;
      },
      load: function () {
        var self = this;
        fetch('/archive/config', { headers: { 'Accept': 'application/json' } })
          .then(function (r) { return r.json(); })
          .then(function (d) {
            if (d && d.ok && d.config) {
              self.cfg = d.config;
              if (d.config.defaultDir) {
                window.__DEFAULT_ARCH_DIR = d.config.defaultDir;
              } else {
                try {
                  var last = localStorage.getItem('tg-archive-last-dir');
                  if (last) self.cfg.defaultDir = last;
                } catch (e) {}
              }
            }
          })
          .catch(function () {});
      },
      save: function () {
        if (this.busy) return;
        var self = this;
        this.busy = true;
        fetch('/archive/config', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': window.__csrfToken ? window.__csrfToken() : '' },
          body: JSON.stringify(this.cfg)
        }).then(function (r) { return r.json(); }).then(function (d) {
          self.busy = false;
          if (d && d.ok) {
            self.cfg = d.config || self.cfg;
            if (self.cfg.defaultDir) {
              window.__DEFAULT_ARCH_DIR = self.cfg.defaultDir;
              try { localStorage.setItem('tg-archive-last-dir', self.cfg.defaultDir); } catch (e) {}
            }
            window.__toast('归档设置已保存', 'success', self.cfg.autoArchive ? ('已开启自动归档 → ' + (self.cfg.defaultDir || '未配置目录')) : '已关闭自动归档');
          } else {
            window.__toast('保存失败', 'error', (d && d.message) || '后端返回错误');
          }
        }).catch(function () {
          self.busy = false;
          window.__toast('保存失败', 'error', '网络错误');
        });
      },
      triggerSweep: function () {
        if (this.sweeping) return;
        var self = this;
        this.sweeping = true;
        fetch('/archive/sweep', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': window.__csrfToken ? window.__csrfToken() : '' },
          body: '{}'
        }).then(function (r) { return r.json(); }).then(function (d) {
          self.sweeping = false;
          if (d && d.ok) {
            var n = d.enqueued || 0;
            if (n > 0) {
              window.__toast('扫描完成', 'success', '已将 ' + n + ' 个已完成下载的文件加入归档队列');
              if (window.__archPollStart) window.__archPollStart();
            } else {
              window.__toast('扫描完成', 'info', d.message || '没有新的待归档任务（均已归档或未下载完成）');
            }
            self.load();
          } else {
            window.__toast('扫描失败', 'error', (d && d.message) || '无法完成扫描');
          }
        }).catch(function () {
          self.sweeping = false;
          window.__toast('扫描失败', 'error', '网络错误');
        });
      },
      toggleDir: function () {
        this.dirOpen = !this.dirOpen;
        if (this.dirOpen) {
          var target = (this.cfg.defaultDir || '').trim() || '/';
          this.loadDir(target);
        }
      },
      loadDir: function (path) {
        var self = this;
        this.browsePath = path || '/';
        this.crumbList = this.buildCrumbs(this.browsePath);
        this.loadingDirs = true;
        this.dirs = [];
        fetch('/openlist/dirs?path=' + encodeURIComponent(this.browsePath))
          .then(function (r) { return r.json(); })
          .then(function (d) {
            self.loadingDirs = false;
            if (d && d.ok && Array.isArray(d.dirs)) {
              self.dirs = d.dirs;
            }
          })
          .catch(function () {
            self.loadingDirs = false;
          });
      },
      goUpDir: function () {
        var segs = (this.browsePath || '/').split('/').filter(Boolean);
        segs.pop();
        var p = segs.length ? ('/' + segs.join('/')) : '/';
        this.loadDir(p);
      },
      pickCurrentDir: function () {
        this.cfg.defaultDir = this.browsePath || '/';
        this.dirOpen = false;
        try { localStorage.setItem('tg-archive-last-dir', this.cfg.defaultDir); } catch (e) {}
      }
    };
  };

  /* ---------------- Telegram 消息通知外发引擎设置卡片 ---------------- */
  window.notifySettingsCard = function notifySettingsCard() {
    return {
      cfg: {
        enabled: false,
        channel: 'both',
        botToken: '',
        hasBotToken: false,
        chatId: '',
        minFileSizeMB: 50,
        events: {
          downloadCompleted: true,
          archiveSuccess: true,
          archiveFailed: true,
          diskWatermarkAlert: true
        }
      },
      busy: false,
      testing: false,
      load: function () {
        var self = this;
        fetch('/api/notify/config', { headers: { 'Accept': 'application/json' } })
          .then(function (r) { return r.json(); })
          .then(function (d) {
            if (d && d.ok && d.config) {
              self.cfg = d.config;
            }
          })
          .catch(function () {});
      },
      save: function () {
        var self = this;
        self.busy = true;
        fetch('/api/notify/config', {
          method: 'POST',
          headers: {
            'Content-Type': 'application/json',
            'X-CSRF-Token': window.__csrfToken ? window.__csrfToken() : ''
          },
          body: JSON.stringify(self.cfg)
        })
          .then(function (r) { return r.json(); })
          .then(function (d) {
            self.busy = false;
            if (d && d.ok) {
              window.__toast('通知配置已保存', 'success', d.message || '配置已实时生效');
              self.load();
            } else {
              window.__toast('保存失败', 'error', (d && d.message) || '服务端异常');
            }
          })
          .catch(function (e) {
            self.busy = false;
            window.__toast('保存失败', 'error', '网络异常: ' + e);
          });
      },
      testSend: function () {
        var self = this;
        self.testing = true;
        fetch('/api/notify/test', {
          method: 'POST',
          headers: {
            'Content-Type': 'application/json',
            'X-CSRF-Token': window.__csrfToken ? window.__csrfToken() : ''
          },
          body: JSON.stringify({
            botToken: self.cfg.botToken,
            chatId: self.cfg.chatId,
            channel: self.cfg.channel
          })
        })
          .then(function (r) { return r.json(); })
          .then(function (d) {
            self.testing = false;
            if (d && d.ok) {
              window.__toast('测试通知已外发', 'success', d.message || '请在 Telegram 客户端查看卡片消息');
            } else {
              window.__toast('测试外发失败', 'error', (d && d.message) || '无法完成推送');
            }
          })
          .catch(function (e) {
            self.testing = false;
            window.__toast('测试请求失败', 'error', '网络异常: ' + e);
          });
      }
    };
  };

  /* ---------------- 云端在线播放与播放器联动 ---------------- */
  var curStreamUrl = '';
  window.__playCloudVideo = function (cloudPath, filename) {
    var modal = document.getElementById('cloudVideoModal');
    var titleEl = document.getElementById('cloudVideoTitle');
    var loadingEl = document.getElementById('cloudVideoLoading');
    var video = document.getElementById('cloudVideoPlayer');
    var errEl = document.getElementById('cloudVideoErr');
    if (!modal) return;
    if (titleEl) titleEl.textContent = filename || '在线播放';
    if (loadingEl) loadingEl.style.display = 'block';
    if (video) { video.style.display = 'none'; video.pause(); video.src = ''; }
    if (errEl) errEl.style.display = 'none';
    modal.classList.add('show');
    modal.style.display = 'flex';
    document.body.style.overflow = 'hidden';
    curStreamUrl = '';

    fetch('/openlist/stream-url?path=' + encodeURIComponent(cloudPath))
      .then(function (r) { return r.json(); })
      .then(function (d) {
        if (loadingEl) loadingEl.style.display = 'none';
        if (d && d.ok && d.url) {
          curStreamUrl = d.url;
          if (video) {
            video.src = d.url;
            video.style.display = 'block';
            video.play().catch(function () {});
          }
        } else {
          if (errEl) {
            errEl.textContent = (d && d.message) || '获取视频流地址失败';
            errEl.style.display = 'block';
          }
        }
      })
      .catch(function () {
        if (loadingEl) loadingEl.style.display = 'none';
        if (errEl) {
          errEl.textContent = '网络错误，无法加载视频流';
          errEl.style.display = 'block';
        }
      });
  };

  window.__closeCloudVideo = function () {
    var modal = document.getElementById('cloudVideoModal');
    var video = document.getElementById('cloudVideoPlayer');
    if (video) { video.pause(); video.removeAttribute('src'); video.load(); }
    if (modal) { modal.classList.remove('show'); modal.style.display = 'none'; }
    document.body.style.overflow = '';
    curStreamUrl = '';
  };

  window.__copyStreamUrl = function () {
    if (!curStreamUrl) return;
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(curStreamUrl).then(function () {
        window.__toast('播放直链已复制', 'success', '可直接在 PotPlayer / VLC / IINA 中打开串流');
      });
    } else {
      window.prompt('请复制播放链接：', curStreamUrl);
    }
  };

  window.__openExternalPlayer = function (app) {
    if (!curStreamUrl) return;
    if (app === 'potplayer') {
      window.location.href = 'potplayer://' + curStreamUrl;
    } else if (app === 'vlc') {
      window.location.href = 'vlc://' + curStreamUrl;
    } else if (app === 'iina') {
      window.location.href = 'iina://weblink?url=' + encodeURIComponent(curStreamUrl);
    }
  };

  /* ---------------- Session 备份与还原 Card（真实快照列表 + 一键还原） ---------------- */
  window.sessionBackupCard = function sessionBackupCard() {
    return {
      st: {
        loading: true,
        status: 'warn',
        dotClass: 'warn',
        label: '检查中…',
        lastBackupTime: '',
        lastBackupSize: '',
        lastBackupName: '',
        lastRestoreTime: '',
        lastRestoreName: '',
        remotePath: '',
        backupCount: 0,
        uploadedToOpenList: false,
        message: ''
      },
      list: [],
      localCount: 0,
      remoteCount: 0,
      remoteDir: '',
      remoteError: '',
      mounts: [],
      dirSource: '',
      needsConfig: false,
      bkDir: '',
      savingDir: false,
      // 本地备份目录（绝对路径；空 = 自动推导）
      localDirInput: '',
      localDirResolved: '',
      localSource: '',
      localWritable: true,
      savingLocalDir: false,
      savingDir: false,
      busy: false,
      restoring: false,
      loading: true,
      load: function () {
        var self = this;
        fetch('/api/session/backup/status', { headers: { 'Accept': 'application/json' } })
          .then(function (r) { return r.json(); })
          .then(function (d) {
            self.st.loading = false;
            if (!d || typeof d !== 'object' || d.ok === false) {
              self.st.status = 'err';
              self.st.dotClass = 'err';
              self.st.label = '状态异常';
              self.st.message = (d && d.message) || '无法获取备份状态';
              return;
            }
            self.st.status = d.status || 'warn';
            self.st.dotClass = d.dotClass || 'warn';
            self.st.label = d.label || '检查中…';
            self.st.lastBackupTime = d.lastBackupTime || '';
            self.st.lastBackupSize = d.lastBackupSize || '';
            self.st.lastBackupName = d.lastBackupName || '';
            self.st.lastRestoreTime = d.lastRestoreTime || '';
            self.st.lastRestoreName = d.lastRestoreName || '';
            self.st.remotePath = d.remotePath || '';
            self.st.backupCount = d.backupCount || 0;
            self.st.uploadedToOpenList = !!d.uploadedToOpenList;
            self.st.message = d.message || '';
          })
          .catch(function () {
            self.st.loading = false;
            self.st.status = 'err';
            self.st.dotClass = 'err';
            self.st.label = '网络异常';
            self.st.message = '网络连接异常，无法查询备份状态';
          });
        return this.loadList();
      },
      loadList: function () {
        var self = this;
        return fetch('/api/session/backups', { headers: { 'Accept': 'application/json' } })
          .then(function (r) { return r.json(); })
          .then(function (d) {
            self.loading = false;
            self.list = (d && d.items) || [];
            self.localCount = (d && d.localCount) || 0;
            self.remoteCount = (d && d.remoteCount) || 0;
            self.remoteDir = (d && d.remoteDir) || '';
            self.remoteError = (d && d.remoteError) || '';
            self.mounts = (d && d.remoteMounts) || [];
            self.dirSource = (d && d.remoteSource) || '';
            self.needsConfig = !!(d && d.needsConfig);
            // 输入框回显已保存的自定义目录（未配置则留空，表示自动推导）
            if (!self.bkDir) self.bkDir = (d && d.savedDir) || '';
            // 本地备份目录：已存的自定义值 + 当前实际生效路径与可写性
            if (!self.localDirInput) self.localDirInput = (d && d.localDirSaved) || '';
            self.localDirResolved = (d && d.localDir) || '';
            self.localSource = (d && d.localSource) || '';
            self.localWritable = d && d.localWritable !== false;
          })
          .catch(function () {
            self.loading = false;
            self.list = [];
            self.remoteError = '无法读取快照列表';
          });
      },
      saveDir: function () {
        if (this.savingDir) return;
        var self = this;
        this.savingDir = true;
        fetch('/api/session/backup/dir', {
          method: 'POST',
          headers: {
            'Content-Type': 'application/json',
            'X-CSRF-Token': window.__csrfToken ? window.__csrfToken() : ''
          },
          body: JSON.stringify({ dir: (this.bkDir || '').trim() })
        }).then(function (r) { return r.json(); }).then(function (d) {
          self.savingDir = false;
          if (d && d.ok) {
            window.__toast('备份目录已保存', 'success', d.message || '');
          } else {
            window.__toast('保存失败', 'error', (d && d.message) || '服务端异常');
          }
          self.bkDir = (d && typeof d.dir === 'string') ? d.dir : self.bkDir;
          self.load();
        }).catch(function () {
          self.savingDir = false;
          window.__toast('保存失败', 'error', '网络连接超时或异常');
        });
      },
      saveLocalDir: function () {
        if (this.savingLocalDir) return;
        var self = this;
        this.savingLocalDir = true;
        fetch('/api/session/backup/local-dir', {
          method: 'POST',
          headers: {
            'Content-Type': 'application/json',
            'X-CSRF-Token': window.__csrfToken ? window.__csrfToken() : ''
          },
          body: JSON.stringify({ dir: (this.localDirInput || '').trim() })
        }).then(function (r) { return r.json(); }).then(function (d) {
          self.savingLocalDir = false;
          if (d && d.ok) {
            window.__toast('本地备份目录已保存', 'success', d.message || '');
          } else {
            window.__toast('保存失败', 'error', (d && d.message) || '服务端异常');
          }
          // 保存失败时保留用户输入，便于修正；成功则以服务端归一化结果为准
          if (d && d.ok) self.localDirInput = d.dir || '';
          self.load();
        }).catch(function () {
          self.savingLocalDir = false;
          window.__toast('保存失败', 'error', '网络连接超时或异常');
        });
      },
      triggerLocalBackup: function () {
        if (this.busy || this.restoring) return;
        var self = this;
        this.busy = true;
        fetch('/api/session/backup', {
          method: 'POST',
          headers: {
            'Content-Type': 'application/json',
            'X-CSRF-Token': window.__csrfToken ? window.__csrfToken() : ''
          },
          body: JSON.stringify({ localOnly: true })
        }).then(function (r) { return r.json(); }).then(function (d) {
          self.busy = false;
          if (d && d.ok) {
            window.__toast('本地备份完成', 'success', d.message || '已生成加密快照');
          } else {
            window.__toast('本地备份失败', 'error', (d && d.message) || '服务端异常');
          }
          self.load();
        }).catch(function () {
          self.busy = false;
          window.__toast('本地备份请求失败', 'error', '网络连接超时或异常');
        });
      },
      triggerBackup: function () {
        if (this.busy || this.restoring) return;
        var self = this;
        this.busy = true;
        fetch('/api/session/backup', {
          method: 'POST',
          headers: {
            'Content-Type': 'application/json',
            'X-CSRF-Token': window.__csrfToken ? window.__csrfToken() : ''
          },
          body: '{}'
        }).then(function (r) { return r.json(); }).then(function (d) {
          self.busy = false;
          if (d && d.ok) {
            window.__toast('备份完成', 'success', d.message || '已生成加密快照');
          } else {
            window.__toast('备份失败', 'error', (d && d.message) || '服务端异常');
          }
          self.load();
        }).catch(function () {
          self.busy = false;
          window.__toast('备份请求失败', 'error', '网络连接超时或异常');
        });
      },
      restore: function (it) {
        if (this.restoring || this.busy || !it) return;
        var self = this;
        var where = it.origin === 'remote' ? 'OpenList 云端' : '本地';
        var kind = it.encrypted ? '（加密包）' : '（明文快照）';
        var ok = window.confirm(
          '确定要用这份快照还原 Session 吗？\n\n' +
          '快照: ' + it.name + '\n' +
          '位置: ' + where + kind + '\n' +
          '时间: ' + (it.mtimeStr || '—') + '   大小: ' + (it.sizeStr || '—') + '\n\n' +
          '还原过程将：\n' +
          '1) 先自动生成一份「还原前快照」以便回滚；\n' +
          '2) 短暂停止下载后端容器，把 td.binlog 与管理台凭据写回；\n' +
          '3) 自动重新启动后端容器。\n\n' +
          '期间下载会中断数十秒，确定继续？'
        );
        if (!ok) return;
        this.restoring = true;
        fetch('/api/session/restore', {
          method: 'POST',
          headers: {
            'Content-Type': 'application/json',
            'X-CSRF-Token': window.__csrfToken ? window.__csrfToken() : ''
          },
          body: JSON.stringify({ name: it.name, origin: it.origin, confirm: true })
        }).then(function (r) { return r.json(); }).then(function (d) {
          self.restoring = false;
          if (d && d.ok) {
            window.__toast('还原完成', 'success', d.message || '会话已还原');
          } else {
            window.__toast('还原失败', 'error', (d && d.message) || '服务端异常');
          }
          self.load();
        }).catch(function () {
          self.restoring = false;
          window.__toast('还原请求失败', 'error', '网络连接超时或异常');
        });
      }
    };
  };

  /* ---------------- Telegram FloodWait 智能冷却与倒计时管理器 ---------------- */
  window.floodWaitManager = (function () {
    var timer = null;
    var currentRemaining = 0;
    var isCooling = false;

    function formatTime(sec) {
      if (sec <= 0) return '00:00';
      var m = Math.floor(sec / 60);
      var s = sec % 60;
      return (m < 10 ? '0' + m : m) + ':' + (s < 10 ? '0' + s : s);
    }

    function updateUI(status) {
      var bar = document.getElementById('floodWaitBar');
      var cdEl = document.getElementById('floodWaitCountdown');
      var msgEl = document.getElementById('floodWaitMsg');

      if (!status || !status.isCooling || status.remainingSeconds <= 0) {
        if (isCooling) {
          isCooling = false;
          if (bar) bar.style.display = 'none';
          if (window.__toast) window.__toast('Telegram 冷却已解除', 'success', '风控冷却期已过，任务已全自动恢复续跑');
          var tasksTable = document.getElementById('tasksTable');
          if (tasksTable && window.htmx) {
            window.htmx.ajax('GET', '/partials/tasks', '#tasksTable');
          }
        }
        return;
      }

      isCooling = true;
      currentRemaining = status.remainingSeconds;
      if (bar) bar.style.display = 'block';
      if (cdEl) cdEl.textContent = formatTime(currentRemaining);
      if (msgEl) {
        msgEl.textContent = status.message || ('触发 Telegram 风控冷却 (' + (status.reason || 'FLOOD_WAIT') + ')，系统已安全挂起请求');
      }

      if (!timer) {
        timer = setInterval(function () {
          if (currentRemaining > 0) {
            currentRemaining--;
            if (cdEl) cdEl.textContent = formatTime(currentRemaining);
          } else {
            clearInterval(timer);
            timer = null;
            checkStatus();
          }
        }, 1000);
      }
    }

    function checkStatus() {
      fetch('/api/tg/floodwait/status')
        .then(function (r) { return r.json(); })
        .then(function (res) {
          if (res && res.ok && res.data) {
            updateUI(res.data);
          }
        })
        .catch(function () {});
    }

    if (typeof window !== 'undefined') {
      if (window.__floodWaitPollTimer) {
        clearInterval(window.__floodWaitPollTimer);
        window.__floodWaitPollTimer = null;
      }
      setTimeout(checkStatus, 1500);
      window.__floodWaitPollTimer = setInterval(checkStatus, 30000);
    }

    return {
      check: checkStatus,
      update: updateUI,
    };
  })();

  window.__resetFloodWait = function () {
    fetch('/api/tg/floodwait/reset', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': window.__csrfToken ? window.__csrfToken() : '' },
      body: '{}'
    })
      .then(function (r) { return r.json(); })
      .then(function (d) {
        if (d && d.ok) {
          window.__toast('FloodWait 状态已重置', 'success', d.message || '挂起任务已恢复调度');
          var bar = document.getElementById('floodWaitBar');
          if (bar) bar.style.display = 'none';
          if (window.floodWaitManager) window.floodWaitManager.check();
        } else {
          window.__toast('重置失败', 'error', (d && d.message) || '无法重置');
        }
      })
      .catch(function (e) {
        window.__toast('重置请求失败', 'error', '网络异常: ' + e);
      });
  };

  window.floodWaitAccountCard = function floodWaitAccountCard() {
    return {
      st: {
        isCooling: false,
        remainingSeconds: 0,
        formattedRemaining: '00:00',
        reason: '',
        suspendedTasksCount: 0
      },
      busy: false,
      init: function () {
        var self = this;
        self.load();
        setInterval(function () {
          if (self.st.isCooling && self.st.remainingSeconds > 0) {
            self.st.remainingSeconds--;
            var m = Math.floor(self.st.remainingSeconds / 60);
            var s = self.st.remainingSeconds % 60;
            self.st.formattedRemaining = (m < 10 ? '0' + m : m) + ':' + (s < 10 ? '0' + s : s);
          }
        }, 1000);
      },
      load: function () {
        var self = this;
        fetch('/api/tg/floodwait/status')
          .then(function (r) { return r.json(); })
          .then(function (res) {
            if (res && res.ok && res.data) {
              self.st.isCooling = !!res.data.isCooling;
              self.st.remainingSeconds = res.data.remainingSeconds || 0;
              self.st.reason = res.data.reason || '';
              self.st.suspendedTasksCount = res.data.suspendedTasksCount || 0;
              var m = Math.floor(self.st.remainingSeconds / 60);
              var s = self.st.remainingSeconds % 60;
              self.st.formattedRemaining = (m < 10 ? '0' + m : m) + ':' + (s < 10 ? '0' + s : s);
            }
          })
          .catch(function () {});
      },
      resetCooldown: function () {
        var self = this;
        self.busy = true;
        fetch('/api/tg/floodwait/reset', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': window.__csrfToken ? window.__csrfToken() : '' },
          body: '{}'
        }).then(function (r) { return r.json(); }).then(function (d) {
          self.busy = false;
          if (d && d.ok) {
            window.__toast('冷却状态已重置', 'success');
            self.load();
            if (window.floodWaitManager) window.floodWaitManager.check();
          } else {
            window.__toast('重置失败', 'error', (d && d.message) || '失败');
          }
        }).catch(function () {
          self.busy = false;
          window.__toast('重置失败', 'error', '网络异常');
        });
      }
    };
  };

  /* ---------------- System Doctor 系统健康自检组件 ---------------- */
  window.doctorCard = function doctorCard() {
    return {
      checking: false,
      report: null,
      init: function () {
        this.runCheck();
      },
      runCheck: function () {
        if (this.checking) return;
        var self = this;
        self.checking = true;
        fetch('/api/system/doctor')
          .then(function (r) { return r.json(); })
          .then(function (res) {
            self.checking = false;
            if (res && res.ok && res.data) {
              self.report = res.data;
              var st = res.data.overallStatus;
              if (st === 'healthy') {
                window.__toast('系统体检完成', 'success', '4 大依赖组件全部健康在线');
              } else if (st === 'warning') {
                window.__toast('系统体检完成', 'info', '系统整体在线，发现个别预警项');
              } else {
                window.__toast('发现关键故障', 'error', '检测到组件异常，请查看排查建议');
              }
            } else {
              window.__toast('体检执行失败', 'error', (res && res.message) || '服务端异常');
            }
          })
          .catch(function (e) {
            self.checking = false;
            window.__toast('体检网络超时', 'error', '无法连接 System Doctor 诊断服务: ' + e);
          });
      },
      overallPillClass: function () {
        if (!this.report) return 'pending';
        var st = this.report.overallStatus;
        if (st === 'healthy') return 'ok';
        if (st === 'warning') return 'warn';
        return 'failed';
      },
      overallLabel: function () {
        if (!this.report) return '体检中…';
        var st = this.report.overallStatus;
        if (st === 'healthy') return '全系统健康';
        if (st === 'warning') return '部分预警';
        return '组件故障';
      },
      copyReport: function () {
        if (!this.report) return;
        var r = this.report;
        var lines = [
          '# 🩺 System Doctor 全链路健康自检报告',
          '**体检时间**：' + new Date(r.timestamp * 1000).toLocaleString(),
          '**系统总体健康度**：' + r.overallStatus.toUpperCase() + ' (' + r.summaryMessage + ')',
          '**探测全链路总延时**：' + r.totalLatencyMs + ' ms',
          '',
          '## 组件诊断详情：',
        ];
        if (r.components) {
          for (var k in r.components) {
            var c = r.components[k];
            lines.push('- **' + c.name + '**：[' + c.status.toUpperCase() + '] ' + c.message + ' (耗时: ' + c.latencyMs + 'ms)');
            if (c.recommendation) {
              lines.push('  - *修复建议*：' + c.recommendation);
            }
          }
        }
        var text = lines.join('\n');
        if (navigator.clipboard && navigator.clipboard.writeText) {
          navigator.clipboard.writeText(text).then(function () {
            window.__toast('报告已复制到剪贴板', 'success', '已脱敏敏感凭据与内部拓扑');
          });
        } else {
          window.prompt('请复制脱敏体检报告：', text);
        }
      }
    };
  };

  /* ---------------- 资产智能关联抽屉与查重拦截交互 ---------------- */
  window.__openBrowseAssetDrawer = function (cardOrBtn) {
    if (!cardOrBtn) return;
    var card = cardOrBtn.classList && cardOrBtn.classList.contains('browse-card') ? cardOrBtn : cardOrBtn.closest('.browse-card');
    if (!card) return;

    var backdrop = document.getElementById('browseAssetBackdrop');
    if (!backdrop) return;

    var name = card.dataset.name || '文件';
    var size = card.dataset.size || '—';
    var uid = card.dataset.uid || '—';
    var isArchived = (card.dataset.archived === '1');
    var isDownloaded = (card.dataset.downloaded === '1');
    var cloudPath = card.dataset.cloudPath || '—';
    var cloudDrive = card.dataset.cloudDrive || '默认网盘';
    var openlistUrl = card.dataset.openlistUrl || '';
    var archivedDate = card.dataset.archivedDate || '';
    var localPath = card.dataset.localPath || '';

    var iconEl = document.getElementById('assetDrawerIcon');
    var headEl = document.getElementById('assetDrawerHeading');
    var nameEl = document.getElementById('assetDrawerName');
    var metaEl = document.getElementById('assetDrawerMeta');
    var promptEl = document.getElementById('assetDrawerPrompt');
    var detailsEl = document.getElementById('assetDrawerDetails');
    var btnPrimary = document.getElementById('assetDrawerActionPrimary');
    var btnSecondary = document.getElementById('assetDrawerActionSecondary');

    if (nameEl) nameEl.textContent = name;
    if (metaEl) metaEl.textContent = '文件大小: ' + size + ' · 指纹 ID: ' + (uid ? uid.slice(0, 16) + '...' : '—');

    if (isArchived) {
      if (iconEl) iconEl.textContent = '☁️';
      if (headEl) headEl.textContent = '云端网盘已归档资产';
      if (promptEl) {
        promptEl.className = 'quick-status-banner warning';
        promptEl.innerHTML = '☁️ <b>提示：</b>该文件已于 <b>' + (archivedDate || '近期') + '</b> 安全归档至网盘，无需重复占用 VPS 磁盘和带宽下载。';
      }
      if (detailsEl) {
        detailsEl.innerHTML = 
          '<div style="display:flex;justify-content:space-between;padding:4px 0;"><span style="color:var(--text-3);">目标网盘</span><span style="font-weight:600;">' + (cloudDrive || '默认网盘') + '</span></div>' +
          '<div style="display:flex;justify-content:space-between;padding:4px 0;"><span style="color:var(--text-3);">网盘存储路径</span><span style="font-family:var(--font-mono);word-break:break-all;">' + cloudPath + '</span></div>' +
          '<div style="display:flex;justify-content:space-between;padding:4px 0;"><span style="color:var(--text-3);">归档时间</span><span>' + (archivedDate || '—') + '</span></div>' +
          '<div style="display:flex;justify-content:space-between;padding:4px 0;"><span style="color:var(--text-3);">文件唯一标识</span><span style="font-family:var(--font-mono);font-size:12px;">' + (uid || '—') + '</span></div>';
      }
      if (btnPrimary) {
        btnPrimary.style.display = openlistUrl ? 'inline-flex' : 'none';
        btnPrimary.className = 'btn';
        btnPrimary.textContent = '🔗 直达网盘查看';
        btnPrimary.onclick = function () {
          window.open(openlistUrl, '_blank');
        };
      }
      if (btnSecondary) {
        btnSecondary.style.display = 'inline-flex';
        btnSecondary.className = 'btn primary';
        btnSecondary.textContent = '⚡ 强制重新下载';
        btnSecondary.onclick = function () {
          window.__closeBrowseAssetDrawer();
          if (window.__browseForceDownloadOne) {
            window.__browseForceDownloadOne(card);
          }
        };
      }
    } else if (isDownloaded) {
      if (iconEl) iconEl.textContent = '💾';
      if (headEl) headEl.textContent = '本地中转在存资产';
      if (promptEl) {
        promptEl.className = 'quick-status-banner info';
        promptEl.innerHTML = '💾 <b>提示：</b>该文件已在本地中转磁盘下载完成（本地在存），可随时进行播放或归档转存。';
      }
      if (detailsEl) {
        detailsEl.innerHTML = 
          '<div style="display:flex;justify-content:space-between;padding:4px 0;"><span style="color:var(--text-3);">存储状态</span><span style="color:var(--ok);font-weight:600;">本地磁盘就绪</span></div>' +
          '<div style="display:flex;justify-content:space-between;padding:4px 0;"><span style="color:var(--text-3);">本地文件路径</span><span style="font-family:var(--font-mono);word-break:break-all;">' + (localPath || '中转目录') + '</span></div>' +
          '<div style="display:flex;justify-content:space-between;padding:4px 0;"><span style="color:var(--text-3);">文件唯一标识</span><span style="font-family:var(--font-mono);font-size:12px;">' + (uid || '—') + '</span></div>';
      }
      if (btnPrimary) {
        btnPrimary.style.display = 'inline-flex';
        btnPrimary.className = 'btn';
        btnPrimary.textContent = '📂 查看本地资产';
        btnPrimary.onclick = function () {
          window.location.href = '/library/local';
        };
      }
      if (btnSecondary) {
        btnSecondary.style.display = 'inline-flex';
        btnSecondary.className = 'btn primary';
        btnSecondary.textContent = '⚡ 强制重新下载';
        btnSecondary.onclick = function () {
          window.__closeBrowseAssetDrawer();
          if (window.__browseForceDownloadOne) {
            window.__browseForceDownloadOne(card);
          }
        };
      }
    }

    backdrop.classList.add('show');
    document.body.classList.add('modal-open');
  };

  window.__closeBrowseAssetDrawer = function () {
    var backdrop = document.getElementById('browseAssetBackdrop');
    if (backdrop) backdrop.classList.remove('show');
    document.body.classList.remove('modal-open');
  };

  window.__showDedupInterceptDrawer = function (dupData, files) {
    var backdrop = document.getElementById('browseAssetBackdrop');
    if (!backdrop) return;

    var asset = (dupData && dupData.asset) || {};
    var dupType = (dupData && dupData.duplicateType) || 'cloud';
    var iconEl = document.getElementById('assetDrawerIcon');
    var headEl = document.getElementById('assetDrawerHeading');
    var nameEl = document.getElementById('assetDrawerName');
    var metaEl = document.getElementById('assetDrawerMeta');
    var promptEl = document.getElementById('assetDrawerPrompt');
    var detailsEl = document.getElementById('assetDrawerDetails');
    var btnPrimary = document.getElementById('assetDrawerActionPrimary');
    var btnSecondary = document.getElementById('assetDrawerActionSecondary');

    if (nameEl) nameEl.textContent = asset.filename || '检测到重复文件';
    if (metaEl) metaEl.textContent = '文件大小: ' + (asset.sizeHuman || '—') + ' · 指纹 ID: ' + (asset.uniqueId ? asset.uniqueId.slice(0, 16) + '...' : '—');

    if (dupType === 'cloud') {
      if (iconEl) iconEl.textContent = '☁️';
      if (headEl) headEl.textContent = '下载查重拦截 · 云端已归档';
      if (promptEl) {
        promptEl.className = 'quick-status-banner warning';
        promptEl.innerHTML = '⚠️ <b>拦截原因：</b>' + ((dupData && dupData.message) || '文件已在网盘中归档存储，系统已自动拦截避免重复下载。');
      }
      if (detailsEl) {
        detailsEl.innerHTML = 
          '<div style="display:flex;justify-content:space-between;padding:4px 0;"><span style="color:var(--text-3);">网盘盘符</span><span style="font-weight:600;">' + (asset.drive || '默认网盘') + '</span></div>' +
          '<div style="display:flex;justify-content:space-between;padding:4px 0;"><span style="color:var(--text-3);">网盘存储路径</span><span style="font-family:var(--font-mono);word-break:break-all;">' + (asset.cloudPath || '—') + '</span></div>' +
          '<div style="display:flex;justify-content:space-between;padding:4px 0;"><span style="color:var(--text-3);">归档时间</span><span>' + (asset.archivedDate || '—') + '</span></div>';
      }
      if (btnPrimary) {
        btnPrimary.style.display = asset.openlistUrl ? 'inline-flex' : 'none';
        btnPrimary.className = 'btn';
        btnPrimary.textContent = '🔗 直达网盘查看';
        btnPrimary.onclick = function () {
          window.open(asset.openlistUrl, '_blank');
        };
      }
      if (btnSecondary) {
        btnSecondary.style.display = 'inline-flex';
        btnSecondary.className = 'btn primary';
        btnSecondary.textContent = '⚡ 强制重新下载';
        btnSecondary.onclick = function () {
          window.__closeBrowseAssetDrawer();
          if (window.__browsePost) {
            window.__browsePost(files, '已强制提交下载任务', true);
          }
        };
      }
    } else {
      if (iconEl) iconEl.textContent = '💾';
      if (headEl) headEl.textContent = '下载查重拦截 · 本地在存';
      if (promptEl) {
        promptEl.className = 'quick-status-banner info';
        promptEl.innerHTML = '💾 <b>拦截原因：</b>' + ((dupData && dupData.message) || '该文件已在本地中转区存在，无需重复下载。');
      }
      if (detailsEl) {
        detailsEl.innerHTML = 
          '<div style="display:flex;justify-content:space-between;padding:4px 0;"><span style="color:var(--text-3);">存储状态</span><span style="color:var(--ok);font-weight:600;">本地磁盘在存</span></div>' +
          '<div style="display:flex;justify-content:space-between;padding:4px 0;"><span style="color:var(--text-3);">本地路径</span><span style="font-family:var(--font-mono);word-break:break-all;">' + (asset.localPath || '中转目录') + '</span></div>';
      }
      if (btnPrimary) {
        btnPrimary.style.display = 'inline-flex';
        btnPrimary.className = 'btn';
        btnPrimary.textContent = '📂 查看本地资产';
        btnPrimary.onclick = function () {
          window.location.href = '/library/local';
        };
      }
      if (btnSecondary) {
        btnSecondary.style.display = 'inline-flex';
        btnSecondary.className = 'btn primary';
        btnSecondary.textContent = '⚡ 强制重新下载';
        btnSecondary.onclick = function () {
          window.__closeBrowseAssetDrawer();
          if (window.__browsePost) {
            window.__browsePost(files, '已强制提交下载任务', true);
          }
        };
      }
    }

    backdrop.classList.add('show');
    document.body.classList.add('modal-open');
  };

  /* ---------------- 全局聚合搜索与 Spotlight Command Palette (Ctrl+K) ---------------- */
  var spotlightTimer = null;
  var currentSearchResults = null;
  var currentSearchTab = 'all';
  var selectedResultIndex = -1;

  window.__openSpotlightSearch = function () {
    var backdrop = document.getElementById('spotlightModalBackdrop');
    var input = document.getElementById('spotlightInput');
    if (!backdrop || !input) return;
    backdrop.classList.add('show');
    document.body.classList.add('modal-open');
    input.value = '';
    window.__resetSpotlightResults();
    setTimeout(function () { input.focus(); }, 50);
  };

  window.__closeSpotlightSearch = function () {
    var backdrop = document.getElementById('spotlightModalBackdrop');
    if (backdrop) backdrop.classList.remove('show');
    document.body.classList.remove('modal-open');
  };

  window.__toggleSpotlightSearch = function () {
    if (window.__isSpotlightOpen()) {
      window.__closeSpotlightSearch();
    } else {
      window.__openSpotlightSearch();
    }
  };

  window.__isSpotlightOpen = function () {
    var backdrop = document.getElementById('spotlightModalBackdrop');
    return backdrop && backdrop.classList.contains('show');
  };

  window.__resetSpotlightResults = function () {
    var placeholder = document.getElementById('spotlightPlaceholder');
    var content = document.getElementById('spotlightContent');
    var countAll = document.getElementById('tabCountAll');
    var countTasks = document.getElementById('tabCountTasks');
    var countLocal = document.getElementById('tabCountLocal');
    var countCloud = document.getElementById('tabCountCloud');

    if (placeholder) placeholder.style.display = 'flex';
    if (content) { content.style.display = 'none'; content.innerHTML = ''; }
    if (countAll) countAll.textContent = '0';
    if (countTasks) countTasks.textContent = '0';
    if (countLocal) countLocal.textContent = '0';
    if (countCloud) countCloud.textContent = '0';
    currentSearchResults = null;
    currentSearchTab = 'all';
    selectedResultIndex = -1;
    document.querySelectorAll('.spotlight-tab').forEach(function (t) {
      t.classList.toggle('active', t.dataset.tab === 'all');
    });
  };

  window.__setSpotlightTab = function (tab) {
    currentSearchTab = tab;
    document.querySelectorAll('.spotlight-tab').forEach(function (t) {
      t.classList.toggle('active', t.dataset.tab === tab);
    });
    window.__renderSpotlightResults();
  };

  window.__onSpotlightInput = function (val) {
    if (spotlightTimer) clearTimeout(spotlightTimer);
    var q = (val || '').trim();
    if (!q) {
      window.__resetSpotlightResults();
      return;
    }

    var spinner = document.getElementById('spotlightSpinner');
    if (spinner) spinner.style.display = 'inline-block';

    spotlightTimer = setTimeout(function () {
      fetch('/api/search/aggregate?q=' + encodeURIComponent(q))
        .then(function (r) { return r.json(); })
        .then(function (d) {
          if (spinner) spinner.style.display = 'none';
          if (d && d.ok) {
            currentSearchResults = d;
            var cAll = document.getElementById('tabCountAll');
            var cTasks = document.getElementById('tabCountTasks');
            var cLocal = document.getElementById('tabCountLocal');
            var cCloud = document.getElementById('tabCountCloud');
            if (cAll) cAll.textContent = d.total || 0;
            if (cTasks) cTasks.textContent = (d.counts && d.counts.tasks) || 0;
            if (cLocal) cLocal.textContent = (d.counts && d.counts.local) || 0;
            if (cCloud) cCloud.textContent = (d.counts && d.counts.cloud) || 0;
            window.__renderSpotlightResults();
          }
        })
        .catch(function () {
          if (spinner) spinner.style.display = 'none';
        });
    }, 250);
  };

  window.__renderSpotlightResults = function () {
    var placeholder = document.getElementById('spotlightPlaceholder');
    var content = document.getElementById('spotlightContent');
    if (!content) return;

    if (!currentSearchResults || !currentSearchResults.total) {
      if (placeholder) {
        placeholder.style.display = 'flex';
        placeholder.innerHTML = '<div style="color:var(--text-3);padding:24px 0;">未找到匹配的文件资产</div>';
      }
      content.style.display = 'none';
      content.innerHTML = '';
      return;
    }

    if (placeholder) placeholder.style.display = 'none';
    content.style.display = 'block';

    var results = currentSearchResults.results || {};
    var tasks = results.tasks || [];
    var local = results.local || [];
    var cloud = results.cloud || [];

    var html = '';
    var itemIndex = 0;

    // 1. 任务队列栏目
    if ((currentSearchTab === 'all' || currentSearchTab === 'tasks') && tasks.length) {
      html += '<div class="spotlight-section">';
      html += '<div class="spotlight-section-head"><span>任务队列 (' + tasks.length + ')</span></div>';
      tasks.forEach(function (t) {
        var statusLabel = t.status === 'completed' ? '已完成' : (t.status === 'downloading' ? '下载中 ' + t.progress + '%' : t.status);
        html += '<div class="spotlight-item" data-index="' + (itemIndex++) + '" data-url="' + t.actionUrl + '" onclick="location.href=\'' + t.actionUrl + '\'">';
        html += '  <div class="spotlight-item-left">';
        html += '    <span class="spotlight-item-icon">⏳</span>';
        html += '    <div class="spotlight-item-text">';
        html += '      <div class="spotlight-item-title">' + (t.filename || '任务') + '</div>';
        html += '      <div class="spotlight-item-sub"><span>' + (t.source || '会话') + '</span><span>' + (t.sizeHuman || '—') + '</span><span class="badge accent">' + statusLabel + '</span></div>';
        html += '    </div>';
        html += '  </div>';
        html += '  <div class="spotlight-item-right">';
        html += '    <button type="button" class="btn xs ghost" onclick="event.stopPropagation(); location.href=\'' + t.actionUrl + '\'">查看任务</button>';
        html += '  </div>';
        html += '</div>';
      });
      html += '</div>';
    }

    // 2. 本地在存资产栏目
    if ((currentSearchTab === 'all' || currentSearchTab === 'local') && local.length) {
      html += '<div class="spotlight-section">';
      html += '<div class="spotlight-section-head"><span>本地在存资产 (' + local.length + ')</span></div>';
      local.forEach(function (l) {
        html += '<div class="spotlight-item" data-index="' + (itemIndex++) + '" data-url="' + l.actionUrl + '" onclick="location.href=\'' + l.actionUrl + '\'">';
        html += '  <div class="spotlight-item-left">';
        html += '    <span class="spotlight-item-icon">💾</span>';
        html += '    <div class="spotlight-item-text">';
        html += '      <div class="spotlight-item-title">' + (l.filename || '本地在存文件') + '</div>';
        html += '      <div class="spotlight-item-sub"><span>' + (l.sizeHuman || '—') + '</span><span style="font-family:var(--font-mono);">' + (l.localPath || '本地磁盘') + '</span></div>';
        html += '    </div>';
        html += '  </div>';
        html += '  <div class="spotlight-item-right">';
        html += '    <button type="button" class="btn xs ghost" onclick="event.stopPropagation(); location.href=\'/library/local\'">本地查看</button>';
        html += '  </div>';
        html += '</div>';
      });
      html += '</div>';
    }

    // 3. 云端网盘归档栏目
    if ((currentSearchTab === 'all' || currentSearchTab === 'cloud') && cloud.length) {
      html += '<div class="spotlight-section">';
      html += '<div class="spotlight-section-head"><span>云端网盘归档 (' + cloud.length + ')</span></div>';
      cloud.forEach(function (c) {
        var targetUrl = c.openlistUrl || c.actionUrl;
        html += '<div class="spotlight-item" data-index="' + (itemIndex++) + '" data-url="' + targetUrl + '" onclick="window.open(\'' + targetUrl + '\', \'_blank\')">';
        html += '  <div class="spotlight-item-left">';
        html += '    <span class="spotlight-item-icon">☁️</span>';
        html += '    <div class="spotlight-item-text">';
        html += '      <div class="spotlight-item-title">' + (c.filename || '云端归档文件') + '</div>';
        html += '      <div class="spotlight-item-sub"><span class="badge ok">' + (c.drive || '网盘') + '</span><span style="font-family:var(--font-mono);">' + (c.cloudPath || '—') + '</span></div>';
        html += '    </div>';
        html += '  </div>';
        html += '  <div class="spotlight-item-right">';
        if (c.openlistUrl) {
          html += '    <button type="button" class="btn xs" onclick="event.stopPropagation(); window.open(\'' + c.openlistUrl + '\', \'_blank\')">直达网盘</button>';
        }
        html += '    <button type="button" class="btn xs ghost" onclick="event.stopPropagation(); location.href=\'/library/cloud\'">云端中心</button>';
        html += '  </div>';
        html += '</div>';
      });
      html += '</div>';
    }

    content.innerHTML = html;
    selectedResultIndex = -1;
  };

  window.__onSpotlightKeydown = function (e) {
    var items = document.querySelectorAll('.spotlight-item');
    if (!items.length) return;

    if (e.key === 'ArrowDown') {
      e.preventDefault();
      selectedResultIndex = (selectedResultIndex + 1) % items.length;
      updateActiveItem();
    } else if (e.key === 'ArrowUp') {
      e.preventDefault();
      selectedResultIndex = (selectedResultIndex - 1 + items.length) % items.length;
      updateActiveItem();
    } else if (e.key === 'Enter') {
      e.preventDefault();
      if (selectedResultIndex >= 0 && selectedResultIndex < items.length) {
        items[selectedResultIndex].click();
      }
    }

    function updateActiveItem() {
      items.forEach(function (el, idx) {
        el.classList.toggle('active', idx === selectedResultIndex);
        if (idx === selectedResultIndex) {
          el.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
        }
      });
    }
  };

  // 全局快捷键监听（Ctrl+K / Cmd+K 与 Esc）
  if (typeof document !== 'undefined') {
    document.addEventListener('keydown', function (e) {
      if ((e.ctrlKey || e.metaKey) && (e.key === 'k' || e.key === 'K')) {
        e.preventDefault();
        window.__toggleSpotlightSearch();
      }
      if (e.key === 'Escape' && window.__isSpotlightOpen()) {
        window.__closeSpotlightSearch();
      }
    });
  }

  /* ---------------- 归档异常聚合视图与一键批量重试面板 ---------------- */
  window.archiveFailurePanel = function archiveFailurePanel() {
    return {
      total: 0,
      categories: {
        token_expired: 0,
        storage_full: 0,
        conflict: 0,
        timeout: 0,
        unknown: 0,
        source_missing: 0,
        remote_changed: 0,
        remote_missing: 0
      },
      failedJobs: [],
      busy: false,
      // 可重试的失败数：永久性错误（本地文件已丢失等）不计入。
      // 用它驱动「一键重试」按钮的可用态，避免用户对着注定失败的任务反复点。
      get retryableCount() {
        return this.failedJobs.filter(function (j) { return j.retryable !== false; }).length;
      },
      load: function () {
        var self = this;
        fetch('/api/archive/failed', { headers: { 'Accept': 'application/json' } })
          .then(function (r) { return r.json(); })
          .then(function (d) {
            if (d && d.ok && d.summary) {
              self.total = d.summary.total || 0;
              self.categories = d.summary.categories || self.categories;
              self.failedJobs = d.failedJobs || [];
            }
          })
          .catch(function () {});
      },
      get filteredJobs() {
        if (this.activeCategory === 'all') return this.failedJobs;
        var cat = this.activeCategory;
        return this.failedJobs.filter(function (j) { return j.category === cat; });
      },
      setCategory: function (cat) {
        this.activeCategory = cat;
      },
      retryAll: function () {
        var self = this;
        self.busy = true;
        fetch('/api/archive/retry-failed', {
          method: 'POST',
          headers: {
            'Content-Type': 'application/json',
            'X-CSRF-Token': window.__csrfToken ? window.__csrfToken() : ''
          },
          body: JSON.stringify({ category: self.activeCategory, forceOverwrite: true })
        })
          .then(function (r) { return r.json(); })
          .then(function (d) {
            self.busy = false;
            if (d && d.ok) {
              var extra = (d.blockedCount ? ('（另有 ' + d.blockedCount + ' 个因不可重试被跳过）') : '');
              window.__toast('批量重试完成', 'success',
                (d.message || ('已重新调度 ' + d.retriedCount + ' 个归档任务')) + extra);
              self.load();
              if (window.__browseReload) window.__browseReload();
            } else {
              // NOT_RETRYABLE：后端明确告知为何不能重试（如本地文件已丢失），
              // 原样透传给用户，而不是含糊的「重试失败」。
              window.__toast(d && d.code === 'NOT_RETRYABLE' ? '无法重试' : '重试失败',
                             'error', (d && d.message) || '无法重新调度');
              self.load();
            }
          })
          .catch(function (e) {
            self.busy = false;
            window.__toast('重试请求失败', 'error', '网络异常: ' + e);
          });
      },
      retryOne: function (id) {
        var self = this;
        fetch('/api/archive/retry-failed', {
          method: 'POST',
          headers: {
            'Content-Type': 'application/json',
            'X-CSRF-Token': window.__csrfToken ? window.__csrfToken() : ''
          },
          body: JSON.stringify({ jobIds: [id], forceOverwrite: true })
        })
          .then(function (r) { return r.json(); })
          .then(function (d) {
            if (d && d.ok) {
              window.__toast('任务已重新排队', 'success', '已重新调度该任务');
              self.load();
            } else {
              window.__toast('重试失败', 'error', (d && d.message) || '无法重新调度');
            }
          })
          .catch(function () {});
      }
    };
  };

  /* ---------------- 页面加载兜底与 htmx 事件联动 ---------------- */
  if (typeof document !== 'undefined') {
    if (!window.__DEFAULT_ARCH_DIR) {
      fetch('/archive/config', { headers: { 'Accept': 'application/json' } })
        .then(function (r) { return r.json(); })
        .then(function (d) {
          if (d && d.ok && d.config && d.config.defaultDir) {
            window.__DEFAULT_ARCH_DIR = d.config.defaultDir;
          }
        })
        .catch(function () {});
    }
    if (document.querySelector('[data-arch-btn], [data-arch-pill]')) {
      __archPollStart();
    }
    if (document.querySelector('[data-retrieve-btn], [data-ret-pill]')) {
      __retrievePollStart();
    }
    if (document.querySelector('.local-check') && window.__localUpdateSelection) {
      window.__localUpdateSelection();
    }
    if (firstBoot) {
      document.addEventListener('htmx:afterSwap', function (evt) {
        if (evt.detail && evt.detail.target && evt.detail.target.id === 'localFiles') {
          if (window.__localClearSelection) window.__localClearSelection();
        }
      });
    }
  }
})();
