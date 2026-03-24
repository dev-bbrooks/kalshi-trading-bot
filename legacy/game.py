"""
game.py — Arcade tab content. Idle zone gathering game.

Exports:
- GAME_HTML: injected into the Arcade page body
- GAME_JS: injected into the main dashboard script block
"""

GAME_HTML = r"""
<style>
  .mg-rarity {
    display:inline-block;
    padding:2px 7px;
    border-radius:4px;
    font-size:10px;
    font-weight:700;
    letter-spacing:.3px;
    line-height:1.2;
  }
  .mg-r-common    { color:#ccc;      background:rgba(255,255,255,.08);  border:1px solid rgba(255,255,255,.2); }
  .mg-r-uncommon  { color:#7dff95;   background:rgba(125,255,149,.08);  border:1px solid rgba(125,255,149,.25); }
  .mg-r-rare      { color:#72b8ff;   background:rgba(114,184,255,.08);  border:1px solid rgba(114,184,255,.25); }
  .mg-r-epic      { color:#cf8cff;   background:rgba(207,140,255,.08);  border:1px solid rgba(207,140,255,.25); }
  .mg-r-exotic    { color:#ffe36b;   background:rgba(255,227,107,.08);  border:1px solid rgba(255,227,107,.25); }
  .mg-r-legendary { color:#ffac5e;   background:rgba(255,172,94,.08);   border:1px solid rgba(255,172,94,.25); }

  .mg-grid {
    display:grid;
    grid-template-columns:repeat(auto-fill, minmax(132px, 1fr));
    gap:10px;
    max-height:52vh;
    overflow-y:auto;
    padding:2px;
  }
  .mg-slot {
    border:1px solid var(--border);
    background:var(--bg);
    border-radius:10px;
    padding:10px;
    min-height:132px;
    display:flex;
    flex-direction:column;
    gap:8px;
  }
  .mg-slot-top {
    display:flex;
    justify-content:space-between;
    align-items:center;
    gap:8px;
  }
  .mg-slot-title { font-size:12px; font-weight:700; }
  .mg-slot-small { font-size:10px; }
  .mg-slot-bar {
    height:10px;
    border-radius:999px;
    background:rgba(255,255,255,.06);
    border:1px solid var(--border);
    overflow:hidden;
  }
  .mg-slot-fill {
    height:100%;
    width:0%;
    background:var(--orange);
    transition:width .12s linear;
  }
  .mg-slot-actions {
    display:flex;
    flex-direction:column;
    gap:6px;
    margin-top:auto;
  }
  .mg-slot-drop {
    display:flex;
    flex-direction:column;
    gap:6px;
    margin-top:4px;
  }
  .mg-slot-choice {
    width:100%;
    text-align:left;
    padding:7px 10px;
    border-radius:8px;
    border:1px solid var(--border);
    background:var(--card);
    color:var(--text);
    font-size:11px;
    cursor:pointer;
  }
</style>

<div style="display:flex;gap:8px;align-items:center;justify-content:space-between;margin-bottom:10px">
  <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">
    <button class="btn btn-blue" style="flex:0;white-space:nowrap;padding:8px 16px;font-size:12px" onclick="mgShowInv()">Inventory</button>
    <button id="mgFurnaceBtn" class="btn btn-dim" style="flex:0;white-space:nowrap;padding:8px 16px;font-size:12px;width:auto" onclick="mgFurnaceClick()">Furnace 🔒 1000 Stone</button>
    <button id="mgCauldronBtn" class="btn btn-dim" style="flex:0;white-space:nowrap;padding:8px 16px;font-size:12px;width:auto" onclick="mgCauldronClick()">Cauldron 🔒 1000 Iron</button>
  </div>
</div>

<div id="mgBanner" style="padding:10px 12px;border-radius:8px;border:1px solid var(--border);background:color-mix(in srgb, var(--blue) 8%, var(--card));font-size:13px;margin-bottom:8px">
  Active Zone: Village
</div>

<div id="mgBoostWrap" style="display:flex;align-items:center;gap:8px;min-height:24px;margin-bottom:10px">
  <span id="mgBoostChip" class="chip active-orange" style="display:none">SUGAR RUSH ×10</span>
  <span id="mgBoostTimer" class="dim" style="font-size:11px">Sugar Rush inactive</span>
</div>

<div id="mgPills" style="display:flex;gap:6px;flex-wrap:wrap;min-height:12px;margin-bottom:10px"></div>

<div class="card" style="border-left:3px solid var(--green)">
  <div style="display:flex;justify-content:space-between;align-items:center">
    <div>
      <div style="font-size:16px;font-weight:700">Village</div>
      <div class="dim" style="font-size:11px">Meet villagers and receive gifts</div>
    </div>
    <button class="btn btn-dim" id="mgZ-village" style="padding:7px 16px;font-size:12px;width:auto" onclick="mgEnter('village')">Enter</button>
  </div>
  <div style="height:14px;border-radius:99px;background:var(--bg);border:1px solid var(--border);overflow:hidden;margin-top:10px">
    <div id="mgP-village" style="height:100%;width:0%;background:var(--blue);transition:width .12s linear"></div>
  </div>
  <div id="mgT-village" class="dim" style="font-size:11px;margin-top:4px">Inactive</div>
  <div id="mgItem-village" style="display:flex;justify-content:space-between;align-items:center;margin-top:6px"></div>
</div>

<div class="card" style="border-left:3px solid var(--dim)">
  <div style="display:flex;justify-content:space-between;align-items:center">
    <div>
      <div style="font-size:16px;font-weight:700">Forest</div>
      <div class="dim" style="font-size:11px">Gather basic natural materials</div>
    </div>
    <button class="btn btn-dim" id="mgZ-forest" style="padding:7px 16px;font-size:12px;width:auto" onclick="mgEnter('forest')">Enter</button>
  </div>
  <div style="height:14px;border-radius:99px;background:var(--bg);border:1px solid var(--border);overflow:hidden;margin-top:10px">
    <div id="mgP-forest" style="height:100%;width:0%;background:var(--blue);transition:width .12s linear"></div>
  </div>
  <div id="mgT-forest" class="dim" style="font-size:11px;margin-top:4px">Inactive</div>
  <div id="mgItem-forest" style="display:flex;justify-content:space-between;align-items:center;margin-top:6px"></div>
</div>

<div class="card" style="border-left:3px solid var(--dim)">
  <div style="display:flex;justify-content:space-between;align-items:center">
    <div>
      <div style="font-size:16px;font-weight:700">Cave</div>
      <div class="dim" style="font-size:11px">Mine underground resources</div>
    </div>
    <button class="btn btn-dim" id="mgZ-cave" style="padding:7px 16px;font-size:12px;width:auto" onclick="mgEnter('cave')">Enter</button>
  </div>
  <div style="height:14px;border-radius:99px;background:var(--bg);border:1px solid var(--border);overflow:hidden;margin-top:10px">
    <div id="mgP-cave" style="height:100%;width:0%;background:var(--blue);transition:width .12s linear"></div>
  </div>
  <div id="mgT-cave" class="dim" style="font-size:11px;margin-top:4px">Inactive</div>
  <div id="mgItem-cave" style="display:flex;justify-content:space-between;align-items:center;margin-top:6px"></div>
</div>

<div class="card" style="border-left:3px solid var(--orange)">
  <div style="display:flex;justify-content:space-between;align-items:center">
    <div>
      <div style="font-size:16px;font-weight:700">Outerworld</div>
      <div class="dim" style="font-size:11px">A hostile realm with impossible treasures</div>
    </div>
    <button class="btn btn-dim" id="mgZ-outerworld" style="padding:7px 16px;font-size:12px;width:auto" onclick="mgEnter('outerworld')">Enter</button>
  </div>
  <div style="height:14px;border-radius:99px;background:var(--bg);border:1px solid var(--border);overflow:hidden;margin-top:10px">
    <div id="mgP-outerworld" style="height:100%;width:0%;background:var(--blue);transition:width .12s linear"></div>
  </div>
  <div id="mgT-outerworld" class="dim" style="font-size:11px;margin-top:4px">Inactive</div>
  <div id="mgItem-outerworld" style="display:flex;justify-content:space-between;align-items:center;margin-top:6px"></div>
</div>

<div class="dim" style="font-size:11px;margin-top:4px;margin-bottom:20px">
  Only one zone active at a time. Gathering continues while away.
</div>

<div class="confirm-overlay" id="mgInvOverlay" style="display:none">
  <div class="modal-panel" style="max-width:380px">
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:12px">
      <strong style="font-size:15px">Inventory</strong>
      <button class="btn btn-dim" style="width:auto;padding:6px 14px;font-size:11px" onclick="closeModal('mgInvOverlay')">Close</button>
    </div>
    <div id="mgInvBody" class="dim">No items yet.</div>
  </div>
</div>

<div class="confirm-overlay" id="mgFurnaceOverlay" style="display:none">
  <div class="modal-panel" style="max-width:760px">
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:12px">
      <div>
        <strong style="font-size:15px">Furnace</strong>
        <div class="dim" style="font-size:11px;margin-top:2px">100 items per batch · 3s per item</div>
      </div>
      <button class="btn btn-dim" style="width:auto;padding:6px 14px;font-size:11px" onclick="closeModal('mgFurnaceOverlay')">Close</button>
    </div>
    <div id="mgFurnaceBody"></div>
  </div>
</div>

<div class="confirm-overlay" id="mgCauldronOverlay" style="display:none">
  <div class="modal-panel" style="max-width:760px">
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:12px">
      <div>
        <strong style="font-size:15px">Cauldron</strong>
        <div class="dim" style="font-size:11px;margin-top:2px">100 items per batch · 10s per item · reward at batch end</div>
      </div>
      <button class="btn btn-dim" style="width:auto;padding:6px 14px;font-size:11px" onclick="closeModal('mgCauldronOverlay')">Close</button>
    </div>
    <div id="mgCauldronBody"></div>
  </div>
</div>
"""

