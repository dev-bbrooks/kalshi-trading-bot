/* terminal.js — Terminal/Dev Tools JavaScript extracted from dashboard.py */
(function() {
// Expose shared state on window for dashboard.py access
window._devSidebarOpen = false;
window._devLastPanel = 'claude';
window._tState = 'none';
(function() {
  var _swStartX = 0, _swStartY = 0, _swTracking = false;

  document.addEventListener('touchstart', function(e) {
    var target = e.target;
    if (target.closest('textarea, input, select, .confirm-overlay')) { _swTracking = false; return; }
    _swTracking = true;
    _swStartX = e.touches[0].clientX;
    _swStartY = e.touches[0].clientY;
  }, {passive: true});

  document.addEventListener('touchend', function(e) {
    if (!_swTracking) return;
    _swTracking = false;
    var t = e.changedTouches[0];
    var dx = t.clientX - _swStartX, dy = t.clientY - _swStartY;
    if (Math.abs(dx) < 50 || Math.abs(dx) <= Math.abs(dy)) return;

    var leftOpen = document.body.classList.contains('sidebar-open');

    if (dx > 0) {
      // Swipe right
      if (_devSidebarOpen) closeDevSidebar();
      else if (!leftOpen) toggleSidebar();
    } else {
      // Swipe left
      if (leftOpen) closeSidebar();
      else if (!_devSidebarOpen) openDevSidebar();
    }
  }, {passive: true});
})();
window.openDevSidebar = function() {
  closeSidebar();
  _devSidebarOpen = true;
  document.getElementById('devSidebar').classList.add('open');
  document.getElementById('devSidebarOverlay').classList.add('open');
  document.body.classList.add('dev-sidebar-open');
  // Highlight current panel in nav — only if dev panels are actually visible
  var devPanelsActive = document.getElementById('devPanels').classList.contains('active');
  document.querySelectorAll('.dev-sidebar-item').forEach(function(item) {
    item.classList.toggle('active', devPanelsActive && item.dataset.panel === _devLastPanel);
  });
  var dot = document.getElementById('devSidebar-claude-dot');
  if (dot) dot.className = 'claude-dot cdot-' + (_tState || 'none');
  var newBtn = document.getElementById('devSidebar-new-btn');
  if (newBtn) newBtn.style.display = _devLastPanel === 'claude' ? 'flex' : 'none';
};
window.closeDevSidebar = function() {
  _devSidebarOpen = false;
  document.getElementById('devSidebar').classList.remove('open');
  document.getElementById('devSidebarOverlay').classList.remove('open');
  document.body.classList.remove('dev-sidebar-open');
};
window.devSidebarNav = function(panel) {
  _devLastPanel = panel;
  // Close the sidebar (same as left sidebar pattern)
  closeDevSidebar();
  // Clear left sidebar highlighting when switching to a dev panel
  document.querySelectorAll('.sidebar-item').forEach(function(item) { item.classList.remove('active'); });
  // Initialize terminal if needed
  try { _termInit(); } catch(e) { console.error('Terminal init error:', e); }
  // Show the dev panels container
  var container = document.getElementById('devPanels');
  container.classList.add('active');
  // Switch to the right panel
  document.querySelectorAll('#devPanels .term-panel').forEach(function(p) { p.classList.remove('active'); });
  var target = document.getElementById('term-' + panel + '-panel');
  if (target) target.classList.add('active');
  // Activate scrollers
  if (window._devNavActivate) window._devNavActivate(panel);
};
// ═══════════════════════════════════════════════════
//  PanelScroller — reusable scroll/keyboard/input manager
// ═══════════════════════════════════════════════════
class PanelScroller {
  constructor(opts) {
    this._scrollEl = opts.scrollEl;
    this._scrollEl.style.webkitOverflowScrolling = 'touch';
    this._panelEl = opts.panelEl || null;
    this._inputAreaEl = opts.inputAreaEl || null;
    this._textareaEl = opts.textareaEl || null;
    this._sendBtnEl = opts.sendBtnEl || null;
    this._enhanceSendBtnEl = opts.enhanceSendBtnEl || null;
    this._pasteBtnEl = opts.pasteBtnEl || null;

    this._headerEl = opts.headerEl || null;
    this._contentWrapEl = opts.contentWrapEl || null;
    this._onSend = opts.onSend || null;
    this._onEnhanceSend = opts.onEnhanceSend || null;
    this._onStop = opts.onStop || null;
    this._onLoadOlder = opts.onLoadOlder || null;
    this._onAdjustLayout = opts.onAdjustLayout || null;
    this._isBusy = opts.isBusy || function() { return false; };
    this._sendIconSvg = opts.sendIconSvg || '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><line x1="12" y1="19" x2="12" y2="5"/><polyline points="5 12 12 5 19 12"/></svg>';
    this._stopIconSvg = opts.stopIconSvg || '<svg width="18" height="18" viewBox="0 0 24 24" fill="currentColor"><rect x="4" y="4" width="16" height="16" rx="2"/></svg>';
    this._enhanceIconSvg = '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="7 15 12 10 17 15"/><polyline points="7 9 12 4 17 9"/></svg>';

    this._autoScroll = true;
    this._active = false;
    this._loading = false;
    this._sendFired = false;
    this._touchY = 0;
    this._touchEl = null;
    this._dragDismissed = false;

    // Bind methods for add/removeEventListener
    this._onScroll = this._handleScroll.bind(this);
    this._onFocus = this._handleFocus.bind(this);
    this._onBlur = this._handleBlur.bind(this);
    this._onInput = this._handleInput.bind(this);
    this._onKeydown = this._handleKeydown.bind(this);
    this._onSendTouch = this._handleSendTouch.bind(this);
    this._onSendClick = this._handleSendClick.bind(this);
    this._onEnhanceSendTouch = this._handleEnhanceSendTouch.bind(this);
    this._onEnhanceSendClick = this._handleEnhanceSendClick.bind(this);
    this._enhanceSendFired = false;
    this._onPaste = this._handlePaste.bind(this);
    this._onTouchStart = this._handleTouchStart.bind(this);
    this._onTouchMove = this._handleTouchMove.bind(this);
    this._onTouchEnd = this._handleTouchEnd.bind(this);

    // Always attach scroll listener
    this._scrollEl.addEventListener('scroll', this._onScroll);

    // Attach input listeners if textarea provided
    if (this._textareaEl) {
      this._textareaEl.addEventListener('input', this._onInput);
      this._textareaEl.addEventListener('keydown', this._onKeydown);
      this._textareaEl.addEventListener('focus', this._onFocus);
      this._textareaEl.addEventListener('blur', this._onBlur);
      // Block vertical scroll propagation from textarea without breaking text selection
      this._textareaEl.addEventListener('touchmove', function(e) {
        var el = e.currentTarget;
        var ty = e.touches[0].clientY;
        var tx = e.touches[0].clientX;
        var dy = Math.abs(ty - (el._lastTouchY || ty));
        var dx = Math.abs(tx - (el._lastTouchX || tx));
        el._lastTouchY = ty;
        el._lastTouchX = tx;
        // Horizontal moves are text selection — never block
        if (dx > dy) return;
        // Vertical: block overscroll propagation to page
        if (el.scrollHeight <= el.clientHeight) { e.preventDefault(); return; }
        if (el.scrollTop === 0 && ty > (el._prevTouchY || 0)) e.preventDefault();
        if (el.scrollTop + el.clientHeight >= el.scrollHeight && ty < (el._prevTouchY || 0)) e.preventDefault();
        el._prevTouchY = ty;
      }, {passive: false});
      this._textareaEl.addEventListener('touchstart', function(e) {
        e.currentTarget._lastTouchY = e.touches[0].clientY;
        e.currentTarget._lastTouchX = e.touches[0].clientX;
        e.currentTarget._prevTouchY = e.touches[0].clientY;
      }, {passive: true});
    }
    if (this._sendBtnEl) {
      this._sendBtnEl.addEventListener('touchstart', this._onSendTouch, {passive: false});
      this._sendBtnEl.addEventListener('click', this._onSendClick);
    }
    if (this._enhanceSendBtnEl) {
      this._enhanceSendBtnEl.addEventListener('touchstart', this._onEnhanceSendTouch, {passive: false});
      this._enhanceSendBtnEl.addEventListener('click', this._onEnhanceSendClick);
    }
    if (this._pasteBtnEl && this._textareaEl) {
      this._pasteBtnEl.addEventListener('click', this._onPaste);
    }
    // Touch dismiss — document-level, guarded by _active
    document.addEventListener('touchstart', this._onTouchStart, {passive: true});
    document.addEventListener('touchmove', this._onTouchMove, {passive: true});
    document.addEventListener('touchend', this._onTouchEnd);

    this._footerEl = this._inputAreaEl;

    // visualViewport keyboard handling — keep page stationary, only move footer
    this._vvHandler = null;
    if (window.visualViewport && this._footerEl) {
      var self = this;
      this._vvHandler = function() {
        if (!self._active) return;
        // Prevent iOS from scrolling the page up
        window.scrollTo(0, 0);
        var vv = window.visualViewport;
        var panelH = self._panelEl ? self._panelEl.clientHeight : window.innerHeight;
        var kbH = Math.max(0, panelH - vv.height - vv.offsetTop);
        if (kbH > 50) {
          self._footerEl.style.bottom = kbH + 'px';
          self._footerEl.style.marginBottom = '0';
          var footerH = self._footerEl.offsetHeight || 130;
          self._scrollEl.style.setProperty('--_conv-after', (kbH + footerH + 8) + 'px');
          self._scrollEl.scrollTop = self._scrollEl.scrollHeight;
        } else {
          self._footerEl.style.bottom = '';
          self._footerEl.style.marginBottom = '';
          self._scrollEl.style.removeProperty('--_conv-after');
          // After keyboard closes, scroll to bottom to keep last message visible
          setTimeout(function() { self._scrollEl.scrollTop = self._scrollEl.scrollHeight; }, 100);
        }
      };
      window.visualViewport.addEventListener('resize', this._vvHandler);
      window.visualViewport.addEventListener('scroll', this._vvHandler);
    }
  }

  // ── Public API ──

  scrollToBottom(force) {
    if (force || this._autoScroll) this._scrollEl.scrollTop = this._scrollEl.scrollHeight;
  }

  isNearBottom() { return this._autoScroll; }

  prependContent(fn) {
    var ph = this._scrollEl.scrollHeight;
    fn();
    this._scrollEl.scrollTop += this._scrollEl.scrollHeight - ph;
  }

  resetInput() {
    if (!this._textareaEl) return;
    this._textareaEl.value = '';
    this._autoResize();
    this.updateSendBtn();
  }

  updateSendBtn() {
    if (!this._sendBtnEl || !this._textareaEl) return;
    var hasText = !!this._textareaEl.value.trim();
    if (this._isBusy()) {
      this._sendBtnEl.innerHTML = this._stopIconSvg;
      this._sendBtnEl.classList.add('stop-btn');
      this._sendBtnEl.disabled = false;
      if (this._enhanceSendBtnEl) this._enhanceSendBtnEl.disabled = true;
    } else {
      this._sendBtnEl.innerHTML = this._sendIconSvg;
      this._sendBtnEl.classList.remove('stop-btn');
      this._sendBtnEl.disabled = !hasText;
      if (this._enhanceSendBtnEl) this._enhanceSendBtnEl.disabled = !hasText;
    }
  }

  setActive(active) { this._active = active; }

  destroy() {
    this._scrollEl.removeEventListener('scroll', this._onScroll);
    if (this._textareaEl) {
      this._textareaEl.removeEventListener('input', this._onInput);
      this._textareaEl.removeEventListener('keydown', this._onKeydown);
      this._textareaEl.removeEventListener('focus', this._onFocus);
      this._textareaEl.removeEventListener('blur', this._onBlur);
    }
    if (this._sendBtnEl) {
      this._sendBtnEl.removeEventListener('touchstart', this._onSendTouch);
      this._sendBtnEl.removeEventListener('click', this._onSendClick);
    }
    if (this._pasteBtnEl) this._pasteBtnEl.removeEventListener('click', this._onPaste);
    document.removeEventListener('touchstart', this._onTouchStart);
    document.removeEventListener('touchmove', this._onTouchMove);
    document.removeEventListener('touchend', this._onTouchEnd);
    if (this._vvHandler && window.visualViewport) {
      window.visualViewport.removeEventListener('resize', this._vvHandler);
      window.visualViewport.removeEventListener('scroll', this._vvHandler);
    }
  }

  // ── Private handlers ──

  _handleScroll() {
    this._autoScroll = this._scrollEl.scrollTop + this._scrollEl.clientHeight >= this._scrollEl.scrollHeight - 30;
    if (this._onLoadOlder && !this._loading && this._scrollEl.scrollTop < this._scrollEl.clientHeight * 3) {
      this._loading = true;
      var self = this;
      Promise.resolve(this._onLoadOlder()).then(function(res) {
        if (res && res.done) self._onLoadOlder = null;
        self._loading = false;
      }).catch(function() { self._loading = false; });
    }
  }

  _handleFocus() {
    if (!this._active) return;
    _termKeyboardOpen = true;
    var el = this._scrollEl;
    setTimeout(function() { el.scrollTop = el.scrollHeight; }, 300);
    setTimeout(function() { el.scrollTop = el.scrollHeight; }, 600);
  }

  _handleBlur() {
    if (!this._active) return;
    _termKeyboardOpen = false;
    if (this._onAdjustLayout) this._onAdjustLayout();
    this._scrollEl.scrollTop = this._scrollEl.scrollHeight;
  }

  _handleInput() {
    this._autoResize();
    this.updateSendBtn();
  }

  _handleKeydown(e) {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); this._doSend(); }
  }

  _handleSendTouch(e) {
    e.preventDefault();
    this._sendFired = true;
    this._doSend();
  }

  _handleSendClick() {
    if (!this._sendFired) this._doSend();
    this._sendFired = false;
  }

  _handleEnhanceSendTouch(e) {
    e.preventDefault();
    this._enhanceSendFired = true;
    this._doEnhanceSend();
  }

  _handleEnhanceSendClick() {
    if (!this._enhanceSendFired) this._doEnhanceSend();
    this._enhanceSendFired = false;
  }

  _doEnhanceSend() {
    if (this._isBusy()) return;
    if (!this._textareaEl) return;
    var text = this._textareaEl.value.trim();
    if (!text) return;
    this._textareaEl.value = '';
    this._autoResize();
    this.updateSendBtn();
    this._textareaEl.blur();
    if (this._onEnhanceSend) this._onEnhanceSend(text);
  }

  _handlePaste() {
    var self = this;
    if (navigator.clipboard && navigator.clipboard.readText) {
      navigator.clipboard.readText().then(function(t) {
        if (t && self._textareaEl) {
          self._textareaEl.value += t;
          self._autoResize();
          self.updateSendBtn();
        }
      }).catch(function(){});
    }
  }

  _handleTouchStart(e) {
    if (!this._active || !this._textareaEl) return;
    this._touchX = e.touches[0].clientX;
    this._touchY = e.touches[0].clientY;
    this._touchEl = e.target;
    this._dragDismissed = false;
  }

  _handleTouchMove(e) {
    if (!this._active || this._dragDismissed || document.activeElement !== this._textareaEl) return;
    if (!this._touchEl || !this._inputAreaEl || !this._touchEl.closest('#' + this._inputAreaEl.id)) return;
    // Don't dismiss keyboard if touch started inside the textarea (user is selecting text)
    if (this._touchEl === this._textareaEl || this._touchEl.closest('textarea')) return;
    var dy = e.touches[0].clientY - this._touchY;
    var dx = Math.abs(e.touches[0].clientX - this._touchX);
    if (dy > 10 && dy > dx) { this._dragDismissed = true; this._textareaEl.blur(); }
  }

  _handleTouchEnd(e) {
    if (!this._active || this._dragDismissed || document.activeElement !== this._textareaEl) return;
    var dy = Math.abs(e.changedTouches[0].clientY - this._touchY);
    if (dy < 10 && this._touchEl && this._touchEl.closest('#' + this._scrollEl.id) && !this._touchEl.closest('button') && !this._touchEl.closest('a')) {
      this._textareaEl.blur();
    }
  }

  _doSend() {
    if (this._isBusy() && this._onStop) { this._onStop(); return; }
    if (!this._textareaEl) return;
    var text = this._textareaEl.value.trim();
    if (!text) return;
    this._textareaEl.value = '';
    this._autoResize();
    this.updateSendBtn();
    this._textareaEl.blur();
    if (this._onSend) this._onSend(text);
  }

  _autoResize() {
    if (!this._textareaEl) return;
    this._textareaEl.style.height = 'auto';
    this._textareaEl.style.height = Math.min(this._textareaEl.scrollHeight, window.innerHeight * 0.45) + 'px';
  }
}
// ═══════════════════════════════════════════════════
//  TERMINAL TAB (lazy-initialized)
// ═══════════════════════════════════════════════════

