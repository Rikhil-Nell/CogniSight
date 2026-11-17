'use strict';
(() => {
  const byId = id => document.getElementById(id);
  const editor = byId('map-editor');
  const token = byId('map-csrf-token').value;
  const roomsList = byId('rooms-list');
  const connectionsList = byId('connections-list');
  const landmarksList = byId('landmarks-list');
  let mapDocument = null, ready = false, saving = false, dirty = false, hasSavedMap = false;

  function setStatus(message) {
    byId('map-status').textContent = message;
  }
  function newId() {
    if (typeof crypto === 'undefined') throw new Error('Secure map identifiers are unavailable in this browser.');
    if (typeof crypto.randomUUID === 'function') return crypto.randomUUID();
    if (typeof crypto.getRandomValues !== 'function') {
      throw new Error('Secure map identifiers are unavailable in this browser.');
    }
    const bytes = crypto.getRandomValues(new Uint8Array(16));
    bytes[6] = (bytes[6] & 0x0f) | 0x40;
    bytes[8] = (bytes[8] & 0x3f) | 0x80;
    const hex = Array.from(bytes, value => value.toString(16).padStart(2, '0')).join('');
    return hex.slice(0, 8) + '-' + hex.slice(8, 12) + '-' + hex.slice(12, 16) +
      '-' + hex.slice(16, 20) + '-' + hex.slice(20);
  }
  function generatedId() {
    try {
      return newId();
    } catch (_) {
      setStatus('Secure map identifiers are unavailable in this browser.');
      return null;
    }
  }
  function blankMap() {
    return {
      schema_version: 1,
      map_id: newId(),
      name: '',
      rooms: [],
      connections: [],
      landmarks: []
    };
  }
  function node(tag, className, text) {
    const item = document.createElement(tag);
    if (className) item.className = className;
    if (text !== undefined) item.textContent = text;
    return item;
  }
  function control(labelText, value, maxLength, onInput) {
    const wrapper = node('label', 'map-field');
    wrapper.appendChild(node('span', '', labelText));
    const input = node('input', 'map-input');
    input.type = 'text';
    input.maxLength = maxLength;
    input.value = value;
    input.autocomplete = 'off';
    input.className = 'map-input map-control';
    input.addEventListener('input', event => {
      onInput(event.target.value);
      changed();
    });
    wrapper.appendChild(input);
    return wrapper;
  }
  function selectControl(labelText, value, options, onChange) {
    const wrapper = node('label', 'map-field');
    wrapper.appendChild(node('span', '', labelText));
    const select = node('select', 'map-input map-control');
    for (const option of options) {
      const item = node('option', '', option.label);
      item.value = option.value;
      select.appendChild(item);
    }
    select.value = value;
    select.addEventListener('change', event => {
      onChange(event.target.value);
      changed();
    });
    wrapper.appendChild(select);
    return wrapper;
  }
  function removeButton(label, callback) {
    const button = node('button', 'button button-quiet map-remove', label);
    button.type = 'button';
    button.addEventListener('click', callback);
    return button;
  }
  function changed() {
    dirty = true;
    setStatus('Unsaved changes. Save the map before exporting it.');
    updateControls();
  }
  function roomOptions() {
    const names = new Map();
    for (const room of mapDocument.rooms) {
      const name = room.name.trim() || 'Unnamed room';
      names.set(name, (names.get(name) || 0) + 1);
    }
    return mapDocument.rooms.map(room => {
      const name = room.name.trim() || 'Unnamed room';
      return {
        value: room.id,
        label: names.get(name) > 1 ? name + ' · ' + room.id.slice(0, 8) : name
      };
    });
  }
  function renderRooms() {
    roomsList.replaceChildren();
    if (mapDocument.rooms.length === 0) {
      roomsList.appendChild(node('p', 'map-empty', 'No rooms added yet.'));
      return;
    }
    for (const room of mapDocument.rooms) {
      const row = node('div', 'map-row');
      row.appendChild(control('Room name', room.name, 100, value => { room.name = value; }));
      row.appendChild(removeButton('Remove room', () => {
        const inUse = mapDocument.connections.some(item =>
          item.from_room_id === room.id || item.to_room_id === room.id
        ) || mapDocument.landmarks.some(item => item.room_id === room.id);
        if (inUse) {
          setStatus("Remove this room's connections and landmarks before deleting the room.");
          return;
        }
        mapDocument.rooms = mapDocument.rooms.filter(item => item.id !== room.id);
        changed();
        render();
      }));
      roomsList.appendChild(row);
    }
  }
  function renderConnections() {
    connectionsList.replaceChildren();
    if (mapDocument.connections.length === 0) {
      connectionsList.appendChild(node('p', 'map-empty', 'No connections added yet.'));
      return;
    }
    const options = roomOptions();
    for (const connection of mapDocument.connections) {
      const row = node('div', 'map-row map-connection-row');
      row.appendChild(selectControl('From room', connection.from_room_id, options, value => {
        connection.from_room_id = value;
      }));
      row.appendChild(selectControl('To room', connection.to_room_id, options, value => {
        connection.to_room_id = value;
      }));
      row.appendChild(selectControl('Connection type', connection.kind, [
        {value: 'doorway', label: 'Doorway'},
        {value: 'corridor', label: 'Corridor'}
      ], value => { connection.kind = value; }));
      row.appendChild(removeButton('Remove connection', () => {
        mapDocument.connections = mapDocument.connections.filter(item => item.id !== connection.id);
        changed();
        render();
      }));
      connectionsList.appendChild(row);
    }
  }
  function renderLandmarks() {
    landmarksList.replaceChildren();
    if (mapDocument.landmarks.length === 0) {
      landmarksList.appendChild(node('p', 'map-empty', 'No landmarks added yet.'));
      return;
    }
    const options = roomOptions();
    for (const landmark of mapDocument.landmarks) {
      const row = node('div', 'map-row map-landmark-row');
      row.appendChild(control('Landmark ID', landmark.id, 64, value => { landmark.id = value; }));
      row.appendChild(control('Label', landmark.label, 100, value => { landmark.label = value; }));
      row.appendChild(selectControl('Room', landmark.room_id, options, value => {
        landmark.room_id = value;
      }));
      row.appendChild(removeButton('Remove landmark', () => {
        mapDocument.landmarks = mapDocument.landmarks.filter(item => item !== landmark);
        changed();
        render();
      }));
      landmarksList.appendChild(row);
    }
  }
  function updateControls() {
    for (const controlNode of editor.querySelectorAll('.map-control')) {
      controlNode.disabled = !ready || saving;
    }
    byId('map-name').disabled = !ready || saving;
    const rooms = mapDocument ? mapDocument.rooms : [];
    const connections = mapDocument ? mapDocument.connections : [];
    const landmarks = mapDocument ? mapDocument.landmarks : [];
    byId('add-room').disabled = !ready || saving || rooms.length >= 100;
    byId('add-connection').disabled = !ready || saving ||
      rooms.length < 2 || connections.length >= 100;
    byId('add-landmark').disabled = !ready || saving ||
      rooms.length === 0 || landmarks.length >= 100;
    byId('save-map').disabled = !ready || saving || !dirty;
    const exportDisabled = !ready || saving || !hasSavedMap || dirty;
    const exportLink = byId('export-map');
    exportLink.setAttribute('aria-disabled', String(exportDisabled));
    exportLink.tabIndex = exportDisabled ? -1 : 0;
    for (const button of editor.querySelectorAll('.map-remove')) {
      button.disabled = !ready || saving;
    }
  }
  function render() {
    byId('map-name').value = mapDocument.name;
    renderRooms();
    renderConnections();
    renderLandmarks();
    updateControls();
  }
  async function loadMap() {
    try {
      const response = await fetch('/dashboard/indoor-map/data', {
        credentials: 'same-origin',
        cache: 'no-store'
      });
      if (!response.ok) {
        setStatus(response.status === 401 || response.status === 403 ?
          'Session ended. Sign in again to configure the map.' :
          'Saved map could not be loaded. Existing data was not changed.');
        return;
      }
      const result = await response.json();
      if (!result || !Object.prototype.hasOwnProperty.call(result, 'map')) {
        throw new Error('Invalid map response');
      }
      mapDocument = result.map || blankMap();
      hasSavedMap = result.map !== null;
      ready = true;
      editor.setAttribute('aria-busy', 'false');
      setStatus(hasSavedMap ? 'Saved map loaded.' : 'Name the home map, then add rooms.');
      render();
    } catch (error) {
      setStatus(error && error.message === 'Secure map identifiers are unavailable in this browser.' ?
        error.message : 'Map service is unavailable. Existing data was not changed.');
    }
  }
  async function saveMap() {
    if (!ready || saving || !dirty) return;
    saving = true;
    updateControls();
    try {
      const response = await fetch('/dashboard/indoor-map/data', {
        method: 'PUT',
        credentials: 'same-origin',
        cache: 'no-store',
        headers: {'Content-Type': 'application/json', 'X-CSRFToken': token},
        body: JSON.stringify(mapDocument)
      });
      const result = await response.json();
      if (!response.ok) {
        setStatus(result && result.message ? result.message : 'Map could not be saved.');
        return;
      }
      if (!result || result.saved !== true || !result.map) {
        setStatus('Map service returned an invalid save response.');
        return;
      }
      mapDocument = result.map;
      dirty = false;
      hasSavedMap = true;
      setStatus('Map saved on this Pi. Export the saved JSON when needed.');
      render();
    } catch (_) {
      setStatus('Map could not be saved. Check the connection and try again.');
    } finally {
      saving = false;
      updateControls();
    }
  }
  byId('map-name').addEventListener('input', event => {
    if (!ready || saving) return;
    mapDocument.name = event.target.value;
    changed();
  });
  byId('add-room').addEventListener('click', () => {
    if (!ready || saving || mapDocument.rooms.length >= 100) return;
    const id = generatedId();
    if (!id) return;
    mapDocument.rooms.push({id, name: ''});
    changed();
    render();
  });
  byId('add-connection').addEventListener('click', () => {
    if (!ready || saving || mapDocument.rooms.length < 2 || mapDocument.connections.length >= 100) return;
    const id = generatedId();
    if (!id) return;
    mapDocument.connections.push({
      id,
      from_room_id: mapDocument.rooms[0].id,
      to_room_id: mapDocument.rooms[1].id,
      kind: 'doorway'
    });
    changed();
    render();
  });
  byId('add-landmark').addEventListener('click', () => {
    if (!ready || saving || mapDocument.rooms.length === 0 || mapDocument.landmarks.length >= 100) return;
    mapDocument.landmarks.push({id: '', room_id: mapDocument.rooms[0].id, label: ''});
    changed();
    render();
  });
  byId('save-map').addEventListener('click', saveMap);
  byId('export-map').addEventListener('click', event => {
    if (!ready || saving || !hasSavedMap || dirty) {
      event.preventDefault();
      setStatus('Save the current changes before exporting the map.');
    }
  });
  window.addEventListener('beforeunload', event => {
    if (!dirty) return;
    event.preventDefault();
    event.returnValue = '';
  });
  updateControls();
  loadMap();
})();