GAME_JS = r"""
var _mgRarityTable = [
  {rar:'common', chance:0.70},
  {rar:'uncommon', chance:0.20},
  {rar:'rare', chance:0.08},
  {rar:'epic', chance:0.015},
  {rar:'legendary', chance:0.005}
];

var _mgZones = {
  village: { name:'Village', dur:12000, drops:[{item:'green_pearl', label:'Green Pearl', rar:'rare', min:1, max:5, npc:'Kent'}] },
  forest: { name:'Forest', dur:10000, drops:[{item:'wood', label:'Wood', rar:'common', min:1, max:1}] },
  cave: { name:'Cave', dur:11000, drops:[{item:'stone', label:'Stone', rar:'common', min:1, max:1},{item:'iron_ore', label:'Iron Ore', rar:'rare', min:1, max:1}] },
  outerworld: { name:'Outerworld', dur:18000, drops:[{item:'dragons_breath', label:"Dragon's Breath", rar:'legendary', min:1, max:1}] }
};

var _mgItems = {
  wood:'Wood',
  stone:'Stone',
  green_pearl:'Green Pearl',
  dragons_breath:"Dragon's Breath",
  iron_ore:'Iron Ore',
  iron:'Iron',
  lava:'Lava'
};

var _mgItemMeta = {
  wood:{rar:'common'},
  stone:{rar:'common', cauldron:true, cauldronTo:'lava', cauldronBatchIn:100, cauldronBatchOut:1, cauldronTime:10000},
  green_pearl:{rar:'rare'},
  dragons_breath:{rar:'legendary'},
  iron_ore:{rar:'rare', smeltable:true, smeltTo:'iron', smeltTime:3000},
  iron:{rar:'rare'},
  lava:{rar:'exotic'}
};

var _mgSugarRush = { label:'Sugar Rush', rar:'legendary', mult:10, dur:5 * 60 * 1000, chance:0.001 };

var _mgState = null;
var _mgTimer = null;
var _mgPills = [];
var _mgExpandedFurnace = null;
var _mgExpandedCauldron = null;
var _mgPrevPct = {village:0,forest:0,cave:0,outerworld:0};
var _mgResetPending = {village:false,forest:false,cave:false,outerworld:false};
var _mgStorageKeys = ['mg3','miniGameStatePersistent','miniGameState_v5','miniGameState_v4','miniGameState_v3','miniGameState_v2','miniGameState_v1'];
var _mgBootStarted = false;
var _mgBootPoll = null;

function _mgDefaultState() {
  return {
    zone:'village',
    prog:{village:0,forest:0,cave:0,outerworld:0},
    currentDrop:{village:0,forest:0,cave:0,outerworld:0},
    inv:{},
    last:Date.now(),
    sugarUntil:0,
    furnaceUnlocked:false,
    furnaceSlots:1,
    furnaces:[{queueItem:null, queueLeft:0, prog:0}],
    cauldronUnlocked:false,
    cauldronSlots:1,
    cauldrons:[{queueItem:null, queueLeft:0, prog:0, batchOut:0, batchTo:null}]
  };
}

function _mgNormalizeState(s) {
  if (!s || typeof s !== 'object') s = _mgDefaultState();
  if (!_mgZones[s.zone]) s.zone = 'village';
  if (!s.prog) s.prog = {};
  if (!s.currentDrop) s.currentDrop = {};
  ['village','forest','cave','outerworld'].forEach(function(k){
    if (typeof s.prog[k] !== 'number') s.prog[k] = 0;
    if (typeof s.currentDrop[k] !== 'number') s.currentDrop[k] = 0;
  });
  if (!s.inv) s.inv = {};
  ['wood','stone','green_pearl','dragons_breath','iron_ore','iron','lava'].forEach(function(k){
    if (typeof s.inv[k] !== 'number') s.inv[k] = Number(s.inv[k] || 0);
  });
  if (typeof s.last !== 'number') s.last = Date.now();
  if (typeof s.sugarUntil !== 'number') s.sugarUntil = 0;

  if (typeof s.furnaceUnlocked !== 'boolean') s.furnaceUnlocked = false;
  if (typeof s.furnaceSlots !== 'number' || s.furnaceSlots < 1) s.furnaceSlots = 1;
  if (!Array.isArray(s.furnaces)) s.furnaces = [];
  while (s.furnaces.length < s.furnaceSlots) s.furnaces.push({queueItem:null, queueLeft:0, prog:0});
  if (s.furnaces.length > s.furnaceSlots) s.furnaces = s.furnaces.slice(0, s.furnaceSlots);
  for (var i = 0; i < s.furnaces.length; i++) {
    var f = s.furnaces[i] || {};
    s.furnaces[i] = {
      queueItem: f.queueItem || null,
      queueLeft: Math.max(0, Number(f.queueLeft || 0)),
      prog: Math.max(0, Number(f.prog || 0))
    };
  }

  if (typeof s.cauldronUnlocked !== 'boolean') s.cauldronUnlocked = false;
  if (typeof s.cauldronSlots !== 'number' || s.cauldronSlots < 1) s.cauldronSlots = 1;
  if (!Array.isArray(s.cauldrons)) s.cauldrons = [];
  while (s.cauldrons.length < s.cauldronSlots) s.cauldrons.push({queueItem:null, queueLeft:0, prog:0, batchOut:0, batchTo:null});
  if (s.cauldrons.length > s.cauldronSlots) s.cauldrons = s.cauldrons.slice(0, s.cauldronSlots);
  for (var j = 0; j < s.cauldrons.length; j++) {
    var c = s.cauldrons[j] || {};
    s.cauldrons[j] = {
      queueItem: c.queueItem || null,
      queueLeft: Math.max(0, Number(c.queueLeft || 0)),
      prog: Math.max(0, Number(c.prog || 0)),
      batchOut: Math.max(0, Number(c.batchOut || 0)),
      batchTo: c.batchTo || null
    };
  }

  return s;
}

function _mgSetCookie(name, value) {
  try {
    document.cookie = name + '=' + encodeURIComponent(value) + '; path=/; max-age=31536000; SameSite=Lax';
  } catch(e) {}
}

function _mgGetCookie(name) {
  try {
    var prefix = name + '=';
    var parts = document.cookie ? document.cookie.split('; ') : [];
    for (var i = 0; i < parts.length; i++) {
      if (parts[i].indexOf(prefix) === 0) {
        return decodeURIComponent(parts[i].slice(prefix.length));
      }
    }
  } catch(e) {}
  return null;
}

function _mgMigrateLegacy(raw) {
  if (!raw || typeof raw !== 'object') return null;
  var s = _mgDefaultState();

  if (raw.z || raw.zone) s.zone = raw.z || raw.zone;

  var p = raw.p || raw.prog || raw.progress || {};
  if (typeof p === 'number') {
    s.prog[s.zone] = Number(p || 0);
  } else {
    s.prog.village = Number(p.village || 0);
    s.prog.forest = Number(p.forest || 0);
    s.prog.cave = Number(p.cave || 0);
    s.prog.outerworld = Number(p.outerworld || 0);
  }

  var inv = raw.i || raw.inventory || {};
  s.inv.wood = Number(inv.wood || raw.wood || 0);
  s.inv.stone = Number(inv.stone || 0);
  s.inv.green_pearl = Number(inv.green_pearl || 0);
  s.inv.dragons_breath = Number(inv.dragons_breath || 0);
  s.inv.iron_ore = Number(inv.iron_ore || 0);
  s.inv.iron = Number(inv.iron || 0);
  s.inv.lava = Number(inv.lava || 0);

  s.last = Number(raw.t || raw.last || Date.now());
  s.sugarUntil = Number(raw.su || raw.sugarUntil || 0);
  if (raw.currentDrop) s.currentDrop = raw.currentDrop;
  if (typeof raw.furnaceUnlocked === 'boolean') s.furnaceUnlocked = raw.furnaceUnlocked;
  if (typeof raw.furnaceSlots === 'number') s.furnaceSlots = raw.furnaceSlots;
  if (Array.isArray(raw.furnaces)) s.furnaces = raw.furnaces;
  if (typeof raw.cauldronUnlocked === 'boolean') s.cauldronUnlocked = raw.cauldronUnlocked;
  if (typeof raw.cauldronSlots === 'number') s.cauldronSlots = raw.cauldronSlots;
  if (Array.isArray(raw.cauldrons)) s.cauldrons = raw.cauldrons;

  return _mgNormalizeState(s);
}

function _mgSaveState() {
  var raw = JSON.stringify(_mgNormalizeState(_mgGet()));
  try { localStorage.setItem('mg3', raw); } catch(e) {}
  try { localStorage.setItem('miniGameStatePersistent', raw); } catch(e) {}
  try { sessionStorage.setItem('mg3', raw); } catch(e) {}
  try { window.name = 'mg3:' + raw; } catch(e) {}
  _mgSetCookie('mg3', raw);
}

function _mgLoadState() {
  if (_mgState) return _mgState;

  try {
    var ssRaw = sessionStorage.getItem('mg3');
    if (ssRaw) _mgState = _mgMigrateLegacy(JSON.parse(ssRaw));
  } catch(e) {}

  if (!_mgState) {
    for (var i = 0; i < _mgStorageKeys.length; i++) {
      try {
        var raw = localStorage.getItem(_mgStorageKeys[i]);
        if (!raw) continue;
        _mgState = _mgMigrateLegacy(JSON.parse(raw));
        if (_mgState) break;
      } catch(e) {}
    }
  }

  if (!_mgState) {
    try {
      if (window.name && window.name.indexOf('mg3:') === 0) {
        _mgState = _mgMigrateLegacy(JSON.parse(window.name.slice(4)));
      }
    } catch(e) {}
  }

  if (!_mgState) {
    try {
      var cookieRaw = _mgGetCookie('mg3');
      if (cookieRaw) _mgState = _mgMigrateLegacy(JSON.parse(cookieRaw));
    } catch(e) {}
  }

  if (!_mgState) _mgState = _mgDefaultState();
  _mgState = _mgNormalizeState(_mgState);
  return _mgState;
}

function _mgGet() { return _mgLoadState(); }
function _mgPut() { _mgSaveState(); }

function _mgAddPill(msg, t) {
  _mgPills.unshift({msg:msg, t:t || Date.now()});
  if (_mgPills.length > 6) _mgPills.length = 6;
}

function _mgRollRarity() {
  var r = Math.random();
  var acc = 0;
  for (var i = 0; i < _mgRarityTable.length; i++) {
    acc += _mgRarityTable[i].chance;
    if (r <= acc) return _mgRarityTable[i].rar;
  }
  return 'common';
}

function _mgPickDropForZone(zoneKey) {
  var drops = _mgZones[zoneKey].drops || [];
  if (!drops.length) return 0;

  var rolled = _mgRollRarity();
  var same = [];
  for (var i = 0; i < drops.length; i++) {
    if (drops[i].rar === rolled) same.push(i);
  }

  if (!same.length) {
    var available = {};
    for (var j = 0; j < drops.length; j++) available[drops[j].rar] = true;
    var order = ['common','uncommon','rare','epic','legendary'];
    var idx = order.indexOf(rolled);
    for (var d = idx - 1; d >= 0; d--) {
      if (available[order[d]]) { rolled = order[d]; break; }
    }
    if (!available[rolled]) {
      for (var u = idx + 1; u < order.length; u++) {
        if (available[order[u]]) { rolled = order[u]; break; }
      }
    }
    for (var k = 0; k < drops.length; k++) {
      if (drops[k].rar === rolled) same.push(k);
    }
  }

  if (!same.length) return 0;
  return same[Math.floor(Math.random() * same.length)];
}

function _mgRandomizeCurrentDrop(zoneKey) {
  var s = _mgGet();
  s.currentDrop[zoneKey] = _mgPickDropForZone(zoneKey);
}

function _mgGetCurrentDrop(zoneKey) {
  var s = _mgGet();
  var idx = s.currentDrop[zoneKey] || 0;
  return _mgZones[zoneKey].drops[idx] || _mgZones[zoneKey].drops[0];
}

function _mgMaybeSugarRush(now) {
  var s = _mgGet();
  if (Math.random() < _mgSugarRush.chance) {
    s.sugarUntil = now + _mgSugarRush.dur;
    _mgAddPill(_mgSugarRush.label + ' activated! ×10 for 5m', now);
    try { showToast(_mgSugarRush.label + ' activated! ×10 gather speed', 'var(--orange)'); } catch(e) {}
  }
}

function _mgAwardDrop(drop, now) {
  var qty = Math.floor(Math.random() * (drop.max - drop.min + 1)) + drop.min;
  var s = _mgGet();
  s.inv[drop.item] = (s.inv[drop.item] || 0) + qty;
  _mgAddPill(drop.npc ? ('+' + qty + ' ' + drop.label + ' from ' + drop.npc) : ('+' + qty + ' ' + drop.label), now);
  _mgMaybeSugarRush(now);
}

function _mgAdvanceGatheringTo(nowMs) {
  var s = _mgGet();
  var cursor = s.last || nowMs;
  if (!isFinite(cursor) || cursor > nowMs) cursor = nowMs;

  while (cursor < nowMs) {
    var z = _mgZones[s.zone];
    var prog = s.prog[s.zone] || 0;
    var mult = (cursor < (s.sugarUntil || 0)) ? _mgSugarRush.mult : 1;
    var remain = Math.max(0, z.dur - prog);
    var msToDrop = remain / mult;
    var boostEnd = (mult > 1) ? s.sugarUntil : Infinity;
    var nextAt = Math.min(cursor + msToDrop, boostEnd, nowMs);
    var dt = nextAt - cursor;

    if (dt > 0) {
      s.prog[s.zone] = prog + dt * mult;
      cursor = nextAt;
    } else {
      cursor = nowMs;
    }

    if ((s.prog[s.zone] || 0) >= z.dur - 0.0001) {
      s.prog[s.zone] = Math.max(0, (s.prog[s.zone] || 0) - z.dur);
      _mgAwardDrop(_mgGetCurrentDrop(s.zone), cursor);
      _mgRandomizeCurrentDrop(s.zone);
      _mgResetPending[s.zone] = true;
      continue;
    }

    if (cursor >= boostEnd && mult > 1) continue;
    if (cursor >= nowMs) break;
  }
}

function _mgAdvanceFurnaces(dt, nowMs) {
  var s = _mgGet();
  if (!dt || dt <= 0) return;

  for (var i = 0; i < s.furnaces.length; i++) {
    var f = s.furnaces[i];
    if (!f.queueItem || f.queueLeft <= 0) { f.prog = 0; continue; }

    var meta = _mgItemMeta[f.queueItem];
    if (!meta || !meta.smeltable || !meta.smeltTime || !meta.smeltTo) {
      f.queueItem = null; f.queueLeft = 0; f.prog = 0; continue;
    }

    f.prog += dt;
    while (f.queueItem && f.queueLeft > 0 && f.prog >= meta.smeltTime) {
      f.prog -= meta.smeltTime;
      f.queueLeft -= 1;
      s.inv[meta.smeltTo] = (s.inv[meta.smeltTo] || 0) + 1;
      _mgAddPill('+1 ' + _mgItems[meta.smeltTo], nowMs);
      if (f.queueLeft <= 0) {
        f.queueItem = null; f.queueLeft = 0; f.prog = 0; break;
      }
    }
  }
}

function _mgAdvanceCauldrons(dt, nowMs) {
  var s = _mgGet();
  if (!dt || dt <= 0) return;

  for (var i = 0; i < s.cauldrons.length; i++) {
    var c = s.cauldrons[i];
    if (!c.queueItem || c.queueLeft <= 0) { c.prog = 0; continue; }

    var meta = _mgItemMeta[c.queueItem];
    if (!meta || !meta.cauldron || !meta.cauldronTime || !meta.cauldronTo) {
      c.queueItem = null; c.queueLeft = 0; c.prog = 0; c.batchOut = 0; c.batchTo = null; continue;
    }

    c.prog += dt;
    while (c.queueItem && c.queueLeft > 0 && c.prog >= meta.cauldronTime) {
      c.prog -= meta.cauldronTime;
      c.queueLeft -= 1;
      if (c.queueLeft <= 0) {
        s.inv[c.batchTo] = (s.inv[c.batchTo] || 0) + (c.batchOut || 0);
        _mgAddPill('+' + (c.batchOut || 0) + ' ' + _mgItems[c.batchTo], nowMs);
        c.queueItem = null; c.queueLeft = 0; c.prog = 0; c.batchOut = 0; c.batchTo = null;
        break;
      }
    }
  }
}

function _mgSnapBar(bar, zoneKey, pct) {
  if (!bar) return;
  pct = Math.max(0, Math.min(100, pct));
  var prev = _mgPrevPct[zoneKey] || 0;

  if (_mgResetPending[zoneKey] || pct + 0.001 < prev) {
    bar.style.transition = 'none';
    bar.style.width = '0%';
    bar.offsetHeight;
    _mgPrevPct[zoneKey] = 0;
    _mgResetPending[zoneKey] = false;
  }

  bar.style.transition = 'width .12s linear';
  bar.style.width = pct + '%';
  _mgPrevPct[zoneKey] = pct;
}

function _mgSim() {
  var now = Date.now();
  var s = _mgGet();
  var last = s.last || now;
  var dt = Math.max(0, now - last);

  _mgAdvanceGatheringTo(now);
  _mgAdvanceFurnaces(dt, now);
  _mgAdvanceCauldrons(dt, now);

  s.last = now;
  _mgPills = _mgPills.filter(function(p) { return now - p.t < 3000; });
}

function _mgFormatMs(ms) {
  ms = Math.max(0, Math.floor(ms / 1000));
  var m = Math.floor(ms / 60);
  var s = ms % 60;
  return m + ':' + String(s).padStart(2, '0');
}

function _mgDrawBoost() {
  var s = _mgGet();
  var chip = document.getElementById('mgBoostChip');
  var timer = document.getElementById('mgBoostTimer');
  if (!chip || !timer) return;

  var remain = Math.max(0, (s.sugarUntil || 0) - Date.now());
  if (remain > 0) {
    chip.style.display = 'inline-flex';
    timer.textContent = _mgSugarRush.label + ' ends in ' + _mgFormatMs(remain);
    timer.className = 'pos';
    timer.style.fontSize = '11px';
  } else {
    chip.style.display = 'none';
    timer.textContent = 'Sugar Rush inactive';
    timer.className = 'dim';
    timer.style.fontSize = '11px';
  }
}

function _mgDrawZoneItems() {
  var zones = Object.keys(_mgZones);
  for (var i = 0; i < zones.length; i++) {
    var z = zones[i];
    var drop = _mgGetCurrentDrop(z);
    var el = document.getElementById('mgItem-' + z);
    if (!el || !drop) continue;
    el.innerHTML =
      '<div style="display:flex;align-items:center;gap:6px">' +
      '<span style="font-size:13px">' + drop.label + '</span>' +
      '<span class="mg-rarity mg-r-' + drop.rar + '">' + drop.rar.toUpperCase() + '</span>' +
      '</div>' +
      '<span class="dim" style="font-size:11px">' + (_mgZones[z].dur / 1000).toFixed(0) + 's</span>';
  }
}

function _mgDrawMain() {
  var s = _mgGet();
  var banner = document.getElementById('mgBanner');
  if (banner) banner.textContent = 'Active Zone: ' + _mgZones[s.zone].name;

  var pc = document.getElementById('mgPills');
  if (pc) {
    var h = '';
    for (var i = 0; i < _mgPills.length; i++) {
      h += '<span style="font-size:11px;padding:4px 10px;border-radius:99px;background:var(--card);border:1px solid var(--border)">' + _mgPills[i].msg + '</span>';
    }
    pc.innerHTML = h;
  }

  _mgDrawBoost();
  _mgDrawZoneItems();

  var furnaceBtn = document.getElementById('mgFurnaceBtn');
  if (furnaceBtn) {
    furnaceBtn.className = s.furnaceUnlocked ? 'btn btn-blue' : 'btn btn-dim';
    furnaceBtn.textContent = s.furnaceUnlocked ? 'Furnace' : 'Furnace 🔒 1000 Stone';
  }

  var cauldronBtn = document.getElementById('mgCauldronBtn');
  if (cauldronBtn) {
    cauldronBtn.className = s.cauldronUnlocked ? 'btn btn-blue' : 'btn btn-dim';
    cauldronBtn.textContent = s.cauldronUnlocked ? 'Cauldron' : 'Cauldron 🔒 1000 Iron';
  }

  ['village','forest','cave','outerworld'].forEach(function(zk) {
    var active = (s.zone === zk);
    var btn = document.getElementById('mgZ-' + zk);
    var bar = document.getElementById('mgP-' + zk);
    var txt = document.getElementById('mgT-' + zk);

    if (btn) {
      btn.textContent = active ? 'Active' : 'Enter';
      btn.className = active ? 'btn btn-blue' : 'btn btn-dim';
    }

    if (active) {
      var boostMult = (Date.now() < (s.sugarUntil || 0)) ? _mgSugarRush.mult : 1;
      var pct = Math.min(100, ((s.prog[zk] || 0) / _mgZones[zk].dur) * 100);
      var rem = Math.max(0, (_mgZones[zk].dur - (s.prog[zk] || 0)) / boostMult / 1000);
      _mgSnapBar(bar, zk, pct);
      if (txt) txt.textContent = 'Next in ' + rem.toFixed(1) + 's' + (boostMult > 1 ? ' · ×10' : '');
    } else {
      if (bar) {
        bar.style.transition = 'none';
        bar.style.width = '0%';
      }
      _mgPrevPct[zk] = 0;
      _mgResetPending[zk] = false;
      if (txt) txt.textContent = 'Inactive';
    }
  });
}

function _mgRenderFurnaceModal() {
  var s = _mgGet();
  var body = document.getElementById('mgFurnaceBody');
  if (!body) return;

  if (!s.furnaceUnlocked) {
    body.innerHTML = '<div class="dim">Needs 1000 stone to unlock.</div>';
    return;
  }

  var h = '<div class="mg-grid">';
  for (var i = 0; i < s.furnaceSlots; i++) {
    var f = s.furnaces[i];
    var label = f.queueItem ? (_mgItems[f.queueItem] || f.queueItem) : 'Empty';
    var pct = 0;
    if (f.queueItem && f.queueLeft > 0) {
      var meta = _mgItemMeta[f.queueItem];
      pct = Math.min(100, ((f.prog || 0) / meta.smeltTime) * 100);
    }

    h += '<div class="mg-slot">';
    h += '<div class="mg-slot-top"><div class="mg-slot-title">Square ' + (i + 1) + '</div><div class="dim mg-slot-small">' + (f.queueLeft > 0 ? (f.queueLeft + ' left') : 'Idle') + '</div></div>';
    h += '<div style="font-size:12px">' + label + '</div>';
    h += '<div class="mg-slot-bar"><div class="mg-slot-fill" style="width:' + pct + '%"></div></div>';
    h += '<div class="dim mg-slot-small">' + (f.queueItem && f.queueLeft > 0 ? '3.0s per item' : 'Tap to choose item') + '</div>';

    if (_mgExpandedFurnace === i) {
      h += '<div class="mg-slot-drop">';
      if (f.queueItem && f.queueLeft > 0) {
        h += '<div class="dim mg-slot-small">Smelting in progress</div>';
      } else {
        var invKeys = Object.keys(s.inv);
        var options = [];
        for (var j = 0; j < invKeys.length; j++) {
          var key = invKeys[j];
          if ((_mgItemMeta[key] || {}).smeltable && (s.inv[key] || 0) >= 100) options.push(key);
        }
        if (!options.length) {
          h += '<div class="dim mg-slot-small">No smeltable items with 100+ available</div>';
        } else {
          for (var o = 0; o < options.length; o++) {
            h += '<button class="mg-slot-choice" onclick="mgQueueSmelt(' + i + ', \'' + options[o] + '\')">' + _mgItems[options[o]] + ' ×100</button>';
          }
        }
      }
      h += '</div>';
    }

    h += '<div class="mg-slot-actions"><button class="btn btn-dim" style="width:100%;padding:6px 10px;font-size:11px" onclick="mgToggleFurnaceSlot(' + i + ')">' + (_mgExpandedFurnace === i ? 'Hide' : 'Select') + '</button></div>';
    h += '</div>';
  }

  var nextCost = 1000 * (s.furnaceSlots + 1);
  h += '<div class="mg-slot">';
  h += '<div class="mg-slot-top"><div class="mg-slot-title">Locked</div><div class="dim mg-slot-small">' + nextCost + ' stone</div></div>';
  h += '<div style="font-size:12px">Unlock next square</div>';
  h += '<div class="mg-slot-bar"><div class="mg-slot-fill" style="width:0%"></div></div>';
  h += '<div class="dim mg-slot-small">Cost scales by 1000 × N</div>';
  h += '<div class="mg-slot-actions"><button class="btn btn-dim" style="width:100%;padding:6px 10px;font-size:11px" onclick="mgUnlockFurnaceSlot()">Unlock</button></div>';
  h += '</div></div>';

  body.innerHTML = h;
}

function _mgRenderCauldronModal() {
  var s = _mgGet();
  var body = document.getElementById('mgCauldronBody');
  if (!body) return;

  if (!s.cauldronUnlocked) {
    body.innerHTML = '<div class="dim">Needs 1000 iron to unlock.</div>';
    return;
  }

  var h = '<div class="mg-grid">';
  for (var i = 0; i < s.cauldronSlots; i++) {
    var c = s.cauldrons[i];
    var label = c.queueItem ? (_mgItems[c.queueItem] || c.queueItem) : 'Empty';
    var pct = 0;
    if (c.queueItem && c.queueLeft > 0) {
      var meta = _mgItemMeta[c.queueItem];
      pct = Math.min(100, ((c.prog || 0) / meta.cauldronTime) * 100);
    }

    h += '<div class="mg-slot">';
    h += '<div class="mg-slot-top"><div class="mg-slot-title">Square ' + (i + 1) + '</div><div class="dim mg-slot-small">' + (c.queueLeft > 0 ? (c.queueLeft + ' left') : 'Idle') + '</div></div>';
    h += '<div style="font-size:12px">' + label + '</div>';
    h += '<div class="mg-slot-bar"><div class="mg-slot-fill" style="width:' + pct + '%"></div></div>';
    h += '<div class="dim mg-slot-small">' + (c.queueItem && c.queueLeft > 0 ? ('10.0s per item · outputs ' + (c.batchOut || 0) + ' ' + (_mgItems[c.batchTo] || '')) : 'Tap to choose item') + '</div>';

    if (_mgExpandedCauldron === i) {
      h += '<div class="mg-slot-drop">';
      if (c.queueItem && c.queueLeft > 0) {
        h += '<div class="dim mg-slot-small">Brewing in progress</div>';
      } else {
        var keys = Object.keys(s.inv);
        var opts = [];
        for (var j = 0; j < keys.length; j++) {
          var key = keys[j];
          if ((_mgItemMeta[key] || {}).cauldron && (s.inv[key] || 0) >= 100) opts.push(key);
        }
        if (!opts.length) {
          h += '<div class="dim mg-slot-small">No cauldron items with 100+ available</div>';
        } else {
          for (var o = 0; o < opts.length; o++) {
            h += '<button class="mg-slot-choice" onclick="mgQueueCauldron(' + i + ', \'' + opts[o] + '\')">' + _mgItems[opts[o]] + ' ×100</button>';
          }
        }
      }
      h += '</div>';
    }

    h += '<div class="mg-slot-actions"><button class="btn btn-dim" style="width:100%;padding:6px 10px;font-size:11px" onclick="mgToggleCauldronSlot(' + i + ')">' + (_mgExpandedCauldron === i ? 'Hide' : 'Select') + '</button></div>';
    h += '</div>';
  }

  var nextCost = 1000 * (s.cauldronSlots + 1);
  h += '<div class="mg-slot">';
  h += '<div class="mg-slot-top"><div class="mg-slot-title">Locked</div><div class="dim mg-slot-small">' + nextCost + ' iron</div></div>';
  h += '<div style="font-size:12px">Unlock next square</div>';
  h += '<div class="mg-slot-bar"><div class="mg-slot-fill" style="width:0%"></div></div>';
  h += '<div class="dim mg-slot-small">Cost scales by 1000 × N</div>';
  h += '<div class="mg-slot-actions"><button class="btn btn-dim" style="width:100%;padding:6px 10px;font-size:11px" onclick="mgUnlockCauldronSlot()">Unlock</button></div>';
  h += '</div></div>';

  body.innerHTML = h;
}

function _mgDraw() {
  if (!document.getElementById('mgBanner')) return;
  _mgDrawMain();
  _mgRenderFurnaceModal();
  _mgRenderCauldronModal();
}

function _mgTick() {
  _mgSim();
  _mgDraw();
  _mgPut();
}

function _mgForceSave() {
  try { _mgPut(); } catch(e) {}
}

function loadArcade() {
  var s = _mgGet();
  ['village','forest','cave','outerworld'].forEach(function(z) {
    if (typeof s.currentDrop[z] !== 'number' || s.currentDrop[z] < 0 || s.currentDrop[z] >= _mgZones[z].drops.length) {
      s.currentDrop[z] = _mgPickDropForZone(z);
    }
  });

  if (_mgTimer) clearInterval(_mgTimer);
  _mgTick();
  _mgTimer = setInterval(_mgTick, 100);

  try {
    window.removeEventListener('beforeunload', _mgForceSave);
    window.removeEventListener('pagehide', _mgForceSave);
    window.removeEventListener('blur', _mgForceSave);
    window.removeEventListener('focus', _mgTick);
  } catch(e) {}

  window.addEventListener('beforeunload', _mgForceSave);
  window.addEventListener('pagehide', _mgForceSave);
  window.addEventListener('blur', _mgForceSave);
  window.addEventListener('focus', _mgTick);
  document.addEventListener('visibilitychange', function() {
    _mgForceSave();
    if (!document.hidden) _mgTick();
  });

  try {
    window.removeEventListener('pageshow', _mgTick);
  } catch(e) {}
  window.addEventListener('pageshow', _mgTick);
}

function _mgEnsureBoot() {
  if (_mgBootStarted) return;
  if (!document.getElementById('mgBanner')) return;
  _mgBootStarted = true;
  if (_mgBootPoll) {
    clearInterval(_mgBootPoll);
    _mgBootPoll = null;
  }
  loadArcade();
}

function _mgStartBootPoll() {
  if (_mgBootPoll) return;
  _mgBootPoll = setInterval(function() {
    if (document.getElementById('mgBanner')) _mgEnsureBoot();
  }, 250);
}

function mgStopLoop() {
  if (_mgTimer) {
    clearInterval(_mgTimer);
    _mgTimer = null;
  }
  _mgForceSave();
}

function mgEnter(zk) {
  if (!_mgZones[zk]) return;
  var s = _mgGet();
  s.zone = zk;
  s.prog[zk] = 0;
  _mgRandomizeCurrentDrop(zk);
  s.last = Date.now();
  _mgPrevPct[zk] = 0;
  _mgResetPending[zk] = true;
  _mgAddPill('Entered ' + _mgZones[zk].name, Date.now());
  _mgDraw();
  _mgPut();
}

function mgShowInv() {
  var s = _mgGet();
  var body = document.getElementById('mgInvBody');
  if (!body) return;

  var keys = Object.keys(s.inv).filter(function(k) { return s.inv[k] > 0; });
  if (!keys.length) {
    body.innerHTML = '<div class="dim">No items yet.</div>';
  } else {
    keys.sort(function(a,b) { return (_mgItems[a]||a).localeCompare(_mgItems[b]||b); });
    var h = '';
    for (var i = 0; i < keys.length; i++) {
      var k = keys[i], name = _mgItems[k] || k, rar = (_mgItemMeta[k] && _mgItemMeta[k].rar) || 'common';
      h += '<div style="display:flex;justify-content:space-between;align-items:center;padding:10px;border-radius:8px;background:var(--bg);border:1px solid var(--border);margin-bottom:6px">';
      h += '<div style="display:flex;align-items:center;gap:8px"><span style="font-size:13px">' + name + '</span><span class="mg-rarity mg-r-' + rar + '">' + rar.toUpperCase() + '</span></div>';
      h += '<strong style="font-family:monospace;font-size:14px">' + s.inv[k] + '</strong></div>';
    }
    body.innerHTML = h;
  }
  openModal('mgInvOverlay');
}

function mgFurnaceClick() {
  var s = _mgGet();
  if (!s.furnaceUnlocked) {
    if ((s.inv.stone || 0) < 1000) {
      try { showToast('Needs 1000 stone to unlock', 'var(--orange)'); } catch(e) {}
      return;
    }
    s.inv.stone -= 1000;
    s.furnaceUnlocked = true;
    _mgAddPill('Furnace unlocked', Date.now());
    _mgPut();
    try { showToast('Furnace unlocked', 'var(--green)'); } catch(e) {}
  }
  _mgExpandedFurnace = null;
  _mgRenderFurnaceModal();
  openModal('mgFurnaceOverlay');
}

function mgUnlockFurnaceSlot() {
  var s = _mgGet();
  if (!s.furnaceUnlocked) return;
  var cost = 1000 * (s.furnaceSlots + 1);
  if ((s.inv.stone || 0) < cost) {
    try { showToast('Needs ' + cost + ' stone', 'var(--orange)'); } catch(e) {}
    return;
  }
  s.inv.stone -= cost;
  s.furnaceSlots += 1;
  s.furnaces.push({queueItem:null, queueLeft:0, prog:0});
  _mgAddPill('Unlocked furnace square ' + s.furnaceSlots, Date.now());
  _mgPut();
  _mgRenderFurnaceModal();
}

function mgToggleFurnaceSlot(idx) {
  _mgExpandedFurnace = (_mgExpandedFurnace === idx ? null : idx);
  _mgRenderFurnaceModal();
}

function mgQueueSmelt(idx, itemKey) {
  var s = _mgGet();
  var f = s.furnaces[idx];
  var meta = _mgItemMeta[itemKey];
  if (!f || !meta || !meta.smeltable) return;
  if ((s.inv[itemKey] || 0) < 100) {
    try { showToast('Need 100 ' + (_mgItems[itemKey] || itemKey), 'var(--orange)'); } catch(e) {}
    return;
  }
  if (f.queueItem && f.queueLeft > 0) {
    try { showToast('That square is already busy', 'var(--orange)'); } catch(e) {}
    return;
  }
  s.inv[itemKey] -= 100;
  f.queueItem = itemKey;
  f.queueLeft = 100;
  f.prog = 0;
  _mgExpandedFurnace = null;
  _mgAddPill('Started smelting 100 ' + (_mgItems[itemKey] || itemKey), Date.now());
  _mgPut();
  _mgRenderFurnaceModal();
}

function mgCauldronClick() {
  var s = _mgGet();
  if (!s.cauldronUnlocked) {
    if ((s.inv.iron || 0) < 1000) {
      try { showToast('Needs 1000 iron to unlock', 'var(--orange)'); } catch(e) {}
      return;
    }
    s.inv.iron -= 1000;
    s.cauldronUnlocked = true;
    _mgAddPill('Cauldron unlocked', Date.now());
    _mgPut();
    try { showToast('Cauldron unlocked', 'var(--green)'); } catch(e) {}
  }
  _mgExpandedCauldron = null;
  _mgRenderCauldronModal();
  openModal('mgCauldronOverlay');
}

function mgUnlockCauldronSlot() {
  var s = _mgGet();
  if (!s.cauldronUnlocked) return;
  var cost = 1000 * (s.cauldronSlots + 1);
  if ((s.inv.iron || 0) < cost) {
    try { showToast('Needs ' + cost + ' iron', 'var(--orange)'); } catch(e) {}
    return;
  }
  s.inv.iron -= cost;
  s.cauldronSlots += 1;
  s.cauldrons.push({queueItem:null, queueLeft:0, prog:0, batchOut:0, batchTo:null});
  _mgAddPill('Unlocked cauldron square ' + s.cauldronSlots, Date.now());
  _mgPut();
  _mgRenderCauldronModal();
}

function mgToggleCauldronSlot(idx) {
  _mgExpandedCauldron = (_mgExpandedCauldron === idx ? null : idx);
  _mgRenderCauldronModal();
}

function mgQueueCauldron(idx, itemKey) {
  var s = _mgGet();
  var c = s.cauldrons[idx];
  var meta = _mgItemMeta[itemKey];
  if (!c || !meta || !meta.cauldron) return;
  if ((s.inv[itemKey] || 0) < 100) {
    try { showToast('Need 100 ' + (_mgItems[itemKey] || itemKey), 'var(--orange)'); } catch(e) {}
    return;
  }
  if (c.queueItem && c.queueLeft > 0) {
    try { showToast('That square is already busy', 'var(--orange)'); } catch(e) {}
    return;
  }
  s.inv[itemKey] -= 100;
  c.queueItem = itemKey;
  c.queueLeft = 100;
  c.prog = 0;
  c.batchOut = meta.cauldronBatchOut || 1;
  c.batchTo = meta.cauldronTo;
  _mgExpandedCauldron = null;
  _mgAddPill('Started brewing 100 ' + (_mgItems[itemKey] || itemKey), Date.now());
  _mgPut();
  _mgRenderCauldronModal();
}

try {
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', _mgEnsureBoot);
  } else {
    setTimeout(_mgEnsureBoot, 0);
  }
  _mgStartBootPoll();
} catch(e) {}
"""
