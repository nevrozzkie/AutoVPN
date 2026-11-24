let state = JSON.parse(document.querySelector('#port-forwarding-data').textContent);
let groups = state.config.groups;
const form = document.querySelector('#settings');
const fields = document.querySelector('#forwarding-fields');
const groupList = document.querySelector('#groups');
const addGroup = document.querySelector('#add-group');
const enabled = document.querySelector('#enabled');
const status = document.querySelector('#status');
let picker = null;
enabled.checked = state.config.enabled;

function setStatus(message, tone = '') {
  status.textContent = message;
  status.dataset.tone = tone;
}

function markChanged() {
  for (const feedback of groupList.querySelectorAll('.port-feedback')) feedback.hidden = true;
  setStatus('Есть несохранённые изменения');
}

function ruleName(rule) {
  return `${rule.protocol} ${rule.external || '…'} → ${rule.internal || '…'}`;
}

function updateTarget(rule, button) {
  const user = state.users.find(item => item.id === rule.client_id);
  const device = user?.devices.find(item => item.id === rule.device_id);
  const name = device ? `${user.name} · ${device.name}` : 'Выбрать устройство';
  button.textContent = name;
  button.setAttribute('aria-label', `${ruleName(rule)}, ${name}`);
}

function renderGroups() {
  groupList.replaceChildren(...groups.map(group => {
    const card = document.querySelector('#group-template').content.firstElementChild.cloneNode(true);
    const name = card.querySelector('.group-name');
    const toggle = card.querySelector('.group-enabled');
    const portList = card.querySelector('.port-list');
    const addPort = card.querySelector('.add-port');
    name.value = group.name;
    toggle.checked = group.enabled;
    card.classList.toggle('group-off', !group.enabled);
    name.addEventListener('input', () => {
      group.name = name.value;
      markChanged();
    });
    toggle.addEventListener('change', () => {
      group.enabled = toggle.checked;
      card.classList.toggle('group-off', !group.enabled);
      markChanged();
    });
    card.querySelector('.group-remove').addEventListener('click', () => {
      const index = groups.indexOf(group);
      closePicker();
      groups.splice(index, 1);
      renderGroups();
      const nextName = (groupList.children[index] ?? groupList.lastElementChild)?.querySelector('.group-name');
      (nextName ?? addGroup).focus();
      markChanged();
    });
    addPort.addEventListener('click', () => {
      closePicker();
      group.rules.push({ enabled: true, protocol: 'UDP', external: '', internal: '', client_id: null, device_id: null });
      renderPorts(group.rules, portList, addPort);
      portList.lastElementChild.querySelector('.external').focus();
      markChanged();
    });
    renderPorts(group.rules, portList, addPort);
    return card;
  }));
}

function renderPorts(rules, portList, addPort) {
  portList.replaceChildren(...rules.map(rule => {
    const row = document.querySelector('#port-template').content.firstElementChild.cloneNode(true);
    const target = row.querySelector('.target');
    const toggle = row.querySelector('.port-enabled');
    toggle.checked = rule.enabled;
    row.classList.toggle('is-off', !rule.enabled);
    toggle.addEventListener('change', () => {
      rule.enabled = toggle.checked;
      row.classList.toggle('is-off', !rule.enabled);
      markChanged();
    });
    const fields = {
      protocol: row.querySelector('select'),
      external: row.querySelector('.external'),
      internal: row.querySelector('.internal'),
    };
    for (const [key, field] of Object.entries(fields)) {
      field.value = rule[key];
      field.addEventListener('input', () => {
        rule[key] = field.value;
        markChanged();
        updateTarget(rule, target);
      });
    }
    updateTarget(rule, target);
    const assignment = row.querySelector('.assignment');
    target.addEventListener('click', () => {
      if (picker?.button === target) closePicker();
      else openPicker(rule, target, assignment);
    });
    row.querySelector('.remove').addEventListener('click', () => {
      const index = rules.indexOf(rule);
      closePicker();
      rules.splice(index, 1);
      renderPorts(rules, portList, addPort);
      const nextTarget = (portList.children[index] ?? portList.lastElementChild)?.querySelector('.target');
      (nextTarget ?? addPort).focus();
      markChanged();
    });
    return row;
  }));
}