var _termReady = false;
function _termInit() {
  if (_termReady) {
    return;
  }
  _termReady = true;

  var _ts = io('/terminal/ws', { path: '/terminal/ws/socket.io', transports: ['websocket'],
    reconnection: true, reconnectionDelay: 1000, reconnectionDelayMax: 5000 });
  var _termShellInit = false;
  var _shellScroller = null, _shellCurrentResult = null, _shellOutputEl, _shellInputEl;
  function _stripAnsi(str) {
    return str.replace(/\x1b\[[?0-9;]*[a-zA-Z]/g, '').replace(/\[[\?0-9;]*[a-zA-Zlh]/g, '').replace(/\x1b\][^\x07]*\x07/g, '').replace(/\x1b[^[]\S/g, '');
  }
  function _stripPrompt(str) {
    return str.replace(/\[\?2004[hl]/g, '').replace(/^[a-z0-9_-]+@[a-z0-9._-]+:[^\$#\n]*[\$#]\s*/gmi, '');
  }
  var _tconv = document.getElementById('term-conversation');
  var _tpi = document.getElementById('term-prompt-input');
  var _tsb = document.getElementById('term-send-btn');
  var _tlc = document.getElementById('term-log-content');
  var _tSessionStarting = false, _tActCard = null, _tActLog = [];
  var _tDbSid = null, _tCsSid = null, _tBackendAlive = false;
  var _tRendered = 0, _tOldestId = null, _tTotal = 0, _tPS = 50;
  var _tPendingRestart = false;
  var _tModelDisplay = 'Sonnet';
  var _designWidgets = {};
  var _dwRecentColors = [];
  var _dwApplyPending = false;

  // Shell
  function _termInitShell() {
    if (_termShellInit) return;
    _termShellInit = true;

    _shellOutputEl = document.getElementById('term-shell-output');
    _shellInputEl = document.getElementById('term-shell-input');

    _ts.emit('shell_start');

    _shellScroller = new PanelScroller({
      scrollEl: _shellOutputEl,
      panelEl: document.getElementById('term-shell-panel'),
      inputAreaEl: document.getElementById('term-shell-input-area'),
      textareaEl: _shellInputEl,
      sendBtnEl: document.getElementById('term-shell-send-btn'),
      pasteBtnEl: document.getElementById('term-shell-paste-btn'),

      contentWrapEl: document.getElementById('contentWrap'),
      onSend: function(text) { _shellSendCmd(text); },
      onAdjustLayout: _adjustContentTop,
      isBusy: function() { return false; },
    });

    _shellCurrentResult = null;
  }

  function _shellSendCmd(text) {
    var cmdEl = document.createElement('div');
    cmdEl.className = 'msg msg-user';
    cmdEl.textContent = text;
    _shellOutputEl.appendChild(cmdEl);

    _shellCurrentResult = _createResultBlock();
    _shellOutputEl.appendChild(_shellCurrentResult);
    _shellScroller.scrollToBottom(true);

    _ts.emit('input', text + '\n');
  }

  function _createResultBlock() {
    var el = document.createElement('div');
    el.className = 'msg msg-assistant';
    el.style.fontFamily = "'SF Mono', Menlo, Monaco, monospace";
    el.style.whiteSpace = 'pre-wrap';
    var btn = document.createElement('button');
    btn.className = 'copy-btn';
    btn.innerHTML = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="9" y="9" width="13" height="13" rx="2" ry="2"/><path d="M5 15H4a2 2 0 01-2-2V4a2 2 0 012-2h9a2 2 0 012 2v1"/></svg>';
    btn.addEventListener('click', function() {
      navigator.clipboard.writeText(el.childNodes[0].textContent || '').then(function() {
        btn.innerHTML = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="var(--green)" stroke-width="2.5"><path d="M20 6L9 17l-5-5"/></svg>';
        setTimeout(function() {
          btn.innerHTML = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="9" y="9" width="13" height="13" rx="2" ry="2"/><path d="M5 15H4a2 2 0 01-2-2V4a2 2 0 012-2h9a2 2 0 012 2v1"/></svg>';
        }, 1500);
      }).catch(function(){});
    });
    el.appendChild(document.createTextNode(''));
    el.appendChild(btn);
    return el;
  }

  // Socket events — shell
  _ts.on('connect', function() {
    if (_tPendingRestart) {
      _tPendingRestart = false;
      setTimeout(function() { location.reload(); }, 1000);
      return;
    }
    if (_termShellInit) { _ts.emit('shell_start');
      _shellCurrentResult = null; }
    fetch('/terminal/api/session/current?limit=' + _tPS).then(function(r) { return r.json(); }).then(function(data) {
      if (!data.session) { _tSetState('none'); return; }
      _tDbSid = data.session.id; _tCsSid = data.session.claude_session_id;
      var nt = data.total_count || 0;
      if (nt > _tRendered) { _tCollapseAct();
        var skip = Math.max(0, data.messages.length - (nt - _tRendered));
        data.messages.slice(skip).forEach(function(m) { _tRenderMsg(m); });
        _tRendered = nt; _tScrollBot(); }
      if (data.busy) { _tBackendAlive = true; _tSetState('busy'); if (!_tActCard) _tShowAct(); }
      else if (_tCsSid) { _tSetState('ready'); } else { _tSetState('none'); }
    }).catch(function() {});
  });
  _ts.on('output', function(data) {
    var clean = _stripPrompt(_stripAnsi(data));
    if (!clean.trim()) return;
    if (!_shellCurrentResult) {
      _shellCurrentResult = _createResultBlock();
      _shellOutputEl.appendChild(_shellCurrentResult);
    }
    var textNode = _shellCurrentResult.childNodes[0];
    if (textNode) textNode.textContent += clean;
    if (_shellScroller) _shellScroller.scrollToBottom();
  });
  _ts.on('exit', function() {
    var el = document.createElement('div');
    el.className = 'msg msg-user';
    el.style.background = 'var(--yellow)';
    el.style.color = '#000';
    el.textContent = 'Shell exited — restarting...';
    if (_shellOutputEl) _shellOutputEl.appendChild(el);
    if (_shellScroller) _shellScroller.scrollToBottom(true);
    _shellCurrentResult = null;
    setTimeout(function() {
      _ts.emit('shell_start');
      _shellCurrentResult = _createResultBlock();
      if (_shellOutputEl) _shellOutputEl.appendChild(_shellCurrentResult);
    }, 1000);
  });
  _ts.on('disconnect', function() { _tBackendAlive = false; _tSetState('dead'); });

  // Quick actions
  var _tqa = document.getElementById('term-quick-actions');
  document.getElementById('term-qa-toggle').addEventListener('click', function() {
    _tqa.classList.toggle('collapsed'); _tqa.classList.toggle('expanded');
  });
  document.querySelectorAll('.term-qa-btn').forEach(function(btn) {
    btn.addEventListener('click', function() {
      if (btn.dataset.cmd) {
        if (!_termShellInit) devSidebarNav('shell');
        _shellSendCmd(btn.dataset.cmd);
      }
    });
  });
  // Options — model switcher (header dropdown)
  var _modelSelect = document.getElementById('term-claude-hdr-model');
  function _termUpdateModelUI(model) {
    var isSonnet = !model || model === 'sonnet';
    _tModelDisplay = isSonnet ? 'Sonnet' : 'Opus';
    if (_modelSelect) _modelSelect.value = isSonnet ? 'sonnet' : 'opus';
  }
  fetch('/terminal/api/model', {credentials: 'same-origin'}).then(function(r) { return r.json(); }).then(function(d) {
    _termUpdateModelUI(d.model);
  }).catch(function() {});
  if (_modelSelect) _modelSelect.addEventListener('change', async function() {
    var apiModel = _modelSelect.value === 'sonnet' ? null : _modelSelect.value;
    try {
      var r = await fetch('/terminal/api/model', {method: 'POST', credentials: 'same-origin',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({model: apiModel})});
      var d = await r.json();
      _termUpdateModelUI(d.model);
      var label = d.model === 'opus' ? 'Opus' : 'Sonnet';
      // Stop current session and start fresh with new model
      if (_tState === 'busy' || _tState === 'ready') _ts.emit('claude_stop');
      _tCsSid = null; _tBackendAlive = false;
      try { var sr = await fetch('/terminal/api/session/new', {method: 'POST'}); if (sr.ok) { var sd = await sr.json(); if (sd.session) _tDbSid = sd.session.id; } } catch(e) {}
      _tSetState('none');
      var el = document.createElement('div'); el.className = 'session-divider'; el.textContent = '— Switched to ' + label + ' — New Session —'; _tconv.appendChild(el); _tScrollBot();
    } catch(e) {}
  });

  // Markdown renderer
  function _tMd(text) {
    var h = text.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
    h = h.replace(/```(\w*)\n([\s\S]*?)```/g, function(_, l, c) { return '<pre><code>' + c.replace(/\n$/, '') + '</code></pre>'; });
    h = h.replace(/`([^`]+)`/g, '<code>$1</code>');
    h = h.replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>');
    h = h.replace(/\n/g, '<br>');
    h = h.replace(/<pre><code>([\s\S]*?)<\/code><\/pre>/g, function(_, c) { return '<pre><code>' + c.replace(/<br>/g, '\n') + '</code></pre>'; });
    return h;
  }

  // State
  function _tSetState(state) {
    _tState = state;
    var devDot = document.getElementById('devSidebar-claude-dot');
    if (devDot) devDot.className = 'claude-dot cdot-' + state;
    var hdrDot = document.getElementById('term-claude-hdr-dot');
    if (hdrDot) hdrDot.className = 'claude-dot cdot-' + state;
    _claudeScroller.updateSendBtn();
  }
  function _tScrollBot() { _claudeScroller.scrollToBottom(); }

  // Message helpers
  function _tAddUser(text) { var el = document.createElement('div'); el.className = 'msg msg-user'; el.textContent = text; _tconv.appendChild(el); _tScrollBot(); }
  function _dwCheckChanges(ws) {
    var changed = false;
    for (var k in ws.baselineValues) {
      if (ws.currentValues[k] !== ws.baselineValues[k]) { changed = true; break; }
    }
    if (ws.applyBtn) {
      ws.applyBtn.disabled = !changed;
      ws.applyBtn.style.cssText = changed
        ? 'width:100%;padding:10px 0;border-radius:10px;border:1px solid rgba(88,166,255,0.4);background:rgba(88,166,255,0.25);color:#fff;font-size:14px;cursor:pointer;'
        : 'width:100%;padding:10px 0;border-radius:10px;border:1px solid #30363d;background:rgba(48,54,61,0.3);color:#8b949e;font-size:14px;cursor:default;';
    }
  }
  function _dwAddRecent(hex) {
    hex = hex.toLowerCase();
    var idx = _dwRecentColors.indexOf(hex);
    if (idx !== -1) _dwRecentColors.splice(idx, 1);
    _dwRecentColors.unshift(hex);
    if (_dwRecentColors.length > 8) _dwRecentColors.length = 8;
  }
  function _dwHexToRgb(hex) {
    hex = hex.replace('#', '');
    return { r: parseInt(hex.substring(0, 2), 16), g: parseInt(hex.substring(2, 4), 16), b: parseInt(hex.substring(4, 6), 16) };
  }
  var _dwPreviewTimers = {};
  var _dwPreviewSent = false;
  function _dwEmitPreview(prop, value, ws) {
    if (!prop.preview) return;
    var pv = value;
    if (prop.type === 'color') { /* hex string, send directly */ }
    else if (prop.type === 'px') { pv = value + 'px'; }
    else if (prop.type === 'toggle') {
      if (!prop.toggle_values) return;
      var decl = value ? prop.toggle_values.on : prop.toggle_values.off;
      var ci = decl.indexOf(':');
      pv = ci !== -1 ? decl.substring(ci + 1).trim() : decl;
      if (pv.endsWith(';')) pv = pv.slice(0, -1).trim();
    }
    else if (prop.type === 'select') { /* send directly */ }
    else if (prop.type === 'range') { pv = String(value); }
    var key = prop.key;
    if (_dwPreviewTimers[key]) clearTimeout(_dwPreviewTimers[key]);
    _dwPreviewTimers[key] = setTimeout(function() {
      _ts.emit('style_override', { selector: prop.preview.selector, property: prop.preview.css_property, value: pv });
      _dwPreviewSent = true;
      if (ws._resetBtn) ws._resetBtn.style.display = 'inline-block';
    }, 120);
  }
  function _dwClearAllPreviews(ws) {
    var propList = ws.payload.properties || [];
    for (var i = 0; i < propList.length; i++) {
      if (propList[i].preview) {
        _ts.emit('style_override_clear', { selector: propList[i].preview.selector, property: propList[i].preview.css_property });
      }
    }
    _dwPreviewSent = false;
  }
  function _dwBuildApplyPrompt(ws) {
    var changes = [];
    var propList = ws.payload.properties || [];
    for (var i = 0; i < propList.length; i++) {
      var p = propList[i];
      if (ws.currentValues[p.key] === ws.baselineValues[p.key]) continue;
      var oldMatch = p.source.match;
      var newMatch = oldMatch;
      if (p.type === 'color') {
        // Replace old hex with new hex in match string
        var oldHex = ws.baselineValues[p.key];
        var newHex = ws.currentValues[p.key];
        newMatch = newMatch.replace(oldHex, newHex);
      } else if (p.type === 'px') {
        var oldPx = ws.baselineValues[p.key] + 'px';
        var newPx = ws.currentValues[p.key] + 'px';
        newMatch = newMatch.replace(oldPx, newPx);
      } else if (p.type === 'toggle') {
        var tv = p.toggle_values || {};
        var ci = oldMatch.indexOf(':');
        var prop_name = ci !== -1 ? oldMatch.substring(0, ci + 1) : '';
        newMatch = prop_name + ' ' + (ws.currentValues[p.key] ? tv.on : tv.off).split(':').pop().trim();
      } else if (p.type === 'select') {
        newMatch = newMatch.replace(ws.baselineValues[p.key], ws.currentValues[p.key]);
      } else if (p.type === 'range') {
        newMatch = newMatch.replace(String(ws.baselineValues[p.key]), String(ws.currentValues[p.key]));
      }
      changes.push({ file: p.source.file, line: p.source.line, oldMatch: oldMatch, newMatch: newMatch, prop: p });
    }
    // Merge changes on the same line
    var merged = [];
    for (var ci2 = 0; ci2 < changes.length; ci2++) {
      var c = changes[ci2];
      var found = false;
      for (var mi = 0; mi < merged.length; mi++) {
        if (merged[mi].file === c.file && merged[mi].line === c.line) {
          // Apply this change's transformation on top of the already-merged newMatch
          merged[mi].newMatch = merged[mi].newMatch.replace(
            c.oldMatch === merged[mi].oldMatch ? c.oldMatch : ws.baselineValues[c.prop.key],
            c.prop.type === 'color' ? ws.currentValues[c.prop.key] : String(ws.currentValues[c.prop.key])
          );
          found = true; break;
        }
      }
      if (!found) merged.push({ file: c.file, line: c.line, oldMatch: c.oldMatch, newMatch: c.newMatch });
    }
    // Group by file
    var byFile = {};
    for (var fi = 0; fi < merged.length; fi++) {
      var f = merged[fi].file;
      if (!byFile[f]) byFile[f] = [];
      byFile[f].push(merged[fi]);
    }
    var prompt = '[DESIGN_APPLY]\nElement: ' + (ws.payload.element_label || ws.elementId) + '\n';
    var num = 1;
    for (var file in byFile) {
      prompt += 'File: ' + file + '\n';
      var items = byFile[file];
      for (var ii = 0; ii < items.length; ii++) {
        prompt += num + '. Line ' + items[ii].line + ': "' + items[ii].oldMatch + '" → "' + items[ii].newMatch + '"\n';
        num++;
      }
    }
    prompt += '[/DESIGN_APPLY]';
    return prompt;
  }
  function _dwRenderControl(container, prop, ws) {
    container.innerHTML = '';
    var type = prop.type;

    if (type === 'color') {
      // Default state: swatch + hex text
      var wrap = document.createElement('div');
      wrap.style.cssText = 'display:flex;flex-direction:column;width:100%;';
      var defRow = document.createElement('div');
      defRow.style.cssText = 'display:flex;align-items:center;justify-content:flex-end;gap:8px;cursor:pointer;min-height:28px;';
      var swatch = document.createElement('div');
      swatch.style.cssText = 'width:24px;height:24px;border-radius:50%;border:2px solid #30363d;flex-shrink:0;background:' + (ws.currentValues[prop.key] || prop.value) + ';';
      var hexLbl = document.createElement('span');
      hexLbl.style.cssText = 'color:#c9d1d9;font-size:13px;font-family:monospace;';
      hexLbl.textContent = ws.currentValues[prop.key] || prop.value;
      defRow.appendChild(swatch);
      defRow.appendChild(hexLbl);
      wrap.appendChild(defRow);

      var expanded = document.createElement('div');
      expanded.style.cssText = 'display:none;flex-direction:column;gap:8px;margin-top:8px;';

      function selectColor(hex) {
        hex = hex.toLowerCase();
        ws.currentValues[prop.key] = hex;
        swatch.style.background = hex;
        hexLbl.textContent = hex;
        _dwAddRecent(hex);
        expanded.style.display = 'none';
        _dwCheckChanges(ws);
        _dwEmitPreview(prop, hex, ws);
      }

      // Palette row
      var palRow = document.createElement('div');
      palRow.style.cssText = 'display:flex;flex-wrap:wrap;gap:6px;';
      var pal = ws.payload.palette || [];
      for (var pi = 0; pi < pal.length; pi++) {
        (function(c) {
          var ps = document.createElement('div');
          ps.style.cssText = 'width:28px;height:28px;border-radius:6px;border:2px solid #30363d;cursor:pointer;background:' + c + ';';
          ps.addEventListener('click', function(e) { e.stopPropagation(); selectColor(c); });
          palRow.appendChild(ps);
        })(pal[pi]);
      }
      expanded.appendChild(palRow);

      // Recent row (rendered dynamically on expand)
      var recentWrap = document.createElement('div');
      recentWrap.style.cssText = 'display:none;flex-direction:column;gap:4px;';
      var recentLbl = document.createElement('span');
      recentLbl.style.cssText = 'color:#8b949e;font-size:11px;';
      recentLbl.textContent = 'Recent';
      var recentRow = document.createElement('div');
      recentRow.style.cssText = 'display:flex;flex-wrap:wrap;gap:6px;';
      recentWrap.appendChild(recentLbl);
      recentWrap.appendChild(recentRow);
      expanded.appendChild(recentWrap);

      // Custom + hex input row
      var customRow = document.createElement('div');
      customRow.style.cssText = 'display:flex;align-items:center;gap:8px;';
      var customBtn = document.createElement('button');
      customBtn.style.cssText = 'padding:6px 12px;border-radius:6px;border:1px solid #30363d;background:#21262d;color:#c9d1d9;font-size:12px;cursor:pointer;';
      customBtn.textContent = 'Custom';
      var hiddenPicker = document.createElement('input');
      hiddenPicker.type = 'color';
      hiddenPicker.style.cssText = 'position:absolute;opacity:0;pointer-events:none;';
      hiddenPicker.value = ws.currentValues[prop.key] || prop.value;
      customBtn.addEventListener('click', function(e) { e.stopPropagation(); hiddenPicker.click(); });
      hiddenPicker.addEventListener('input', function() { selectColor(hiddenPicker.value); });
      var hexInput = document.createElement('input');
      hexInput.type = 'text';
      hexInput.style.cssText = 'width:80px;padding:4px 8px;border-radius:6px;border:1px solid #30363d;background:#0d1117;color:#c9d1d9;font-size:13px;font-family:monospace;';
      hexInput.value = ws.currentValues[prop.key] || prop.value;
      function applyHex() {
        var v = hexInput.value.trim();
        if (/^#[0-9a-fA-F]{3,8}$/.test(v)) selectColor(v);
      }
      hexInput.addEventListener('blur', applyHex);
      hexInput.addEventListener('keydown', function(e) { if (e.key === 'Enter') { e.preventDefault(); applyHex(); } });
      customRow.appendChild(customBtn);
      customRow.appendChild(hiddenPicker);
      customRow.appendChild(hexInput);
      expanded.appendChild(customRow);

      wrap.appendChild(expanded);

      defRow.addEventListener('click', function() {
        var showing = expanded.style.display === 'flex';
        if (showing) { expanded.style.display = 'none'; return; }
        // Rebuild recent row
        recentRow.innerHTML = '';
        if (_dwRecentColors.length > 0) {
          recentWrap.style.display = 'flex';
          for (var ri = 0; ri < _dwRecentColors.length; ri++) {
            (function(c) {
              var rs = document.createElement('div');
              rs.style.cssText = 'width:28px;height:28px;border-radius:6px;border:2px solid #30363d;cursor:pointer;background:' + c + ';';
              rs.addEventListener('click', function(e) { e.stopPropagation(); selectColor(c); });
              recentRow.appendChild(rs);
            })(_dwRecentColors[ri]);
          }
        } else { recentWrap.style.display = 'none'; }
        hexInput.value = ws.currentValues[prop.key];
        hiddenPicker.value = ws.currentValues[prop.key];
        expanded.style.display = 'flex';
      });

      container.appendChild(wrap);

    } else if (type === 'px') {
      var pxWrap = document.createElement('div');
      pxWrap.style.cssText = 'display:flex;align-items:center;gap:4px;';
      var minV = (prop.min !== undefined && prop.min !== null) ? prop.min : 0;
      var maxV = (prop.max !== undefined && prop.max !== null) ? prop.max : null;

      function clampPx(v) {
        v = parseInt(v, 10);
        if (isNaN(v)) v = parseInt(ws.currentValues[prop.key], 10) || 0;
        if (v < minV) v = minV;
        if (maxV !== null && v > maxV) v = maxV;
        return v;
      }

      var svgMinus = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><line x1="5" y1="12" x2="19" y2="12"/></svg>';
      var svgPlus = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/></svg>';

      var pxInput = document.createElement('input');
      pxInput.type = 'text';
      pxInput.style.cssText = 'width:48px;text-align:center;padding:4px 2px;border-radius:6px;border:1px solid #30363d;background:#0d1117;color:#c9d1d9;font-size:13px;font-family:monospace;';
      pxInput.value = ws.currentValues[prop.key];

      function updatePx(v) {
        v = clampPx(v);
        pxInput.value = v;
        ws.currentValues[prop.key] = v;
        _dwCheckChanges(ws);
        _dwEmitPreview(prop, v, ws);
      }

      function mkStepBtn(delta, muted) {
        var b = document.createElement('button');
        b.style.cssText = 'width:32px;height:32px;border-radius:6px;border:1px solid #30363d;background:#21262d;color:' + (muted ? '#6e7681' : '#c9d1d9') + ';cursor:pointer;display:flex;align-items:center;justify-content:center;padding:0;';
        b.innerHTML = delta < 0 ? svgMinus : svgPlus;
        b.addEventListener('click', function() { updatePx(parseInt(ws.currentValues[prop.key], 10) + delta); });
        return b;
      }

      pxWrap.appendChild(mkStepBtn(-5, true));
      pxWrap.appendChild(mkStepBtn(-1, false));
      pxWrap.appendChild(pxInput);
      pxWrap.appendChild(mkStepBtn(1, false));
      pxWrap.appendChild(mkStepBtn(5, true));

      pxInput.addEventListener('blur', function() { updatePx(pxInput.value); });
      pxInput.addEventListener('keydown', function(e) { if (e.key === 'Enter') { e.preventDefault(); updatePx(pxInput.value); pxInput.blur(); } });

      container.appendChild(pxWrap);

    } else if (type === 'toggle') {
      var tWrap = document.createElement('div');
      tWrap.style.cssText = 'display:flex;align-items:center;cursor:pointer;';
      var track = document.createElement('div');
      var isOn = !!ws.currentValues[prop.key];
      track.style.cssText = 'width:44px;height:24px;border-radius:12px;position:relative;transition:background 0.15s;background:' + (isOn ? '#3fb950' : '#30363d') + ';';
      var thumb = document.createElement('div');
      thumb.style.cssText = 'width:20px;height:20px;border-radius:50%;background:#fff;position:absolute;top:2px;transition:left 0.15s;left:' + (isOn ? '22px' : '2px') + ';';
      track.appendChild(thumb);
      tWrap.appendChild(track);

      tWrap.addEventListener('click', function() {
        var cur = !ws.currentValues[prop.key];
        ws.currentValues[prop.key] = cur;
        track.style.background = cur ? '#3fb950' : '#30363d';
        thumb.style.left = cur ? '22px' : '2px';
        _dwCheckChanges(ws);
        _dwEmitPreview(prop, cur, ws);
      });

      container.appendChild(tWrap);

    } else if (type === 'select') {
      var sel = document.createElement('select');
      sel.style.cssText = 'padding:6px 10px;border-radius:6px;border:1px solid #30363d;background:#0d1117;color:#c9d1d9;font-size:13px;font-family:inherit;-webkit-appearance:none;appearance:none;cursor:pointer;min-width:80px;';
      var opts = prop.options || [];
      for (var oi = 0; oi < opts.length; oi++) {
        var opt = document.createElement('option');
        opt.value = opts[oi];
        opt.textContent = opts[oi];
        opt.style.cssText = 'background:#0d1117;color:#c9d1d9;';
        if (opts[oi] === ws.currentValues[prop.key]) opt.selected = true;
        sel.appendChild(opt);
      }
      sel.addEventListener('change', function() {
        ws.currentValues[prop.key] = sel.value;
        _dwCheckChanges(ws);
        _dwEmitPreview(prop, sel.value, ws);
      });
      container.appendChild(sel);

    } else if (type === 'range') {
      var rWrap = document.createElement('div');
      rWrap.style.cssText = 'display:flex;align-items:center;gap:8px;width:100%;';
      var slider = document.createElement('input');
      slider.type = 'range';
      slider.min = prop.min !== undefined ? prop.min : 0;
      slider.max = prop.max !== undefined ? prop.max : 100;
      slider.step = prop.step !== undefined ? prop.step : 1;
      slider.value = ws.currentValues[prop.key];
      slider.style.cssText = 'flex:1;accent-color:#58a6ff;height:4px;cursor:pointer;';
      var rInput = document.createElement('input');
      rInput.type = 'text';
      rInput.style.cssText = 'width:48px;text-align:center;padding:4px 2px;border-radius:6px;border:1px solid #30363d;background:#0d1117;color:#c9d1d9;font-size:13px;font-family:monospace;';
      rInput.value = ws.currentValues[prop.key];

      slider.addEventListener('input', function() {
        rInput.value = slider.value;
        ws.currentValues[prop.key] = parseFloat(slider.value);
        _dwCheckChanges(ws);
        _dwEmitPreview(prop, parseFloat(slider.value), ws);
      });
      function applyRange() {
        var v = parseFloat(rInput.value);
        if (isNaN(v)) v = ws.currentValues[prop.key];
        v = Math.max(parseFloat(slider.min), Math.min(parseFloat(slider.max), v));
        slider.value = v;
        rInput.value = v;
        ws.currentValues[prop.key] = v;
        _dwCheckChanges(ws);
        _dwEmitPreview(prop, v, ws);
      }
      rInput.addEventListener('blur', applyRange);
      rInput.addEventListener('keydown', function(e) { if (e.key === 'Enter') { e.preventDefault(); applyRange(); rInput.blur(); } });

      rWrap.appendChild(slider);
      rWrap.appendChild(rInput);
      container.appendChild(rWrap);

    } else {
      // Fallback placeholder for unknown types
      var ph = document.createElement('span');
      ph.style.cssText = 'color:#8b949e;font-size:13px;font-family:monospace;';
      ph.textContent = type + ': ' + prop.value;
      container.appendChild(ph);
    }
  }
  function _tRenderDesignWidget(payload) {
    var eid = payload.element_id;
    var old = _tconv.querySelector('.design-widget[data-element-id="' + eid + '"]');
    if (old) old.remove();
    if (_designWidgets[eid]) delete _designWidgets[eid];

    // Build widget with inline styles to bypass any CSS issues
    var w = document.createElement('div');
    w.className = 'design-widget';
    w.setAttribute('data-element-id', eid);
    w.style.cssText = 'align-self:flex-start;max-width:88%;width:100%;background:#161b22;border:1px solid #30363d;border-radius:16px;border-bottom-left-radius:4px;overflow:visible;';

    // Header
    var hdr = document.createElement('div');
    hdr.style.cssText = 'padding:12px 16px;border-bottom:1px solid #30363d;background:rgba(255,255,255,0.02);';
    var title = document.createElement('span');
    title.style.cssText = 'font-size:15px;font-weight:600;color:#c9d1d9;';
    title.textContent = payload.element_label || eid;
    hdr.appendChild(title);
    w.appendChild(hdr);

    // Properties
    var props = document.createElement('div');
    props.style.cssText = 'display:flex;flex-direction:column;';
    var baseline = {}, current = {};
    var propList = payload.properties || [];
    var hasNoPreview = false;

    for (var i = 0; i < propList.length; i++) {
      var p = propList[i];
      var row = document.createElement('div');
      row.style.cssText = 'display:flex;align-items:center;justify-content:space-between;min-height:44px;padding:8px 16px;border-bottom:1px solid rgba(255,255,255,0.04);';
      row.setAttribute('data-key', p.key);

      var lbl = document.createElement('span');
      lbl.style.cssText = 'color:#8b949e;font-size:14px;flex-shrink:0;margin-right:12px;display:flex;align-items:center;gap:5px;';
      lbl.textContent = p.label;
      if (p.preview) {
        var dot = document.createElement('span');
        dot.style.cssText = 'width:5px;height:5px;border-radius:50%;background:#58a6ff;flex-shrink:0;';
        dot.title = 'Live preview';
        lbl.appendChild(dot);
      } else { hasNoPreview = true; }

      var ctrl = document.createElement('div');
      ctrl.style.cssText = 'flex:1;display:flex;justify-content:flex-end;';

      row.appendChild(lbl);
      row.appendChild(ctrl);
      props.appendChild(row);
      baseline[p.key] = p.value;
      current[p.key] = p.value;
    }
    if (hasNoPreview) {
      var noPreviewNote = document.createElement('div');
      noPreviewNote.style.cssText = 'padding:6px 16px 10px;color:#6e7681;font-size:12px;';
      noPreviewNote.textContent = 'Preview not available for some properties';
      props.appendChild(noPreviewNote);
    }
    w.appendChild(props);

    // Actions
    var acts = document.createElement('div');
    acts.style.cssText = 'padding:12px 16px;display:flex;flex-direction:column;align-items:center;gap:8px;';
    var applyBtn = document.createElement('button');
    applyBtn.style.cssText = 'width:100%;padding:10px 0;border-radius:10px;border:1px solid #30363d;background:rgba(48,54,61,0.3);color:#8b949e;font-size:14px;cursor:default;';
    applyBtn.disabled = true;
    applyBtn.textContent = 'Apply';
    acts.appendChild(applyBtn);
    var resetBtn = document.createElement('button');
    resetBtn.style.cssText = 'display:none;padding:4px 12px;border:none;background:transparent;color:#8b949e;font-size:12px;cursor:pointer;';
    resetBtn.textContent = 'Reset Preview';
    acts.appendChild(resetBtn);
    w.appendChild(acts);

    _tconv.appendChild(w);
    var wState = { elementId: eid, payload: payload, baselineValues: baseline, currentValues: current, domElement: w, applyBtn: applyBtn, _resetBtn: resetBtn };
    _designWidgets[eid] = wState;

    // Apply button click handler
    applyBtn.addEventListener('click', function() {
      if (applyBtn.disabled) return;
      var prompt = _dwBuildApplyPrompt(wState);
      applyBtn.disabled = true;
      applyBtn.textContent = 'Applying...';
      applyBtn.style.cssText = 'width:100%;padding:10px 0;border-radius:10px;border:1px solid #30363d;background:rgba(48,54,61,0.3);color:#8b949e;font-size:14px;cursor:default;';
      _dwApplyPending = true;
      // Snapshot old baselines before overwriting
      var oldBL = {};
      for (var bk in wState.baselineValues) oldBL[bk] = wState.baselineValues[bk];
      // Listen for Claude to finish
      var onState = function(d) {
        if (d.state === 'ready') {
          _ts.off('claude_state', onState);
          _dwApplyPending = false;
          applyBtn.textContent = 'Applied';
          applyBtn.style.cssText = 'width:100%;padding:10px 0;border-radius:10px;border:1px solid rgba(63,185,80,0.4);background:rgba(63,185,80,0.15);color:#3fb950;font-size:14px;cursor:default;';
          // Update baselines to match applied values
          for (var k in wState.currentValues) wState.baselineValues[k] = wState.currentValues[k];
          // Update payload property values and source.match for subsequent applies
          var pl = wState.payload.properties || [];
          for (var pi = 0; pi < pl.length; pi++) {
            var pp = pl[pi];
            if (oldBL[pp.key] !== wState.currentValues[pp.key]) {
              var ov = oldBL[pp.key], nv = wState.currentValues[pp.key];
              pp.value = nv;
              if (pp.type === 'color') {
                pp.source.match = pp.source.match.replace(ov, nv);
              } else if (pp.type === 'px' || pp.type === 'range') {
                pp.source.match = pp.source.match.replace(String(ov) + 'px', String(nv) + 'px');
              } else {
                pp.source.match = pp.source.match.replace(String(ov), String(nv));
              }
            }
          }
          // Keep previews active — no reload, so preview is the live visual
          if (resetBtn) resetBtn.style.display = 'none';
          setTimeout(function() {
            applyBtn.textContent = 'Apply';
            _dwCheckChanges(wState);
          }, 2000);
        }
      };
      _ts.on('claude_state', onState);
      // Timeout safety — stop listening after 60s
      setTimeout(function() { _ts.off('claude_state', onState); _dwApplyPending = false; }, 60000);
      _ts.emit('claude_prompt', { text: prompt, enhance: false });
    });

    // Reset Preview button
    resetBtn.addEventListener('click', function() {
      _dwClearAllPreviews(wState);
      // Reset controls to baseline
      for (var k in wState.baselineValues) wState.currentValues[k] = wState.baselineValues[k];
      _dwCheckChanges(wState);
      resetBtn.style.display = 'none';
      // Re-render controls with baseline values
      var ctrlRows = props.querySelectorAll('div[data-key]');
      for (var ri = 0; ri < propList.length; ri++) {
        var cd = ctrlRows[ri].querySelector('div');
        _dwRenderControl(cd, propList[ri], wState);
      }
    });

    // Render controls after state is set up
    var ctrlDivs = props.querySelectorAll('div[data-key]');
    for (var j = 0; j < propList.length; j++) {
      var ctrlDiv = ctrlDivs[j].querySelector('div');
      _dwRenderControl(ctrlDiv, propList[j], wState);
    }
    _tScrollBot();
  }
  function _tAddAsst(text, raw, activity) {
    // Check for router widget payload
    var wStart = '__WIDGET__', wEnd = '__/WIDGET__';
    var wi = (raw || text).indexOf(wStart);
    if (wi !== -1) {
      var we = (raw || text).indexOf(wEnd, wi);
      if (we !== -1) {
        var wp = JSON.parse((raw || text).substring(wi + wStart.length, we).trim());
        _renderWidget(wp);
        return;
      }
    }
    // Check for design widget payload
    var dwStart = '<!--DESIGN_WIDGET-->', dwEnd = '<!--/DESIGN_WIDGET-->';
    var si = (raw || text).indexOf(dwStart);
    if (si !== -1) {
      var ei = (raw || text).indexOf(dwEnd, si);
      if (ei !== -1) {
        var jsonStr = (raw || text).substring(si + dwStart.length, ei).trim();
        try {
          var payload = JSON.parse(jsonStr);
          console.log('[DW] parsed payload, element_id:', payload.element_id, 'props:', payload.properties ? payload.properties.length : 'NONE');
          _tRenderDesignWidget(payload);
          return;
        } catch(e) { console.error('[DW] widget error:', e); /* fall through to normal render */ }
      }
    }
    var el = document.createElement('div'); el.className = 'msg msg-assistant'; el.innerHTML = _tMd(text);
    var btn = document.createElement('button'); btn.className = 'copy-btn';
    btn.innerHTML = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="9" y="9" width="13" height="13" rx="2" ry="2"/><path d="M5 15H4a2 2 0 01-2-2V4a2 2 0 012-2h9a2 2 0 012 2v1"/></svg>';
    btn.addEventListener('click', function() { navigator.clipboard.writeText(raw || text).then(function() {
      btn.innerHTML = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="var(--green)" stroke-width="2.5"><path d="M20 6L9 17l-5-5"/></svg>';
      btn.classList.add('copied'); setTimeout(function() { btn.innerHTML = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="9" y="9" width="13" height="13" rx="2" ry="2"/><path d="M5 15H4a2 2 0 01-2-2V4a2 2 0 012-2h9a2 2 0 012 2v1"/></svg>'; btn.classList.remove('copied'); }, 1500);
    }).catch(function(){}); });
    el.appendChild(btn); _tconv.appendChild(el);
    if (activity && activity.length > 0) {
      var toggle = document.createElement('div'); toggle.className = 'activity-collapsed';
      toggle.innerHTML = '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="vertical-align:middle;margin-right:4px;"><polyline points="9 18 15 12 9 6"/></svg>View activity (' + activity.length + ' steps)';
      var logEl = document.createElement('div'); logEl.className = 'activity-log'; logEl.textContent = activity.join('\n');
      toggle.addEventListener('click', function() { var o = logEl.style.display === 'block'; logEl.style.display = o ? 'none' : 'block'; });
      _tconv.appendChild(toggle); _tconv.appendChild(logEl);
    }
    _tScrollBot();
  }
  function _tAddErr(text, showRestart) {
    var el = document.createElement('div'); el.className = 'msg-error'; el.textContent = text;
    if (showRestart) { var b = document.createElement('button'); b.textContent = 'Restart Session';
      b.addEventListener('click', function() { _tStartSess(); }); el.appendChild(document.createElement('br')); el.appendChild(b); }
    _tconv.appendChild(el); _tScrollBot();
  }
  function _tShowAct() { _tActLog = []; _tActCard = document.createElement('div'); _tActCard.className = 'activity-card';
    _tActCard.innerHTML = '<span class="spinner"></span><span class="activity-text">Thinking...</span>';
    _tconv.appendChild(_tActCard); _tScrollBot(); }
  function _tUpdateAct(text) { if (!_tActCard) return; _tActLog.push(text);
    var t = _tActCard.querySelector('.activity-text'); if (t) t.textContent = text; _tScrollBot(); }
  function _tCollapseAct() { if (!_tActCard) return; var card = _tActCard; _tActCard = null; card.remove();
    if (_tActLog.length > 0) {
      var toggle = document.createElement('div'); toggle.className = 'activity-collapsed';
      toggle.innerHTML = '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="vertical-align:middle;margin-right:4px;"><polyline points="9 18 15 12 9 6"/></svg>View activity (' + _tActLog.length + ' steps)';
      var logEl = document.createElement('div'); logEl.className = 'activity-log'; logEl.textContent = _tActLog.join('\n');
      toggle.addEventListener('click', function() { var o = logEl.style.display === 'block'; logEl.style.display = o ? 'none' : 'block'; });
      _tconv.appendChild(toggle); _tconv.appendChild(logEl);
    } _tScrollBot(); }

  // Session management
  function _tStartSess(thenSend, enhance) {
    _tSessionStarting = true; _tBackendAlive = true;
    _ts.emit('claude_start', {claude_session_id: _tCsSid, db_session_id: _tDbSid});
    if (thenSend) {
      var h = function(d) { if (d.state === 'ready') { _ts.off('claude_state', h); _tSessionStarting = false; _tSendPrompt(thenSend, enhance); } };
      _ts.on('claude_state', h);
      setTimeout(function() { _ts.off('claude_state', h); _tSessionStarting = false; }, 15000);
    }
  }
  function _tSendPrompt(text, enhance) { _tAddUser(text); _tRendered++; _tShowAct(); _ts.emit('claude_prompt', {text: text, enhance: !!enhance}); }
  // Send logic is now handled by _claudeScroller.onSend callback


  // Paste button — handled by _claudeScroller

  // ── Widget rendering ──
  function _renderWidget(widget) {
    var renderers = {
      'git_status': _renderGitStatus,
      'git_push_result': _renderGitPushResult,
    };
    var fn = renderers[widget.type];
    if (fn) {
      try {
        fn(widget.data);
      } catch(e) {
        var errDiv = document.createElement('div');
        errDiv.style.cssText = 'padding:12px;margin:8px;background:#1a1a2e;border:2px solid #f85149;border-radius:8px;color:#f85149;font-size:13px;font-family:monospace;white-space:pre-wrap;';
        errDiv.textContent = 'Widget error: ' + e.message + '\n' + e.stack;
        _tconv.appendChild(errDiv);
        _tScrollBot();
      }
    } else {
      _tAddAsst(JSON.stringify(widget.data, null, 2));
    }
  }

  function _renderGitStatus(data) {
    var mono = "'SF Mono',Menlo,Monaco,monospace";
    var card = document.createElement('div');
    card.style.cssText = 'align-self:flex-start;max-width:88%;width:100%;margin:4px 0;background:#161b22;border:1px solid #30363d;border-radius:12px;border-bottom-left-radius:4px;overflow:visible;';
    card.setAttribute('data-widget', 'git_status');

    // Header
    var header = document.createElement('div');
    header.style.cssText = 'display:flex;justify-content:space-between;align-items:center;padding:12px 14px;border-bottom:1px solid #30363d;';
    var title = document.createElement('span');
    title.style.cssText = 'font-size:14px;font-weight:600;color:#c9d1d9;';
    title.textContent = 'Git Status';
    header.appendChild(title);
    var branchEl = document.createElement('a');
    branchEl.style.cssText = 'font-size:12px;color:#58a6ff;text-decoration:none;';
    branchEl.textContent = data.branch || 'main';
    branchEl.href = 'https://github.com/dev-bbrooks/15-min-btc-bot';
    branchEl.target = '_blank';
    header.appendChild(branchEl);
    card.appendChild(header);

    if (data.clean) {
      var cleanMsg = document.createElement('div');
      cleanMsg.style.cssText = 'padding:16px 14px;color:#3fb950;font-size:13px;display:flex;align-items:center;gap:8px;';
      cleanMsg.innerHTML = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="#3fb950" stroke-width="2.5"><path d="M20 6L9 17l-5-5"/></svg> Working tree clean';
      card.appendChild(cleanMsg);
    } else {
      // Files section
      if (data.uncommitted && data.uncommitted.length > 0) {
        var section = document.createElement('div');
        section.style.cssText = 'padding:10px 14px;';
        var sLabel = document.createElement('div');
        sLabel.style.cssText = 'font-size:11px;font-weight:600;text-transform:uppercase;letter-spacing:0.3px;color:#8b949e;margin-bottom:8px;';
        sLabel.textContent = 'Uncommitted Changes (' + data.uncommitted.length + ')';
        section.appendChild(sLabel);

        var statusColors = { M: '#d29922', A: '#3fb950', D: '#f85149' };
        for (var i = 0; i < data.uncommitted.length; i++) {
          var f = data.uncommitted[i];
          var row = document.createElement('div');
          row.style.cssText = 'display:flex;align-items:center;gap:8px;padding:4px 0;';
          var badge = document.createElement('span');
          badge.style.cssText = 'font-size:11px;font-weight:700;width:18px;text-align:center;flex-shrink:0;color:' + (statusColors[f.status] || '#8b949e') + ';';
          badge.textContent = f.status === '??' ? 'U' : f.status;
          row.appendChild(badge);
          var fname = document.createElement('span');
          fname.style.cssText = 'font-size:12px;color:#c9d1d9;';
          fname.textContent = f.file;
          row.appendChild(fname);
          section.appendChild(row);
        }
        card.appendChild(section);
      }

      // Unpushed commits
      if (data.unpushed && data.unpushed.length > 0) {
        var uSec = document.createElement('div');
        uSec.style.cssText = 'padding:10px 14px;border-top:1px solid #30363d;';
        var uLbl = document.createElement('div');
        uLbl.style.cssText = 'font-size:11px;font-weight:600;text-transform:uppercase;letter-spacing:0.3px;color:#8b949e;margin-bottom:8px;';
        uLbl.textContent = 'Unpushed Commits (' + data.unpushed.length + ')';
        uSec.appendChild(uLbl);
        for (var j = 0; j < data.unpushed.length; j++) {
          var cm = data.unpushed[j];
          var cRow = document.createElement('div');
          cRow.style.cssText = 'display:flex;align-items:baseline;gap:8px;padding:3px 0;';
          var h = document.createElement('span');
          h.style.cssText = 'font-size:11px;color:#58a6ff;';
          h.textContent = cm.hash;
          cRow.appendChild(h);
          var m = document.createElement('span');
          m.style.cssText = 'font-size:12px;color:#c9d1d9;';
          m.textContent = cm.message;
          cRow.appendChild(m);
          uSec.appendChild(cRow);
        }
        card.appendChild(uSec);
      }

      // Push button
      var footer = document.createElement('div');
      footer.style.cssText = 'padding:10px 14px;border-top:1px solid #30363d;';
      var pushBtn = document.createElement('button');
      pushBtn.style.cssText = 'width:100%;padding:10px;border-radius:8px;border:1px solid rgba(88,166,255,0.4);background:rgba(88,166,255,0.12);color:#58a6ff;font-size:14px;font-weight:600;font-family:inherit;cursor:pointer;';
      pushBtn.textContent = 'Push All';
      pushBtn.addEventListener('click', function() {
        pushBtn.disabled = true;
        pushBtn.style.opacity = '0.5';
        pushBtn.textContent = 'Pushing...';
        _ts.emit('direct_action', { action: 'git_push' });
      });
      footer.appendChild(pushBtn);
      card.appendChild(footer);
    }

    _tconv.appendChild(card);
    _tScrollBot();
  }

  function _renderGitPushResult(data) {
    // Find the existing git status card and update it in-place
    var existing = _tconv.querySelector('[data-widget="git_status"]');
    if (!existing) {
      // Fallback: render as standalone message
      var msg = data.success ? ('Pushed: ' + (data.commit_hash || '') + ' ' + (data.commit_message || '')) : ('Push failed: ' + (data.message || ''));
      _tAddAsst(msg);
      return;
    }

    // Remove everything after the header (file list, unpushed commits, push button)
    var header = existing.children[0];
    while (existing.children.length > 1) existing.removeChild(existing.lastChild);

    // Update header title
    var titleEl = header.querySelector('span');
    if (titleEl) titleEl.textContent = data.success ? 'Pushed to GitHub' : 'Push Failed';

    // Build result body
    var body = document.createElement('div');
    body.style.cssText = 'padding:14px;font-size:13px;display:flex;align-items:flex-start;gap:8px;line-height:1.5;';

    if (data.already_clean) {
      body.innerHTML = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="#3fb950" stroke-width="2.5"><path d="M20 6L9 17l-5-5"/></svg> Nothing to push — already clean.';
      body.style.color = '#3fb950';
    } else if (data.success) {
      var info = '';
      if (data.commit_hash) info += '<span style="color:#58a6ff">' + data.commit_hash + '</span> — ' + (data.commit_message || '');
      if (data.files && data.files.length > 0) info += (info ? '<br>' : '') + data.files.length + ' file' + (data.files.length > 1 ? 's' : '') + ' pushed';
      body.innerHTML = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="#3fb950" stroke-width="2.5"><path d="M20 6L9 17l-5-5"/></svg> ' + info;
      body.style.color = '#3fb950';
    } else {
      body.innerHTML = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="#f85149" stroke-width="2.5"><circle cx="12" cy="12" r="10"/><line x1="15" y1="9" x2="9" y2="15"/><line x1="9" y1="9" x2="15" y2="15"/></svg> ' + (data.message || 'Push failed.');
      body.style.color = '#f85149';
    }

    existing.appendChild(body);
    _tScrollBot();
  }

  // Claude WebSocket events
  _ts.on('claude_state', function(d) {
    if (d.state === 'ready') { if (_tState === 'busy') _tCollapseAct(); _tSetState('ready'); }
    else if (d.state === 'busy') _tSetState('busy');
    else if (d.state === 'dead') { _tCollapseAct(); _tSetState('dead'); }
  });
  _ts.on('claude_status', function(d) {
    if (d.type === 'restart') { var el = document.createElement('div'); el.className = 'msg-system'; el.textContent = d.text;
      _tconv.appendChild(el); _tScrollBot(); _tRendered++;
      if (d.text.indexOf('platform-terminal') !== -1) { _tPendingRestart = true; }
      if (d.text.indexOf('platform-dashboard') !== -1 && !_dwApplyPending) {
        setTimeout(function() { location.reload(); }, 3000);
      }
    }
    _tUpdateAct(d.text);
  });
  _ts.on('claude_response', function(d) {
    _tCollapseAct(); if (d.id) _tCsSid = d.id;
    console.log('[CR] text len=' + (d.text||'').length + ' first80=' + (d.text||'').substring(0,80));
    _tAddAsst(d.text, d.text); _tRendered++;
  });
  _ts.on('claude_error', function(d) { _tCollapseAct(); _tAddErr(d.text + (d.detail ? ': ' + d.detail : ''), true); _tRendered++;
    if (_tState === 'busy') _tSetState('dead'); });
  _ts.on('claude_raw', function(d) { _tlc.textContent += d.data; _tlc.scrollTop = _tlc.scrollHeight; });
  _ts.on('style_override', function(data) {
    try {
      var el = document.querySelector(data.selector);
      if (el) el.style.setProperty(data.property, data.value, 'important');
    } catch(e) {}
  });
  _ts.on('style_override_clear', function(data) {
    try {
      var el = document.querySelector(data.selector);
      if (el && data.property) {
        el.style.removeProperty(data.property);
      } else if (el) {
        el.removeAttribute('style');
      }
    } catch(e) {}
  });

  // Log clear
  document.getElementById('term-log-clear-btn').addEventListener('click', function() { _tlc.textContent = ''; });

  // Render DB message (append or prepend)
  function _tRenderMsg(msg, prepend) {
    if (msg.role === 'user') {
      if (prepend) { var el = document.createElement('div'); el.className = 'msg msg-user'; el.textContent = msg.content; _tconv.insertBefore(el, _tconv.firstChild); }
      else _tAddUser(msg.content);
    } else if (msg.role === 'assistant') {
      var act = []; try { act = JSON.parse(msg.activity_log || '[]'); } catch(e) {}
      if (prepend) { var el = document.createElement('div'); el.className = 'msg msg-assistant'; el.innerHTML = _tMd(msg.content); _tconv.insertBefore(el, _tconv.firstChild); }
      else _tAddAsst(msg.content, msg.content, act);
    } else if (msg.role === 'error') {
      if (prepend) { var el = document.createElement('div'); el.className = 'msg-error'; el.textContent = msg.content; _tconv.insertBefore(el, _tconv.firstChild); }
      else _tAddErr(msg.content, false);
    } else if (msg.role === 'system') {
      var el = document.createElement('div'); el.className = 'msg-system'; el.textContent = msg.content;
      if (prepend) _tconv.insertBefore(el, _tconv.firstChild); else _tconv.appendChild(el);
    }
  }

  // Load session
  async function _tLoadSession() {
    try {
      var r = await fetch('/terminal/api/session/current?limit=' + _tPS);
      var data = await r.json();
      if (data.session) {
        _tDbSid = data.session.id; _tCsSid = data.session.claude_session_id;
        _tTotal = data.total_count || 0;
        data.messages.forEach(function(m) { _tRenderMsg(m); });
        _tRendered = _tTotal;
        if (data.messages.length > 0) _tOldestId = data.messages[0].id;
        if (data.busy) { _tBackendAlive = true; _tSetState('busy'); _tShowAct(); }
        else if (_tCsSid) _tSetState('ready');
        _tconv.scrollTop = _tconv.scrollHeight;
        if (_tOldestId) _tLoadOlder();
      }
    } catch(e) { console.error('Failed to load terminal session:', e); }
  }

  // Infinite scroll — called by PanelScroller, returns {done: bool}
  async function _tLoadOlder() {
    if (!_tOldestId || !_tDbSid) return {done: true};
    try {
      var r = await fetch('/terminal/api/session/current?limit=' + _tPS + '&before_id=' + _tOldestId);
      var data = await r.json();
      if (!data.messages || !data.messages.length) { _tOldestId = null; return {done: true}; }
      var frag = document.createDocumentFragment();
      for (var i = 0; i < data.messages.length; i++) {
        var m = data.messages[i], el;
        if (m.role === 'user') { el = document.createElement('div'); el.className = 'msg msg-user'; el.textContent = m.content; }
        else if (m.role === 'assistant') { el = document.createElement('div'); el.className = 'msg msg-assistant'; el.innerHTML = _tMd(m.content); }
        else if (m.role === 'error') { el = document.createElement('div'); el.className = 'msg-error'; el.textContent = m.content; }
        else if (m.role === 'system') { el = document.createElement('div'); el.className = 'msg-system'; el.textContent = m.content; }
        if (el) frag.appendChild(el);
      }
      _tOldestId = data.messages[0].id;
      _claudeScroller.prependContent(function() { _tconv.insertBefore(frag, _tconv.firstChild); });
    } catch(e) {}
    return {done: false};
  }

  // PanelScroller instance for Claude chat
  var _claudeScroller = new PanelScroller({
    scrollEl: _tconv,
    panelEl: document.getElementById('term-claude-panel'),
    inputAreaEl: document.getElementById('term-input-area'),
    textareaEl: _tpi,
    sendBtnEl: _tsb,
    enhanceSendBtnEl: document.getElementById('term-enhance-send-btn'),
    pasteBtnEl: document.getElementById('term-claude-paste-btn'),
    headerEl: document.getElementById('term-claude-header'),
    contentWrapEl: document.getElementById('contentWrap'),
    onSend: function(text) {
      if (_tState === 'none' || _tState === 'dead' || !_tBackendAlive) _tStartSess(text);
      else _tSendPrompt(text, false);
    },
    onEnhanceSend: function(text) {
      if (_tState === 'none' || _tState === 'dead' || !_tBackendAlive) _tStartSess(text, true);
      else _tSendPrompt(text, true);
    },
    onStop: function() { _ts.emit('claude_stop'); },
    onLoadOlder: function() { return _tLoadOlder(); },
    onAdjustLayout: _adjustContentTop,
    isBusy: function() { return _tState === 'busy'; },
  });
  _claudeScroller.setActive(true);

  // Visibility handler
  document.addEventListener('visibilitychange', function() {
    if (!_termReady || document.visibilityState !== 'visible') return;
    if (!_ts.connected) { _ts.connect(); } else {
      fetch('/terminal/api/session/current?limit=' + _tPS).then(function(r) { return r.json(); }).then(function(data) {
        if (!data.session) { _tSetState('none'); return; }
        _tDbSid = data.session.id; _tCsSid = data.session.claude_session_id;
        var nt = data.total_count || 0;
        if (nt > _tRendered) { _tCollapseAct();
          var skip = Math.max(0, data.messages.length - (nt - _tRendered));
          data.messages.slice(skip).forEach(function(m) { _tRenderMsg(m); });
          _tRendered = nt; _tScrollBot(); }
        if (data.busy) { _tBackendAlive = true; _tSetState('busy'); if (!_tActCard) _tShowAct(); }
        else if (_tCsSid) _tSetState('ready'); else _tSetState('none');
      }).catch(function() {});
    }
  });

  // Expose scroller activation for devSidebarNav
  window._devNavActivate = function(panel) {
    if (panel === 'shell') {
      if (!_termShellInit) _termInitShell();
      if (_shellScroller) _shellScroller.setActive(true);
      _claudeScroller.setActive(false);
    } else if (panel === 'claude') {
      _claudeScroller.setActive(true);
      if (_shellScroller) _shellScroller.setActive(false);
      _tScrollBot();
    } else {
      _claudeScroller.setActive(false);
      if (_shellScroller) _shellScroller.setActive(false);
    }
  };

  // New session button
  var _newSessBtn = document.getElementById('term-claude-new-btn');
  if (_newSessBtn) {
    _newSessBtn.addEventListener('click', async function() {
      if (_tState === 'busy' || _tState === 'ready') _ts.emit('claude_stop');
      _tCsSid = null; _tBackendAlive = false;
      try { var r = await fetch('/terminal/api/session/new', {method: 'POST'}); if (r.ok) { var d = await r.json(); if (d.session) _tDbSid = d.session.id; } } catch(e) {}
      _tSetState('none');
      var el = document.createElement('div'); el.className = 'session-divider'; el.textContent = '— New Session —'; _tconv.appendChild(el); _tScrollBot();
    });
  }

  // Init
  _tLoadSession();
  _claudeScroller.updateSendBtn();
}
})();
