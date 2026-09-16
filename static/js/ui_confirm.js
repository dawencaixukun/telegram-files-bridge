/* ============================================================
 * 全局确认弹窗（替代原生 window.confirm）
 * 用法：__uiConfirm({ title, body, okText, danger, meta }) -> Promise<boolean>
 *  - danger=true 时确认按钮为红色危险样式，默认焦点在「取消」
 *  - Esc / 点遮罩 = 取消；回车 = 确认
 * ============================================================ */
(function () {
  'use strict';

  var el = null;
  var currentResolve = null;

  function ensureDom() {
    if (el) return;
    el = document.createElement('div');
    el.className = 'modal-backdrop ui-confirm-backdrop';
    el.style.zIndex = '120';
    el.innerHTML =
      '<div class="modal ui-confirm" role="alertdialog" aria-modal="true" aria-labelledby="uiConfirmTitle">' +
      '  <div class="ui-confirm-head">' +
      '    <span class="ui-confirm-icon" aria-hidden="true"><svg class="icon"><use href="#ic-alert"/></svg></span>' +
      '    <h2 id="uiConfirmTitle"></h2>' +
      '  </div>' +
      '  <div class="ui-confirm-body"></div>' +
      '  <div class="modal-foot ui-confirm-foot">' +
      '    <button type="button" class="btn ghost" data-act="cancel">取消</button>' +
      '    <button type="button" class="btn" data-act="ok">确定</button>' +
      '  </div>' +
      '</div>';
    document.body.appendChild(el);

    el.addEventListener('click', function (ev) {
      if (ev.target === el) { settle(false); return; }
      var btn = ev.target.closest('[data-act]');
      if (!btn) return;
      settle(btn.getAttribute('data-act') === 'ok');
    });
    document.addEventListener('keydown', function (ev) {
      if (!el.classList.contains('show')) return;
      if (ev.key === 'Escape') { ev.preventDefault(); settle(false); }
      else if (ev.key === 'Enter' && ev.target !== document.getElementById('uiPromptInput')) { ev.preventDefault(); settle(true); }
    });
  }

  function settle(ok) {
    if (!currentResolve) return;
    var r = currentResolve;
    currentResolve = null;
    el.classList.remove('show');
    r(ok);
  }

  window.__uiConfirm = function (opts) {
    ensureDom();
    opts = opts || {};
    if (currentResolve) currentResolve(false); // 上一个未决的按取消处理
    return new Promise(function (resolve) {
      currentResolve = resolve;

      el.querySelector('#uiConfirmTitle').textContent = opts.title || '确认操作';
      var bodyEl = el.querySelector('.ui-confirm-body');
      bodyEl.innerHTML = '';
      var body = opts.body || '';
      if (body) {
        var p = document.createElement('div');
        p.className = 'ui-confirm-text';
        p.textContent = body;
        bodyEl.appendChild(p);
      }
      (opts.meta || []).forEach(function (row) {
        var line = document.createElement('div');
        line.className = 'ui-confirm-meta';
        var k = document.createElement('span');
        k.className = 'ui-confirm-meta-k';
        k.textContent = row[0];
        var v = document.createElement('span');
        v.className = 'ui-confirm-meta-v';
        v.textContent = row[1];
        line.appendChild(k); line.appendChild(v);
        bodyEl.appendChild(line);
      });
      if (opts.warn) {
        var w = document.createElement('div');
        w.className = 'ui-confirm-warn';
        w.textContent = opts.warn;
        bodyEl.appendChild(w);
      }

      var okBtn = el.querySelector('[data-act="ok"]');
      var cancelBtn = el.querySelector('[data-act="cancel"]');
      okBtn.textContent = opts.okText || '确定';
      okBtn.className = opts.danger ? 'btn danger' : 'btn primary';
      cancelBtn.textContent = opts.cancelText || '取消';
      el.querySelector('.ui-confirm-icon').className =
        'ui-confirm-icon' + (opts.danger ? ' danger' : '');

      el.classList.add('show');
      // 危险操作默认焦点落在「取消」防误触；普通确认落「确定」
      setTimeout(function () { (opts.danger ? cancelBtn : okBtn).focus(); }, 30);
    });
  };

  /* ---- 输入弹窗：__uiPrompt({ title, label, value, okText, hint, meta }) -> Promise<string|null> ----
   * 确认 → 输入值（trim 后）；取消/Esc/遮罩 → null。复用 __uiConfirm 的骨架与样式。 */
  window.__uiPrompt = function (opts) {
    ensureDom();
    opts = opts || {};
    if (currentResolve) currentResolve(null); // 上一个未决的按取消处理
    return new Promise(function (resolve) {
      currentResolve = null; // prompt 自己管理 resolve（带值）
      var promptResolve = resolve;

      el.querySelector('#uiConfirmTitle').textContent = opts.title || '输入';
      var bodyEl = el.querySelector('.ui-confirm-body');
      bodyEl.innerHTML = '';
      (opts.meta || []).forEach(function (row) {
        var line = document.createElement('div');
        line.className = 'ui-confirm-meta';
        var k = document.createElement('span');
        k.className = 'ui-confirm-meta-k';
        k.textContent = row[0];
        var v = document.createElement('span');
        v.className = 'ui-confirm-meta-v';
        v.textContent = row[1];
        line.appendChild(k); line.appendChild(v);
        bodyEl.appendChild(line);
      });
      if (opts.hint) {
        var h = document.createElement('div');
        h.className = 'ui-confirm-text';
        h.style.opacity = '0.6';
        h.style.fontSize = '12.5px';
        h.textContent = opts.hint;
        bodyEl.appendChild(h);
      }
      var wrap = document.createElement('div');
      wrap.className = 'ui-confirm-field';
      var lbl = document.createElement('label');
      lbl.textContent = opts.label || '请输入';
      lbl.htmlFor = 'uiPromptInput';
      var inp = document.createElement('input');
      inp.id = 'uiPromptInput';
      inp.type = 'text';
      inp.className = 'input';
      inp.value = String(opts.value == null ? '' : opts.value);
      inp.setAttribute('autocomplete', 'off');
      inp.setAttribute('spellcheck', 'false');
      wrap.appendChild(lbl);
      wrap.appendChild(inp);
      bodyEl.appendChild(wrap);
      if (opts.warn) {
        var w = document.createElement('div');
        w.className = 'ui-confirm-warn';
        w.textContent = opts.warn;
        bodyEl.appendChild(w);
      }

      var okBtn = el.querySelector('[data-act="ok"]');
      var cancelBtn = el.querySelector('[data-act="cancel"]');
      okBtn.textContent = opts.okText || '确定';
      okBtn.className = 'btn primary';
      cancelBtn.textContent = opts.cancelText || '取消';
      el.querySelector('.ui-confirm-icon').className = 'ui-confirm-icon';

      var keyHandler = function (ev) {
        if (!el.classList.contains('show')) return;
        if (ev.key === 'Escape') {
          ev.preventDefault();
          cleanup();
          el.classList.remove('show');
          promptResolve(null);
        } else if (ev.key === 'Enter' && ev.target === inp) {
          ev.preventDefault();
          cleanup();
          el.classList.remove('show');
          promptResolve(inp.value);
        }
      };
      var cleanup = function () {
        document.removeEventListener('keydown', keyHandler, true);
      };
      document.addEventListener('keydown', keyHandler, true);

      // 接管 backdrop 的按钮点击（prompt 模式下返回输入值而非 true/false）
      var clickHandler = function (ev) {
        var btn = ev.target.closest('[data-act]');
        if (!btn) return;
        var act = btn.getAttribute('data-act');
        if (act === 'ok') {
          cleanup();
          promptResolve(inp.value);
        } else if (act === 'cancel' || ev.target === el) {
          cleanup();
          promptResolve(null);
        }
      };
      el.addEventListener('click', clickHandler);
      var oldCleanup = cleanup;
      cleanup = function () {
        oldCleanup();
        el.removeEventListener('click', clickHandler);
      };

      el.classList.add('show');
      setTimeout(function () {
        inp.focus();
        var dot = inp.value.lastIndexOf('.');
        inp.setSelectionRange(0, dot > 0 ? dot : inp.value.length); // 默认全选主名，后缀不动
      }, 30);
    });
  };
})();