function closePicker({ restoreFocus = false } = {}) {
  if (!picker) return;
  const { button, panel } = picker;
  picker = null;
  panel.remove();
  button.setAttribute('aria-expanded', 'false');
  button.removeAttribute('aria-controls');
  if (restoreFocus) button.focus();
}

function openPicker(rule, button, assignment) {
  closePicker();
  const panel = document.createElement('div');
  panel.id = 'device-picker';
  panel.className = 'device-picker';
  panel.setAttribute('role', 'group');
  panel.setAttribute('aria-label', 'Выбор устройства');
  picker = { button, panel };
  button.setAttribute('aria-expanded', 'true');
  button.setAttribute('aria-controls', panel.id);
  assignment.append(panel);
  const expanded = new Set(rule.client_id ? [rule.client_id] : []);
  renderUsers(rule, button, panel, expanded);
}

function renderUsers(rule, button, panel, expanded) {
  const label = document.createElement('p');
  label.className = 'picker-label muted';
  label.textContent = state.users.length ? 'Выберите одно устройство' : 'Нет активных пользователей AmneziaWG.';
  panel.replaceChildren(label, ...state.users.map(user => {
    const card = document.querySelector('#user-template').content.firstElementChild.cloneNode(true);
    const deviceList = card.querySelector('.devices');
    const toggle = card.querySelector('.expand');
    card.querySelector('.user-name').textContent = user.name;
    deviceList.id = `devices-${user.id}`;

    function updateDisclosure() {
      const open = expanded.has(user.id);
      deviceList.hidden = !open;
      toggle.setAttribute('aria-expanded', String(open));
      toggle.setAttribute('aria-label', `${open ? 'Свернуть' : 'Раскрыть'} устройства — ${user.name}`);
    }

    updateDisclosure();
    toggle.setAttribute('aria-controls', deviceList.id);
    toggle.addEventListener('click', () => {
      if (expanded.has(user.id)) expanded.delete(user.id);
      else expanded.add(user.id);
      updateDisclosure();
    });
    deviceList.append(...user.devices.map(device => {
      const label = document.querySelector('#device-template').content.firstElementChild.cloneNode(true);
      const input = label.querySelector('input');
      label.querySelector('span').textContent = device.name;
      label.querySelector('.muted').textContent = device.ip;
      input.checked = rule.client_id === user.id && rule.device_id === device.id;
      input.addEventListener('change', () => {
        rule.client_id = user.id;
        rule.device_id = device.id;
        updateTarget(rule, button);
        markChanged();
        closePicker({ restoreFocus: true });
      });
      return label;
    }));
    return card;
  }));
  if (rule.client_id) {
    const clear = document.createElement('button');
    clear.type = 'button';
    clear.className = 'clear-device';
    clear.textContent = 'Сбросить выбор';
    clear.addEventListener('click', () => {
      rule.client_id = null;
      rule.device_id = null;
      updateTarget(rule, button);
      markChanged();
      closePicker({ restoreFocus: true });
    });
    panel.append(clear);
  }
}

addGroup.addEventListener('click', () => {
  closePicker();
  groups.push({ name: '', enabled: true, rules: [] });
  renderGroups();
  groupList.lastElementChild.querySelector('.group-name').focus();
  markChanged();
});

enabled.addEventListener('change', markChanged);

document.addEventListener('click', event => {
  if (picker && !picker.button.parentElement.contains(event.target)) closePicker();
});
document.addEventListener('focusin', event => {
  if (picker && !picker.button.parentElement.contains(event.target)) closePicker();
});
document.addEventListener('keydown', event => {
  if (picker && event.key === 'Escape') closePicker({ restoreFocus: true });
});

document.querySelector('#add-farcry').addEventListener('click', () => {
  closePicker();
  groups.push({
    name: 'FarCry4', enabled: true,
    rules: [9000, 9103].map(port => ({
      enabled: true, protocol: 'UDP', external: port, internal: port, client_id: null, device_id: null,
    })),
  });
  renderGroups();
  groupList.lastElementChild.querySelector('.target').focus();
  markChanged();
});

function showResult() {
  const result = state.result;
  if (result.state === 'ERROR') {
    setStatus(`Сохранено в панели. Состояние правил на боксе не подтверждено. ${result.error}`, 'error');
  } else if (result.state === 'PARTIAL') {
    setStatus(`На боксе ${state.ip} применены доступные правила. Есть ошибки.`, 'error');
  } else if (result.state === 'APPLIED') {
    setStatus(state.ip ? `Настройки применены на боксе ${state.ip}` : 'Настройки сохранены', 'success');
  } else if (state.revision) {
    setStatus('Текущие настройки проброса на боксе ещё не подтверждены.');
  }
  groups.forEach((group, groupIndex) => {
    const portList = groupList.children[groupIndex].querySelector('.port-list');
    group.rules.forEach((rule, index) => {
      const feedback = portList.children[index].querySelector('.port-feedback');
      const active = state.config.enabled && group.enabled && rule.enabled;
      const error = result.rules?.[`${groupIndex}.${index}`];
      feedback.dataset.tone = error ? 'error' : '';
      if (error) feedback.textContent = error;
      else if (!active) feedback.textContent = result.state === 'ERROR' ? 'Отключение не подтверждено' : 'Отключено';
      else if (['APPLIED', 'PARTIAL'].includes(result.state)) feedback.textContent = 'Установлено на боксе';
      else feedback.textContent = 'Установка на боксе не подтверждена';
      feedback.hidden = false;
    });
  });
}

form.addEventListener('submit', async event => {
  event.preventDefault();
  const submitted = { enabled: enabled.checked, groups: [] };
  for (const [groupIndex, group] of groups.entries()) {
    const card = groupList.children[groupIndex];
    const name = group.name.trim();
    if (!name) {
      setStatus('Введите название группы.', 'error');
      card.querySelector('.group-name').focus();
      return;
    }
    const normalized = { ...group, name, rules: [] };
    const portList = card.querySelector('.port-list');
    for (const [index, rule] of group.rules.entries()) {
      const row = portList.children[index];
      const invalid = row.querySelector('.external:invalid, .internal:invalid');
      if (invalid) {
        setStatus(`Группа «${name}». Укажите порты от 1 до 65535.`, 'error');
        invalid.focus();
        return;
      }
      if (submitted.enabled && group.enabled && rule.enabled && !rule.client_id) {
        setStatus(`Группа «${name}». Выберите устройство для ${ruleName(rule)}.`, 'error');
        openPicker(rule, row.querySelector('.target'), row.querySelector('.assignment'));
        picker.panel.querySelector('.expand')?.focus();
        return;
      }
      normalized.rules.push({ ...rule, external: Number(rule.external), internal: Number(rule.internal) });
    }
    submitted.groups.push(normalized);
  }
  closePicker();
  fields.disabled = true;
  setStatus('Применяем правила по SSH…');
  try {
    const response = await fetch(form.action, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ revision: state.revision, config: submitted }),
    });
    const data = await response.json();
    if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : 'Проверьте настройки портов.');
    state = data;
    groups = state.config.groups;
    enabled.checked = state.config.enabled;
    renderGroups();
    showResult();
  } catch (error) {
    setStatus(error.message || 'Панель не подтвердила сохранение.', 'error');
  } finally {
    fields.disabled = false;
  }
});

renderGroups();
showResult();
